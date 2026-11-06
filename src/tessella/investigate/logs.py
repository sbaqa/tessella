"""Backend log lines from the time a test ran. Sources: a file, Loki or Elasticsearch.

The window is the test's own start and end from the report (Allure and JUnit record them),
plus a few seconds either side, so the lines belong to this test and not its neighbours.
Within the window, errors with their tracebacks come first, then warnings and failed
requests, then lines naming an endpoint the failure mentions. Ordinary lines are left out:
a window of "200 OK" lines is noise to a small model. Every line is redacted.

Loki and Elasticsearch go through net.client, so their host must be on network.allow.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tessella import net
from tessella.config import Logs
from tessella.models import Case, Evidence, EvidenceKind
from tessella.redact import redact_text

PAD = timedelta(seconds=1)  # either side of the test's own start and end
TAIL_BYTES = 50 * 2**20  # a log file is read from at most this far before its end

_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,](\d{1,6}))?"
                   r"\s?(Z|[+-]\d{2}:?\d{2})?")
_ERROR = re.compile(r"\b(ERROR|CRITICAL|FATAL|SEVERE|Traceback|Exception|panic)\b")
_WARN = re.compile(r"\b(WARN|WARNING)\b|-> [45]\d\d\b| [45]\d\d in \d")
_PATH = re.compile(r"(/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-{}.]+)+)")
_EXC = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception)\b.*)$")


@dataclass
class Block:
    """One log entry: its first line and the continuation lines under it (a traceback)."""

    at: datetime | None
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def parse_time(line: str, utc: bool = False) -> datetime | None:
    """The timestamp at the start of a line, as naive local time (like the test times)."""
    m = _TIME.search(line[:40])
    if not m:
        return None
    date, clock, frac, zone = m.groups()
    try:
        t = datetime.fromisoformat(f"{date}T{clock}.{(frac or '0').ljust(6, '0')}")
    except ValueError:
        return None
    if zone:
        z = "+00:00" if zone == "Z" else (zone if ":" in zone else f"{zone[:3]}:{zone[3:]}")
        t = datetime.fromisoformat(f"{t.isoformat()}{z}")
    elif utc:
        t = t.replace(tzinfo=UTC)
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def blocks(lines: list[str], utc: bool = False) -> list[Block]:
    out: list[Block] = []
    for line in lines:
        at = parse_time(line, utc)
        if at is not None or not out:
            out.append(Block(at, [line]))
        else:
            out[-1].lines.append(line)
    return out


def needles(case: Case) -> list[str]:
    """Endpoints the failure mentions, with numbers kept: the log names the same request."""
    text = f"{case.message}\n{case.stack[:3000]}"
    found = [p for p in _PATH.findall(text) if not p.endswith((".py", ".js", ".ts", ".java"))]
    return list(dict.fromkeys(found))[:5]


def window(case: Case, cfg: Logs, widen: float = 1.0) -> tuple[datetime, datetime] | None:
    if case.started_at or case.failed_at:
        start = case.started_at or case.failed_at
        end = case.failed_at or case.started_at
        extra = timedelta(seconds=cfg.window_seconds * (widen - 1)) if widen > 1 else timedelta(0)
        return start - PAD - extra, end + PAD + extra
    return None


def score(b: Block, wanted: list[str]) -> int:
    first = b.lines[0]
    s = 0
    if _ERROR.search(b.text):
        s += 3
    elif _WARN.search(first):
        s += 2
    if any(n in b.text for n in wanted):
        s += 2
    return s


def pick(found: list[Block], wanted: list[str], max_lines: int) -> list[Block]:
    """The highest-scoring blocks that fit in max_lines, back in time order."""
    scored = [(score(b, wanted), i, b) for i, b in enumerate(found)]
    scored = [x for x in scored if x[0] > 0]
    kept, used = [], 0
    for _s, i, b in sorted(scored, key=lambda x: (-x[0], x[1])):
        size = min(len(b.lines), max_lines)
        if used + size > max_lines and kept:
            continue
        kept.append((i, b if len(b.lines) <= max_lines else Block(b.at, b.lines[-max_lines:])))
        used += size
    return [b for _, b in sorted(kept, key=lambda x: x[0])]


def _read_file(path: Path) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
            fh.readline()  # drop the partial first line
        return fh.read().decode(errors="ignore").splitlines()


def _headers(cfg: Logs) -> dict[str, str]:
    token = os.environ.get(cfg.token_env) if cfg.token_env else None
    return {"Authorization": f"Bearer {token}"} if token else {}


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC)  # naive local time to UTC


def from_loki(cfg: Logs, lo: datetime, hi: datetime, allow: list[str]) -> list[Block]:
    if not (cfg.url and cfg.query):
        return []
    params = {"query": cfg.query, "start": str(int(_utc(lo).timestamp() * 1e9)),
              "end": str(int(_utc(hi).timestamp() * 1e9)), "limit": str(cfg.max_lines * 5),
              "direction": "forward"}
    with net.client(allow, timeout=20, headers=_headers(cfg)) as c:
        r = c.get(f"{cfg.url.rstrip('/')}/loki/api/v1/query_range", params=params)
        r.raise_for_status()
        streams = (r.json().get("data") or {}).get("result") or []
    entries = sorted((int(ns), line) for s in streams for ns, line in s.get("values") or [])
    return [Block(datetime.fromtimestamp(ns / 1e9), line.splitlines() or [""])
            for ns, line in entries]


def from_elasticsearch(cfg: Logs, lo: datetime, hi: datetime, allow: list[str]) -> list[Block]:
    if not cfg.url:
        return []
    ts = cfg.timestamp_field
    query: dict = {"bool": {"filter": [{"range": {ts: {"gte": _utc(lo).isoformat(),
                                                       "lte": _utc(hi).isoformat()}}}]}}
    if cfg.query:
        query["bool"]["must"] = [{"query_string": {"query": cfg.query}}]
    body = {"size": cfg.max_lines * 5, "sort": [{ts: "asc"}], "query": query}
    with net.client(allow, timeout=20, headers=_headers(cfg)) as c:
        r = c.post(f"{cfg.url.rstrip('/')}/{cfg.index}/_search", json=body)
        r.raise_for_status()
        hits = (r.json().get("hits") or {}).get("hits") or []
    out = []
    for h in hits:
        src = h.get("_source") or {}
        text = str(src.get("message") or src.get("log") or src)
        out.append(Block(parse_time(str(src.get(ts, ""))), text.splitlines() or [""]))
    return out


def gather(case: Case, cfg: Logs, allow: list[str], widen: float = 1.0) -> tuple[list[Block], str]:
    """Blocks in the case's window (or, for a file without test times, the whole tail)."""
    span = window(case, cfg, widen)
    if cfg.source == "file":
        if not cfg.path or not Path(cfg.path).is_file():
            return [], ""
        found = blocks(_read_file(Path(cfg.path)), cfg.utc)
        if span:
            lo, hi = span
            found = [b for b in found if b.at and lo <= b.at <= hi]
        return found, cfg.path
    if span is None:
        return [], ""  # a remote query needs a time window
    fetch = from_loki if cfg.source == "loki" else from_elasticsearch
    return fetch(cfg, span[0], span[1], allow), cfg.url or cfg.source


