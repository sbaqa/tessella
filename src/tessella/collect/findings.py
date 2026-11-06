"""Findings from established API and security testers, read as probe findings.

Tessella does not send exploratory traffic itself. Schemathesis (property-based testing of
an OpenAPI or GraphQL schema) and OWASP ZAP (web security scanner) do that well; their
reports come in here like any other report, as Cases with origin `probe_finding`, and go
through the same clustering, recall, verdict and review queue as failed tests.

- Schemathesis: the JUnit report, `schemathesis run ... --report junit` (v4) or
  `--junit-xml` (v3). One Case per failing example of an operation.
- ZAP: the traditional JSON report (`zap-baseline.py -J report.json`, `zap-full-scan.py`,
  or Report > Generate > Traditional JSON). One Case per alert per site.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from tessella.models import Case, Evidence, EvidenceKind, Origin
from tessella.redact import MASK, redact_text

# Schemathesis check codes (`st explain <code>`) -> the probe oracle they correspond to and
# a severity. Codes not listed are schema conformance problems: medium.
SCHEMATHESIS_CHECKS: dict[str, tuple[str, str]] = {
    "ST101": ("server_error", "high"),
    "ST102": ("server_error", "high"),
    "ST112": ("slow_response", "medium"),
    "ST201": ("accepted_invalid", "medium"),
    "ST203": ("accepted_invalid", "medium"),
    "ST301": ("state_check", "high"),
    "ST302": ("state_check", "high"),
    "ST401": ("authz", "critical"),
    "ST402": ("authz", "critical"),
}
# Older Schemathesis versions print check names without codes.
_NAMES = {"server error": "ST101", "use after free": "ST301",
          "ensure resource availability": "ST302", "ignored authentication": "ST401",
          "negative data rejection": "ST201", "response time exceeded": "ST112"}
# Oracles that leave no doubt the product is wrong: judged without a model.
CERTAIN = {"server_error", "state_check", "authz"}

ZAP_RISK = {"0": "info", "1": "low", "2": "medium", "3": "high"}
RISK_ORDER = ["info", "low", "medium", "high", "critical"]

_ENTRY = re.compile(r"^\d+\.\s+Test Case ID:\s*(\S+)", re.MULTILINE)
_CHECK = re.compile(r"^- (?:(ST\d{3}) )?(.+)$", re.MULTILINE)
_STATUS = re.compile(r"^\[(\d{3})\]", re.MULTILINE)
_CURL = re.compile(r"^\s*(curl .+)$", re.MULTILINE)
_HEADER = re.compile(r"(-H\s+)(['\"])([^:'\"]+):\s*[^'\"]*\2")


def _signature(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def redact_curl(command: str, keys: list[str]) -> str:
    """A curl line with the values of secret headers masked, then the usual text rules."""
    secret = {k.lower() for k in keys} | {"authorization", "cookie", "x-api-key",
                                           "proxy-authorization"}

    def mask(m: re.Match) -> str:
        name = m.group(3).strip()
        if name.lower() in secret:
            return f"{m.group(1)}{m.group(2)}{name}: {MASK}{m.group(2)}"
        return m.group(0)

    return redact_text(_HEADER.sub(mask, command), keys)


def _text(value) -> str:
    """ZAP descriptions are small HTML fragments."""
    out = re.sub(r"<[^>]+>", "\n", str(value or ""))
    return re.sub(r"\n\s*\n+", "\n", html.unescape(out)).strip()


class SchemathesisReader:
    name = "schemathesis"

    def can_read(self, path: Path) -> bool:
        if path.suffix.lower() != ".xml" or not path.is_file():
            return False
        head = path.read_text(errors="ignore")[:2000]
        return bool(re.search(r"<testsuite[^>]*name=\"schemathesis\"", head))

    def read(self, path: Path, redact_keys: list[str]) -> list[Case]:
        cases: list[Case] = []
        for tc in ET.parse(path).getroot().iter("testcase"):
            failure = tc.find("failure")
            if failure is None:
                failure = tc.find("error")
            if failure is None:
                continue
            operation = tc.get("name", "")
            text = failure.text or failure.get("message") or ""
            # A testcase holds one numbered entry per distinct failing example.
            starts = [m.start() for m in _ENTRY.finditer(text)] or [0]
            for i, start in enumerate(starts):
                chunk = text[start:starts[i + 1] if i + 1 < len(starts) else None]
                case = self._case(path, operation, chunk, redact_keys)
                if case:
                    cases.append(case)
        return cases

    def _case(self, path: Path, operation: str, chunk: str,
              redact_keys: list[str]) -> Case | None:
        checks = []
        for m in _CHECK.finditer(chunk):
            title = m.group(2).strip()
            code = m.group(1) or _NAMES.get(title.lower(), "")
            checks.append((code, title))
        if not checks:
            return None
        found = [SCHEMATHESIS_CHECKS.get(code, ("schema_conformance", "medium"))
                 for code, _ in checks]
        oracles = sorted({o for o, _ in found})
        severity = max((s for _, s in found), key=RISK_ORDER.index)
        status = _STATUS.search(chunk)
        curl = _CURL.search(chunk)
        reproduce = redact_curl(curl.group(1), redact_keys) if curl else ""
        names = ", ".join(f"{c} {t}" if c else t for c, t in checks)
        message = f"{operation}: {names}" + (f" (received {status.group(1)})" if status else "")
        case_id = _ENTRY.search(chunk)
        key = ",".join(sorted(c or t for c, t in checks))
        return Case(
            id=f"schemathesis:{operation}:{case_id.group(1) if case_id else key}",
            origin=Origin.probe_finding,
            title=f"{operation} (Schemathesis)",
            message=redact_text(message, redact_keys),
            source=str(path),
            signature=_signature("schemathesis", operation, key),
            severity=severity,
            reproduce=reproduce,
            evidence=[Evidence(
                kind=EvidenceKind.probe_replay,
                summary=f"Schemathesis: {names} on {operation}"
                        + (f", status {status.group(1)}" if status else ""),
                excerpt=redact_curl(redact_text(chunk.strip(), redact_keys),
                                    redact_keys)[:3000],
                ref="schemathesis:" + ",".join(oracles))],
        )


class ZapReader:
    name = "zap"

    def can_read(self, path: Path) -> bool:
        if path.suffix.lower() != ".json" or not path.is_file():
            return False
        head = path.read_text(errors="ignore")[:500]
        return '"@programName"' in head and "ZAP" in head

    def read(self, path: Path, redact_keys: list[str]) -> list[Case]:
        data = json.loads(path.read_text(errors="ignore"))
        cases: list[Case] = []
        for site in data.get("site", []):
            host = site.get("@name", "")
            for alert in site.get("alerts", []):
                if str(alert.get("confidence", "")) == "0":  # marked false positive in ZAP
                    continue
                cases.append(self._case(path, host, alert, redact_keys))
        return cases

    def _case(self, path: Path, host: str, alert: dict, redact_keys: list[str]) -> Case:
        name = alert.get("alert") or alert.get("name") or "ZAP alert"
        risk = ZAP_RISK.get(str(alert.get("riskcode", "")), "low")
        confidence = str(alert.get("confidence", ""))
        instances = alert.get("instances", [])
        lines = []
        for inst in instances[:5]:
            bits = [f"{inst.get('method', 'GET')} {inst.get('uri', '')}"]
            for field in ("param", "attack", "evidence"):
                if inst.get(field):
                    bits.append(f"{field}: {inst[field]}")
            lines.append("  ".join(bits))
        if len(instances) > 5:
            lines.append(f"... and {len(instances) - 5} more")
        excerpt = "\n".join([
            f"Risk: {alert.get('riskdesc') or risk}",
            (f"CWE: {alert.get('cweid', '')}  WASC: {alert.get('wascid', '')}  "
             f"plugin: {alert.get('pluginid', '')}"),
            _text(alert.get("desc")), "", "Instances:", *lines, "",
            "Solution: " + _text(alert.get("solution")),
        ])
        first = instances[0] if instances else {}
        reproduce = ""
        if first.get("uri"):
            reproduce = f"{first.get('method', 'GET')} {first['uri']}"
            if first.get("param"):
                reproduce += f" (parameter {first['param']}"
                reproduce += f", value {first['attack']})" if first.get("attack") else ")"
        plugin = str(alert.get("alertRef") or alert.get("pluginid") or name)
        return Case(
            id=f"zap:{host}:{plugin}",
            origin=Origin.probe_finding,
            title=f"{name} (ZAP)",
            message=redact_text(f"{name} on {host} ({len(instances)} instance(s))",
                                redact_keys),
            source=str(path),
            signature=_signature("zap", host, plugin),
            severity=risk,
            reproduce=redact_text(reproduce, redact_keys),
            evidence=[Evidence(
                kind=EvidenceKind.probe_replay,
                summary=f"ZAP {risk} risk, confidence {confidence or '?'}: {name}",
                excerpt=redact_text(excerpt, redact_keys)[:3000],
                ref=f"zap:{plugin}:confidence={confidence}")],
        )


SCHEMATHESIS = SchemathesisReader()
ZAP = ZapReader()


def at_least(severity: str | None, minimum: str) -> bool:
    """True when `severity` is `minimum` or worse. Unknown severities pass."""
    if severity not in RISK_ORDER or minimum not in RISK_ORDER:
        return True
    return RISK_ORDER.index(severity) >= RISK_ORDER.index(minimum)