def log_evidence(case: Case, cfg: Logs, allow: list[str], redact_keys: list[str],
                 needle: str | None = None, widen: float = 1.0) -> Evidence | None:
    """Evidence from the logs, or None when the window holds nothing worth reading.
    `needle` (the investigator's tool) keeps only lines that contain it."""
    found, ref = gather(case, cfg, allow, widen)
    if not found:
        return None
    wanted = needles(case)
    if needle:
        found = [b for b in found if needle in b.text]
        wanted = [needle]
        kept = found[-cfg.max_lines:]
    else:
        kept = pick(found, wanted, cfg.max_lines)
    if not kept:
        return None
    lines = [redact_text(line, redact_keys) for b in kept for line in b.lines]
    errors = [b for b in kept if _ERROR.search(b.text)]
    last = next((m.group(1) for b in reversed(errors) for line in reversed(b.lines)
                 if (m := _EXC.match(line))), "")
    times = [b.at for b in kept if b.at]
    span = f" between {min(times):%H:%M:%S} and {max(times):%H:%M:%S}" if times else ""
    what = (f"{len(errors)} error(s)" + (f", last: {redact_text(last, redact_keys)[:120]}"
                                         if last else "")) if errors else "warnings only"
    return Evidence(kind=EvidenceKind.log,
                    summary=f"{len(lines)} log line(s){span}: {what}"
                            + (f" (lines containing {needle!r})" if needle else ""),
                    excerpt="\n".join(lines), ref=ref)
