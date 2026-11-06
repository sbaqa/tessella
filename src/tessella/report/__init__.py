"""`tessella report`: one self-contained HTML page built from Tessella's own local data.

Reads the case store (cases, decisions, run history) and the model audit log. Nothing is
fetched, nothing is sent: the page has no external scripts, fonts or images, so it can be
attached to a CI run, mailed, or opened from a file share.

`build` turns the store into a plain dict (easy to test, easy to dump as JSON later);
`render` turns that dict into HTML. Every number on the page comes from the dict, and the
page says how it was computed. The one estimate (hours saved) is built from assumptions the
reader can edit, and is labelled as an estimate.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path

from tessella.config import Settings
from tessella.memory.store import Store
from tessella import outcomes as outcomes_mod
from tessella.models import Action, Case, EvidenceKind
from tessella.report import timevalue

TEMPLATE = Path(__file__).parent / "template.html"

# Order is the colour order (see the CSS tokens); "none" means no verdict yet.
CATEGORIES = ["product_bug", "test_bug", "flaky", "environment", "test_data", "unknown", "none"]
LABELS = {
    "product_bug": "Product bug", "test_bug": "Test bug", "flaky": "Flaky",
    "environment": "Environment", "test_data": "Test data", "unknown": "Unknown",
    "none": "No verdict yet",
}
MIN_DECISIONS_FOR_TREND = 5
MAX_CASE_ROWS = 300  # the table is a lookup, not an export; the page stays light for big suites
VIEWS = ("full", "management")
# What the short page keeps. The rest is detail a manager does not need to open.
SUMMARY_PANELS = ("saved", "outcomes", "funnel", "trust", "runs", "models")
MIN_OUTCOMES_FOR_RATE = 5


# --------------------------------------------------------------------------- data


def _read_audit(path: Path) -> list[dict]:
    """Model-call records, oldest first. A missing file or a damaged line is skipped."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _models(records: list[dict], settings: Settings) -> dict:
    groups: dict[tuple, dict] = {}
    for r in records:
        key = (r.get("role", "?"), r.get("model", "?"), r.get("location", "?"), r.get("host", "?"))
        g = groups.setdefault(key, {"role": key[0], "model": key[1], "location": key[2],
                                    "host": key[3], "calls": 0, "chars": 0, "images": 0,
                                    "first": r.get("at"), "last": r.get("at")})
        g["calls"] += 1
        g["chars"] += int(r.get("chars") or 0)
        g["images"] += int(r.get("images") or 0)
        g["last"] = r.get("at") or g["last"]
    classes = Counter(c for r in records for c in r.get("data_classes", []))
    cloud = [r for r in records if r.get("location") == "cloud"]
    policy = settings.data_policy
    return {
        "calls": len(records),
        "cloud_calls": len(cloud),
        "local_calls": len(records) - len(cloud),
        "human_approved": sum(1 for r in records if r.get("approved_by_human")),
        "images": sum(int(r.get("images") or 0) for r in records),
        "chars": sum(int(r.get("chars") or 0) for r in records),
        "hosts": sorted({r.get("host", "?") for r in records}),
        "data_classes": dict(classes.most_common()),
        "groups": sorted(groups.values(), key=lambda g: -g["calls"]),
        "first": records[0].get("at") if records else None,
        "last": records[-1].get("at") if records else None,
        "policy": {"may_leave": [c for c in policy.may_leave if policy.allows(c)],
                   "never_leaves": list(policy.never_leaves)},
    }


def _tile_size(failures: int) -> int:
    """Pixels per tile in the mosaic: big when there are few failures, small for many."""
    for limit, size in ((6, 80), (12, 56), (60, 32), (300, 20)):
        if failures <= limit:
            return size
    return 12


def _outcome_summary(store: Store) -> dict:
    """What Jira said about each filed ticket the last time `tessella sync` ran."""
    filed = store.filed_tickets()
    rows = [o for o in store.outcomes() if o["key"] in {t["key"] for t in filed}]
    counts = Counter(o["outcome"] for o in rows)
    closed = sum(counts[k] for k in ("fixed", "not_a_bug", "duplicate", "other"))
    days = []
    for o in rows:
        if o["outcome"] != "fixed" or not (o["created"] and o["resolved"]):
            continue
        try:
            span = datetime.fromisoformat(o["resolved"]) - datetime.fromisoformat(o["created"])
        except ValueError:
            continue
        days.append(max(span.total_seconds() / 86400, 0.0))
    days.sort()
    return {
        "filed": len(filed), "checked": len(rows), "counts": dict(counts), "closed": closed,
        "fixed_rate": round(counts["fixed"] / closed, 3) if closed else None,
        "enough": closed >= MIN_OUTCOMES_FOR_RATE,
        "median_fix_days": round(days[len(days) // 2], 1) if days else None,
        "last_checked": max((o["checked_at"] for o in rows), default=None),
        "by_key": {o["key"]: o for o in rows},
    }


def _compare(runs: list[dict], titles: dict[str, str], states: dict[str, str]) -> dict:
    """The latest run against the one before it. 'Not seen' is not 'fixed': a cluster also
    disappears when its tests were not part of that run, and the page says so."""
    if len(runs) < 2:
        return {"has": False, "runs": len(runs)}
    last, prev = runs[-1], runs[-2]
    earlier = {sig for r in runs[:-2] for sig in r["detail"]}
    now_, before = set(last["detail"]), set(prev["detail"])

    def rows(sigs, source):
        return [{"signature": sig, "title": titles.get(sig, sig[:8]), "failures": source[sig],
                 "state": states.get(sig, "open")}
                for sig in sorted(sigs, key=lambda x: (-source[x], x))]

    return {
        "has": True, "runs": len(runs), "at": last["at"], "prev_at": prev["at"],
        "failures": last["failures"], "prev_failures": prev["failures"],
        "new": rows(now_ - before - earlier, last["detail"]),
        "returned": rows((now_ - before) & earlier, last["detail"]),
        "recurring": rows(now_ & before, last["detail"]),
        "gone": rows(before - now_, prev["detail"]),
    }


def _probe_summary(store: Store) -> dict:
    plans = store.probes()
    kinds: Counter = Counter()
    for pl in plans:
        kinds.update(pl["kinds"])
    return {"plans": len(plans), "flows": len({pl["flow"] for pl in plans}),
            "steps": sum(pl["steps"] for pl in plans), "linked": sum(pl["linked"] for pl in plans),
            "mutations": sum(pl["mutations"] for pl in plans), "kinds": dict(kinds.most_common()),
            "last": plans[-1]["at"] if plans else None}


def _details(clusters: list[dict], runs: list[dict], review_s: dict[str, float],
             by_key: dict[str, dict]) -> dict[str, dict]:
    """What the tile drawer shows for each failure. Trimmed: the page is a summary, and the
    full text stays in the store."""
    seen = Counter(sig for r in runs for sig in r["detail"])
    out: dict[str, dict] = {}
    for c in clusters:
        lead, v = c["lead"], c["lead"].verdict
        cited = set(v.evidence_used) if v else set()
        outcome = by_key.get(c["ticket_key"] or "")
        for m in c["members"]:
            if len(out) >= MAX_CASE_ROWS:
                return out
            out[m.id] = {
                "title": m.title, "message": m.message[:500],
                "stack": "\n".join(m.stack.splitlines()[:8]),
                "verdict": LABELS[c["category"]], "confidence": c["confidence"],
                "root_cause": v.root_cause if v else "",
                "actual": v.actual_result if v else "", "expected": v.expected_result if v else "",
                "fix": v.suggested_fix if v else "",
                "evidence": [{"kind": e.kind.value, "summary": e.summary[:200], "ref": e.ref or "",
                              "cited": i in cited}
                             for i, e in enumerate(lead.evidence[:8])],
                "state": c["state"], "reason": c["reason"], "by": c["by"],
                "decided": _when(c["decided_at"]) if c["decided_at"] else "",
                "review_s": review_s.get(c["signature"]),
                "ticket": c["ticket_key"], "url": _safe_url(c["ticket_url"]),
                "outcome": (outcomes_mod.LABELS[outcome["outcome"]] + (
                    f" ({outcome['resolution']})" if outcome.get("resolution") else ""))
                if outcome else None,
                "cluster": c["signature"][:8], "size": len(c["tests"]),
                "runs": seen.get(c["signature"], 0),
                # The ticket approving would file, so the live board can show it before filing.
                "draft": {"title": lead.draft.title, "description": lead.draft.description,
                          "steps": lead.draft.steps_to_reproduce,
                          "actual": lead.draft.actual_result,
                          "expected": lead.draft.expected_result,
                          "environment": lead.draft.environment,
                          "root_cause": lead.draft.root_cause,
                          "fix": lead.draft.suggested_fix,
                          "screenshots": len(lead.draft.images),
                          "duplicates": lead.draft.possible_duplicates}
                if lead.draft and m is lead else None,
            }
    return out


def _board(runs: list[dict], by_sig: dict[str, list[Case]], decisions: dict) -> dict:
    """Every stored cluster as the board sees it, decisions made since included: already
    filed, probably filed, a new draft, or a person's call. The latest run's clusters come
    first and carry the headline; older ones stay listed, marked as not in that run."""
    from types import SimpleNamespace

    from tessella import board

    latest = [sig for sig in (runs[-1]["detail"] if runs else {}) if sig in by_sig]
    older = [sig for sig in by_sig if sig not in latest]
    leads = {sig: next((m for m in by_sig[sig] if m.verdict), by_sig[sig][0])
             for sig in latest + older}

    def view(sigs):
        return SimpleNamespace(clusters={sig: [leads[sig]] for sig in sigs}, jira_matches={},
                               decisions={sig: decisions[sig] for sig in sigs if sig in decisions})

    status = {}
    for sigs, in_latest in ((latest, True), (older, False)):
        v = view(sigs)
        for r in board.classify(v):
            lead, d = leads[r.signature], v.decisions.get(r.signature)
            if r.status == board.Status.on_board and d is not None:
                links = [{"key": d.ticket_key, "url": d.ticket_url}]
            else:
                links = [{"key": m["key"], "url": m.get("url")}
                         for m in (lead.draft.jira_matches if lead.draft else [])]
            status[r.signature] = {"status": r.status.value, "note": r.note, "links": links,
                                   "latest": in_latest}
    counts = Counter(status[sig]["status"] for sig in latest)
    return {"has": bool(latest), "at": runs[-1]["at"] if runs else None,
            "headline": board.headline(view(latest)) if latest else "",
            "failures": sum(runs[-1]["detail"].get(sig, 1) for sig in latest) if latest else 0,
            "counts": dict(counts), "latest": latest, "status": status}


def build(settings: Settings, store: Store, now: datetime | None = None,
          project: str | None = None) -> dict:
    now = now or datetime.now()
    cases = store.all_cases()
    decisions: dict[str, object] = {}
    for sig, d in store.all_decisions():
        decisions[sig] = d  # latest wins
    runs = store.runs()

    by_sig: dict[str, list[Case]] = {}
    for c in cases:
        by_sig.setdefault(c.signature, []).append(c)

    def state_of(sig: str) -> str:
        d = decisions.get(sig)
        if d is None:
            return "open"
        return "rejected" if d.action == Action.reject else "approved"

    clusters = []
    for sig, members in by_sig.items():
        lead = next((m for m in members if m.verdict), members[0])
        v = lead.verdict
        d = decisions.get(sig)
        clusters.append({
            "signature": sig,
            "category": v.category.value if v else "none",
            "confidence": v.confidence if v else None,
            "state": state_of(sig),
            "has_draft": bool(lead.draft),
            "ticket_key": getattr(d, "ticket_key", None),
            "ticket_url": getattr(d, "ticket_url", None),
            "reason": d.reason.value if d is not None and d.reason else None,
            "decided_at": d.at.isoformat() if d is not None else None,
            "root_cause": v.root_cause if v else "",
            "tests": [{"id": m.id, "title": m.title,
                       "screenshots": len(m.screenshots), "evidence": len(m.evidence)}
                      for m in members],
            "by": d.by if d is not None else None,
            "members": members,
            "lead": lead,
        })
    clusters.sort(key=lambda c: (-len(c["tests"]), c["signature"]))

    judged = [c for c in clusters if c["category"] != "none"]
    drafted = [c for c in clusters if c["has_draft"]]
    decided = [c for c in clusters if c["state"] != "open"]
    approved = [c for c in decided if c["state"] == "approved"]
    rejected = [c for c in decided if c["state"] == "rejected"]

    # Reviewer agreement, overall and per category. Only decisions count: an open cluster is
    # not a vote either way.
    per_cat = {}
    for cat in CATEGORIES:
        a = sum(1 for c in approved if c["category"] == cat)
        r = sum(1 for c in rejected if c["category"] == cat)
        if a or r:
            per_cat[cat] = {"approved": a, "rejected": r}

    def share(pred) -> float | None:
        return round(sum(1 for c in judged if pred(c["lead"])) / len(judged), 3) if judged else None

    evidence = {
        "judged": len(judged),
        "avg_items": round(sum(len(c["lead"].evidence) for c in judged) / len(judged), 1)
        if judged else None,
        "with_screenshot": share(lambda case: bool(case.screenshots)),
        "with_rerun": share(lambda case: any(e.kind == EvidenceKind.rerun for e in case.evidence)),
        "citing_evidence": share(lambda case: bool(case.verdict and case.verdict.evidence_used)),
    }

    # Run history: which clusters showed up in more than one run.
    seen = Counter(sig for r in runs for sig in r["detail"])
    titles = {c["signature"]: c["tests"][0]["title"] for c in clusters}
    recurring = [{"signature": sig, "runs": n, "title": titles.get(sig, "")}
                 for sig, n in seen.most_common() if n >= 2]

    timeline = store.review_timeline()
    review_s = {row["signature"]: sec for row, sec in
                zip(timeline, timevalue.review_intervals(timeline)) if sec is not None}
    outcome = _outcome_summary(store)
    states = {c["signature"]: c["state"] for c in clusters}
    time = timevalue.compute(
        clusters=[{"state": c["state"], "has_draft": c["has_draft"], "category": c["category"]}
                  for c in clusters],
        recalled=sum(r["recalled"] for r in runs),
        timeline=timeline, samples=store.manual_samples(), runs=runs,
        duplicates=sum(1 for _, d in store.all_decisions() if d.linked_existing))

    n_fail = len(cases)
    board_view = _board(runs, by_sig, decisions)
    details = _details(clusters, runs, review_s, outcome["by_key"])
    for c in clusters:  # the drawer says what to do, and offers the matching decision first
        st = board_view["status"].get(c["signature"], {})
        for m in c["members"]:
            if m.id in details:
                details[m.id].update(todo=st.get("note", ""), category=c["category"],
                                     lead=c["lead"].id)
    return {
        "board": board_view,
        "generated_at": now.isoformat(timespec="seconds"),
        "project": project or Path.cwd().name,
        "source": settings.storage.path,
        "totals": {
            "failures": n_fail, "clusters": len(clusters), "judged": len(judged),
            "drafted": len(drafted), "decided": len(decided), "approved": len(approved),
            "rejected": len(rejected), "open": len(clusters) - len(decided),
            "filed": sum(1 for c in approved if c["ticket_key"]),
            "drafted_reviewed": sum(1 for c in drafted if c["state"] != "open"),
            "drafted_approved": sum(1 for c in drafted if c["state"] == "approved"),
            "drafted_filed": sum(1 for c in drafted if c["state"] == "approved" and c["ticket_key"]),
        },
        "categories": {cat: sum(1 for c in clusters if c["category"] == cat) for cat in CATEGORIES
                       if any(c["category"] == cat for c in clusters)},
        "trust": {"per_category": per_cat,
                  "reasons": dict(Counter(c["reason"] for c in rejected if c["reason"])),
                  "enough": len(decided) >= MIN_DECISIONS_FOR_TREND},
        "evidence": evidence,
        "runs": runs,
        "recurring": recurring,
        "models": _models(_read_audit(Path(settings.data_policy.audit_log)), settings),
        "time": time,
        "outcomes": {k: v for k, v in outcome.items() if k != "by_key"},
        "compare": _compare(runs, titles, states),
        "probe": _probe_summary(store),
        "details": details,
        "tile_size": _tile_size(n_fail),
        "clusters": clusters,
    }


# --------------------------------------------------------------------------- html

GLYPHS = {"product_bug": "P", "test_bug": "T", "flaky": "F", "environment": "E",
          "test_data": "D", "unknown": "?", "none": "-"}
STATES = {"open": "Waiting", "approved": "Approved", "rejected": "Rejected"}
# Panels in their default order: (id, title). The page lets the reader hide and reorder them.
PANELS = [
    ("cases", "Failures and what to do"),
    ("mosaic", "Every failure, as a tile"),
    ("compare", "Since the last run"),
    ("saved", "Time saved"),
    ("funnel", "From failures to tickets"),
    ("trust", "Do reviewers agree?"),
    ("outcomes", "What happened to the tickets"),
    ("runs", "Runs"),
    ("evidence", "How well verdicts are backed"),
    ("models", "Models and data"),
    ("probe", "Probe"),
]
# The full page in three tabs, so each question has one place: what this run means, what
# Tessella is worth, and how it is set up. (id, label, panels)
TABS = [
    ("run", "This run", ("cases", "mosaic", "compare")),
    ("value", "Value", ("saved", "funnel", "trust", "outcomes")),
    ("setup", "Setup", ("runs", "evidence", "models", "probe")),
]
MAX_FUNNEL_TILES = 48


def _e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _hours(minutes: float) -> str:
    """'45 min' or '2.3 hours'; a loss keeps its minus sign."""
    sign = "\u2212" if minutes < -0.5 else ""
    m = abs(minutes)
    return f"{sign}{m:.0f} min" if m < 60 else f"{sign}{m / 60:.1f} hours"


def _mins(minutes: float) -> str:
    sign = "\u2212" if minutes < -0.05 else ""
    m = abs(minutes)
    return f"{sign}{m:.0f}" if m >= 10 else f"{sign}{m:.1f}"


def _when(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        when = datetime.fromisoformat(iso)
        if when.tzinfo:  # the audit log is written in UTC; show it in local time like the rest
            when = when.astimezone().replace(tzinfo=None)
        return when.strftime("%d %b %Y, %H:%M")
    except ValueError:
        return str(iso)


def _safe_url(url: str | None) -> str | None:
    return url if url and url.lower().startswith(("http://", "https://")) else None


def _swatch(cat: str) -> str:
    return f'<span class="sw cat-{_e(cat)}" aria-hidden="true"></span>'


def _panel(pid: str, lede: str, body: str, take: str = "") -> str:
    title = dict(PANELS)[pid]
    tab = next((t for t, _, pids in TABS if pid in pids), "run")
    return (
        f'<section class="panel p-{pid}" data-panel="{pid}" data-tab="{tab}" aria-labelledby="h-{pid}">'
        f'<div class="ph"><span class="grip" aria-hidden="true" title="Drag to move"></span>'
        f'<h2 id="h-{pid}">{_e(title)}</h2>'
        f'<button type="button" class="pm" data-menu="{pid}" aria-haspopup="menu" '
        f'aria-label="Options for {_e(title)}">&#8943;</button></div>'
        f'<div class="pb">'
        + (f'<p class="take">{take}</p>' if take else "")
        + (f'<p class="lede">{lede}</p>' if lede else "")
        + f"{body}</div></section>")


def _headline(t: dict, time: dict) -> str:
    if not t["failures"]:
        return ("No failures are stored yet. Run <code>tessella triage</code> on a test report, "
                "then build this page again.")
    parts = [f"{_plural(t['failures'], 'failure')} became {_plural(t['clusters'], 'cluster')}."]
    if t["drafted"]:
        parts.append(f"{_plural(t['drafted'], 'draft')} for a person to review.")
    if t["decided"]:
        parts.append(f"{t['approved']} approved, {t['rejected']} rejected.")
    if t["filed"]:
        parts.append(f"{_plural(t['filed'], 'ticket')} filed.")
    if t["open"]:
        parts.append(f"{t['open']} still waiting in the queue.")
    if time["has_data"]:
        parts.append(f'Net time saved: <span id="h-net">{_hours(time["net_min"])}</span>'
                     f"{' (estimate)' if time['assumed'] else ''}.")
    return " ".join(parts)


def _tile_counts(data: dict) -> tuple[Counter, Counter]:
    """Failures per verdict and per review state, for the filter chips."""
    by_cat: Counter = Counter()
    by_state: Counter = Counter()
    for c in data["clusters"]:
        by_cat[c["category"]] += len(c["tests"])
        by_state[c["state"]] += len(c["tests"])
    return by_cat, by_state


def _mosaic(data: dict) -> str:
    size = data["tile_size"]
    if not data["clusters"]:
        return '<p class="empty">Every failure appears here as one tile, grouped by cluster.</p>'
    by_cat, by_state = _tile_counts(data)
    chips = "".join(
        f'<button type="button" class="chip" data-filter="cat" data-value="{cat}" '
        f'aria-pressed="false">{_swatch(cat)}{_e(LABELS[cat])} <b>{by_cat[cat]}</b></button>'
        for cat in CATEGORIES if by_cat[cat])
    chips += '<span class="chip-gap"></span>' + "".join(
        f'<button type="button" class="chip" data-filter="state" data-value="{st}" '
        f'aria-pressed="false"><span class="sw st-{st}" aria-hidden="true"></span>'
        f'{_e(label)} <b>{by_state[st]}</b></button>'
        for st, label in STATES.items() if by_state[st])

    out = [f'<div class="chips" role="group" aria-label="Show only">{chips}</div>',
           f'<div class="mosaic" id="mosaic" data-auto="{size}" style="--tile:{size}px"'
           f'{" data-small" if size < 28 else ""}>']
    for c in data["clusters"]:
        n = len(c["tests"])
        cols = max(1, math.ceil(math.sqrt(n)))
        conf = f", {c['confidence']:.0%} sure" if c["confidence"] is not None else ""
        status = {"approved": "approved" + (f" ({c['ticket_key']})" if c["ticket_key"] else ""),
                  "rejected": "rejected" + (f" as {c['reason']}" if c["reason"] else ""),
                  "open": "waiting for review"}[c["state"]]
        out.append(f'<div class="cluster" style="--cols:{cols}"><div class="tiles">')
        for t in c["tests"]:
            tip = f"{t['title']}: {LABELS[c['category']].lower()}{conf}, {status}"
            out.append(
                f'<a class="tile cat-{_e(c["category"])} is-{c["state"]}" '
                f'href="#case-{_e(t["id"])}" data-case="{_e(t["id"])}" data-tip="{_e(tip)}" '
                f'aria-label="{_e(tip)}" '
                f'data-cat="{_e(c["category"])}" data-state="{c["state"]}" '
                f'data-cluster="{_e(c["signature"][:8])}" data-label="{_e(LABELS[c["category"]])}" '
                f'data-glyph="{GLYPHS[c["category"]]}"></a>')
        out.append(f'</div><p class="cap">{_e(LABELS[c["category"]])}<br>'
                   f'<span>{_plural(n, "test")}</span></p></div>')
    out.append("</div>")
    return "".join(out)


def _funnel(t: dict) -> tuple[str, str]:
    """Rows of tiles, one row per stage. Each tile stands for `unit` failures or clusters,
    so the rows visibly shrink as grouping and review remove work."""
    stages = [
        ("Failures", t["failures"]), ("Clusters", t["clusters"]), ("Drafts", t["drafted"]),
        ("Approved", t["drafted_approved"]), ("Tickets filed", t["drafted_filed"]),
    ]
    top = max((v for _, v in stages), default=0)
    unit = max(1, math.ceil(top / MAX_FUNNEL_TILES))
    rows = "".join(
        f'<li><span class="lbl">{_e(name)}</span><b>{v}</b>'
        f'<span class="units" aria-hidden="true">{"<i></i>" * (math.ceil(v / unit) if v else 0)}</span></li>'
        for name, v in stages)
    notes = ""
    if unit > 1:
        notes += f'<p class="note">Each tile stands for {unit}.</p>'
    if t["decided"] > t["drafted_reviewed"]:
        extra = t["decided"] - t["drafted_reviewed"]
        notes += (f'<p class="note">{_plural(extra, "more cluster")} '
                  f'{"was" if extra == 1 else "were"} decided without a draft, so '
                  f'{"it is" if extra == 1 else "they are"} not counted in the last three steps.</p>')
    if not t["failures"]:
        take = ""
    elif t["clusters"] == t["failures"]:
        take = "Every failure was its own cluster, so grouping saved nothing here."
    else:
        take = (f"A person reads {_plural(t['clusters'], 'cluster')} instead of "
                f"{_plural(t['failures'], 'failure')}.")
    return f'<ol class="rows funnel">{rows}</ol>{notes}', take


def _basis(kind: str, detail: str = "") -> str:
    """A label that says where a number came from. Text, not just colour."""
    names = {"measured": "Measured", "calibrated": "Calibrated", "assumed": "Assumed",
             "partly": "Partly measured"}
    tail = f' <span class="why">{_e(detail)}</span>' if detail else ""
    return f'<span class="basis b-{kind}">{names[kind]}</span>{tail}'


def _saved(data: dict) -> str:
    tv = data["time"]
    b, c, a, r = tv["baseline"], tv["counts"], tv["avoided"], tv["review"]
    if not tv["has_data"]:
        return ('<p class="empty">Nothing to count yet. Time saved comes from reviewed drafts and '
                "repeat failures. Review a draft in <code>tessella queue</code> and it starts here.</p>")

    def small(n: int) -> str:
        return " (small sample)" if 0 < n < timevalue.SMALL_SAMPLE else ""

    t_basis = (_basis("calibrated", f"median of {b['n_ticket']} timed{small(b['n_ticket'])}")
               if b["ticket_src"] == "calibrated" else _basis("assumed"))
    o_basis = (_basis("calibrated", f"median of {b['n_other']} timed{small(b['n_other'])}")
               if b["other_src"] == "calibrated" else _basis("assumed"))
    if r["src"] == "measured":
        r_basis = _basis("measured", f"median {r['median_s'] / 60:.1f} min each")
    elif r["src"] == "partly measured":
        r_basis = _basis("partly", f"{r['untimed']} untimed, counted at the median "
                                   f"{r['each_min']:.1f} min")
    else:
        r_basis = _basis("assumed")

    def line(label: str, count: int, rate: str, minutes: float, basis: str) -> str:
        rate_min = {"ticket": b["ticket_min"], "other": b["other_min"]}[rate]
        return (f'<tr data-count="{count}" data-rate="{rate}" data-kind="avoid">'
                f'<td>{label} \u00d7 <span data-rate-text="{rate}">{_mins(rate_min)}</span> min</td>'
                f'<td class="num" data-cell>{_mins(minutes)}</td><td>{basis}</td></tr>')

    avoid_rows = ""
    if c["approved_ticket"]:
        avoid_rows += line(f"{_plural(c['approved_ticket'], 'approved bug draft')}",
                           c["approved_ticket"], "ticket", a["ticket_min"], t_basis)
    if c["approved_other"]:
        avoid_rows += line(f"{_plural(c['approved_other'], 'approved cluster')} without a ticket",
                           c["approved_other"], "other", a["other_min"], o_basis)
    if c["recalled"]:
        avoid_rows += line(f"{_plural(c['recalled'], 'repeat failure')} not investigated again",
                           c["recalled"], "other", a["repeat_min"], o_basis)
    if not avoid_rows:
        avoid_rows = '<tr><td colspan="3" class="dim">Nothing finished yet.</td></tr>'

    if r["n"]:
        untimed_text = f", {r['untimed']} estimated" if r["untimed"] else ""
        review_row = (
            f'<tr><td>{_plural(r["n"], "review")}: {r["timed"]} timed{untimed_text}</td>'
            f'<td class="num" data-cell-review>\u2212{_mins(r["total_min"])}</td>'
            f'<td>{r_basis}</td></tr>')
    else:
        review_row = '<tr><td colspan="3" class="dim">No reviews recorded yet.</td></tr>'

    # Only the assumed inputs can be edited; measured and calibrated ones are what they are.
    fields = ""
    if b["ticket_src"] == "assumed" and c["approved_ticket"]:
        fields += (f'<label>Minutes to investigate and write up a ticket by hand'
                   f'<input type="number" min="0" step="1" inputmode="numeric" '
                   f'data-assume="ticket" value="{b["ticket_min"]:g}"></label>')
    if b["other_src"] == "assumed" and (c["approved_other"] or c["recalled"]):
        fields += (f'<label>Minutes to investigate a failure by hand'
                   f'<input type="number" min="0" step="1" inputmode="numeric" '
                   f'data-assume="other" value="{b["other_min"]:g}"></label>')
    if r["src"] == "assumed":
        fields += (f'<label>Minutes to review one draft'
                   f'<input type="number" min="0" step="0.5" inputmode="numeric" '
                   f'data-assume="review" value="{r["each_min"]:g}"></label>')
    adjust = (f'<h3>Adjust the assumptions</h3><div class="fields">{fields}</div>' if fields else "")

    if tv["assumed"]:
        badge = f'<p class="badge">Still assumed: {_e(", ".join(tv["assumed"]))}.</p>'
    else:
        badge = '<p class="badge good">Every input is measured or timed by your team.</p>'

    notes = []
    comp = tv["compute"]
    if comp["timed_runs"]:
        share = (f", {comp['model_seconds'] / comp['seconds']:.0%} of it waiting on models"
                 if comp["seconds"] and comp["model_seconds"] else "")
        notes.append(f"Tessella ran for {_hours(comp['seconds'] / 60)} across "
                     f"{_plural(comp['timed_runs'], 'run')}{share}. That is not subtracted: nobody "
                     "waits on it.")
    else:
        notes.append("Run time is recorded from the next triage.")
    if tv["duplicates_avoided"]:
        notes.append(f"{_plural(tv['duplicates_avoided'], 'duplicate ticket')} avoided by linking to "
                     "an existing one. Real, but not given a price, so not in the hours above.")
    if c["rejected"]:
        notes.append(f"{_plural(c['rejected'], 'rejected draft')} earned no credit; reviewing "
                     f"{'it' if c['rejected'] == 1 else 'them'} took {_mins(r['rejected_min'])} min, "
                     "which is already in the review time.")
    if c["open"]:
        notes.append(f"{_plural(c['open'], 'cluster')} still open earn nothing until someone "
                     "reviews them.")

    ctas = []
    if b["ticket_src"] == "assumed" and c["approved_ticket"] or (
            b["other_src"] == "assumed" and (c["approved_other"] or c["recalled"])):
        ctas.append("Replace the assumed time with your team's own: run <code>tessella calibrate</code> "
                    "on about five failures. It starts a stopwatch on a raw failure and stores the time.")
    if r["src"] in ("assumed", "partly measured", "none"):
        ctas.append("Review times are recorded when you review drafts in <code>tessella queue</code>.")
    cta = "".join(f'<p class="cta">{x}</p>' for x in ctas)

    how = (
        "<details><summary>How this is counted</summary>"
        "<p>Net = manual work avoided minus time spent reviewing.</p>"
        "<p>Manual work avoided counts only finished work that Tessella did: approved clusters it "
        "had a verdict for, and repeats of a failure that an earlier decision already covered. A "
        "rejected draft earns nothing, because the person did that work themselves. An open draft "
        "earns nothing until it is reviewed.</p>"
        f"<p>Review time runs from when a card was first shown, or from your previous decision if "
        f"that was later, up to {r['cap_min']:.0f} minutes each so idle time is not counted. "
        "Reviews made before timing existed are counted at the median of the timed ones.</p>"
        "<p>Manual times come from <code>tessella calibrate</code>: the median of investigations "
        "your team timed. Until there are some, defaults are used and marked Assumed.</p></details>")

    attrs = (f'data-ticket="{b["ticket_min"]}" data-other="{b["other_min"]}" '
             f'data-timed-total="{r["timed_total_min"]:.4f}" data-untimed="{r["untimed"]}" '
             f'data-review-each="{r["each_min"]:.4f}"')
    total_row = (f'<tr class="net"><td>Net time saved</td><td class="num" id="l-net">'
                 f'{_mins(tv["net_min"])}</td><td></td></tr>')
    return (
        f'<div id="saved" {attrs}>'
        '<div class="split2 saved-split"><div>'
        f'<p class="big"><span id="saved-out">{_e(_hours(tv["net_min"]))}</span></p>'
        '<p class="sub">net, after the time spent reviewing</p>'
        f"{badge}</div><div>"
        '<div class="scroll"><table class="ledger"><tbody>'
        f'<tr class="head"><td>Manual work avoided</td><td class="num" id="l-avoid">'
        f'{_mins(a["total_min"])}</td><td></td></tr>{avoid_rows}'
        f'<tr class="head"><td>Review time spent</td><td class="num" id="l-review">'
        f'\u2212{_mins(r["total_min"])}</td><td></td></tr>{review_row}'
        f"{total_row}</tbody></table></div>"
        '<p class="note">Minutes. Each line says where its number came from.</p>'
        f"</div></div>{adjust}{cta}"
        '<ul class="notes">' + "".join(f"<li>{x}</li>" for x in notes) + "</ul>"
        f"{how}</div>")


def _outcomes(data: dict) -> tuple[str, str]:
    o = data["outcomes"]
    if not o["filed"]:
        return ('<p class="empty">No ticket filed yet. Approve a draft in <code>tessella queue</code>, '
                "then run <code>tessella sync</code> later to see whether the ticket was fixed or "
                "closed as not a bug.</p>"), ""
    if not o["checked"]:
        return (f'<p class="empty">{_plural(o["filed"], "ticket")} filed, none checked yet. Run '
                "<code>tessella sync</code> to read their status from Jira. It only reads; it never "
                "changes a ticket.</p>"), ""
    c = o["counts"]
    if o["closed"]:
        head = (f'<p class="big">{c.get("fixed", 0)} of {o["closed"]}</p>'
                '<p class="sub">closed tickets were fixed</p>')
    else:
        head = (f'<p class="big">{c.get("open", 0)}</p>'
                '<p class="sub">tickets still open, none closed yet</p>')
    top = max(c.values()) or 1
    meters = "".join(
        f'<li><span class="lbl">{_e(outcomes_mod.LABELS[k])}</span><b>{c[k]}</b>'
        f'<span class="bar"><i style="width:{c[k] / top * 100:.1f}%"></i></span></li>'
        for k in outcomes_mod.OUTCOMES if c.get(k))
    notes = []
    if o["closed"] and not o["enough"]:
        notes.append(f"Fewer than {MIN_OUTCOMES_FOR_RATE} closed tickets so far, too few to call a rate.")
    if o["median_fix_days"] is not None:
        notes.append(f"Fixed tickets took {o['median_fix_days']:g} days from filing to resolution (median).")
    if o["filed"] > o["checked"]:
        notes.append(f"{_plural(o['filed'] - o['checked'], 'ticket')} not checked yet; run "
                     "<code>tessella sync</code> again.")
    notes.append(f"Last checked {_e(_when(o['last_checked']))}. \"Fixed\" means the board closed it "
                 "as done or resolved; a resolution the page does not recognise shows as "
                 "\"reason unclear\" instead of being guessed.")
    return (head + f'<ul class="rows meters">{meters}</ul>'
            + "".join(f'<p class="note">{n}</p>' for n in notes)), ""


def _compare_html(data: dict) -> tuple[str, str]:
    cmp_ = data["compare"]
    if not cmp_["has"]:
        return ('<p class="empty">This needs two runs. Run <code>tessella triage</code> again and it '
                "shows what is new, what came back and what was not seen.</p>"), ""
    delta = cmp_["failures"] - cmp_["prev_failures"]
    sign = "\u2212" if delta < 0 else "+"
    sub = (f'<p class="sub">{_plural(cmp_["prev_failures"], "failure")} in the previous run, '
           f'{cmp_["failures"]} in the latest ({sign}{abs(delta)}).</p>')
    groups = (("new", "New"), ("returned", "Came back"), ("recurring", "Still failing"),
              ("gone", "Not seen this run"))
    rows = "".join(f'<li><span class="lbl">{label}</span><b>{len(cmp_[k])}</b></li>'
                   for k, label in groups)
    lists = ""
    for k, label in groups[:2] + groups[3:]:
        if cmp_[k]:
            items = "".join(
                f'<li><b>{_plural(r["failures"], "test")}</b> <span>{_e(r["title"])}</span></li>'
                for r in cmp_[k][:4])
            more = len(cmp_[k]) - 4
            lists += (f"<h3>{label}</h3><ul class=\"back\">{items}</ul>"
                      + (f'<p class="note">and {more} more</p>' if more > 0 else ""))
    note = ('<p class="note">Not seen is not the same as fixed: a cluster also disappears when '
            "its tests were not part of that run.</p>")
    take = (f"{len(cmp_['new'])} new, {len(cmp_['returned'])} came back, "
            f"{len(cmp_['gone'])} not seen since the last run.")
    return f'{sub}<ul class="rows cmp">{rows}</ul>{lists}{note}', _e(take)


def _probe_html(data: dict) -> tuple[str, str]:
    pr = data["probe"]
    if not pr["plans"]:
        return ('<p class="empty">No probe has run. <code>tessella probe trace.zip</code> reads a '
                "recorded flow and plans the changes it would try. To find bugs, run Schemathesis or "
                "OWASP ZAP and pass their reports to <code>tessella triage</code>: their findings "
                "appear with the other failures.</p>"), ""
    kinds = "".join(f'<li><span class="lbl">{_e(k)}</span><b>{v}</b></li>' for k, v in pr["kinds"].items())
    take = f"{_plural(pr['mutations'], 'change')} planned across {_plural(pr['flows'], 'flow')}."
    return (f'<p class="sub">{_plural(pr["steps"], "step")} read, {_plural(pr["linked"], "linked ID")} '
            f"found, last planned {_e(_when(pr['last']))}.</p>"
            f'<ul class="rows cmp">{kinds}</ul>'
            '<p class="note">Planned only: Tessella sends nothing to your system. Findings come from '
            "Schemathesis and ZAP reports passed to <code>tessella triage</code>.</p>"), _e(take)


def _trust(data: dict) -> str:
    tr, t = data["trust"], data["totals"]
    if not t["decided"]:
        return ('<p class="empty">No reviews yet. Approve or reject a draft in the queue and '
                "the result shows up here.</p>")
    pct = round(t["approved"] / t["decided"] * 100)
    out = [f'<p class="big">{pct}%</p>'
           f'<p class="sub">{t["approved"]} of {_plural(t["decided"], "reviewed cluster")} approved</p>']
    top = max(max(v["approved"] + v["rejected"] for v in tr["per_category"].values()), 1)
    out.append('<ul class="rows split">')
    for cat, v in tr["per_category"].items():
        out.append(
            f'<li><span class="lbl">{_swatch(cat)}{_e(LABELS[cat])}</span>'
            f'<b>{v["approved"]} / {v["rejected"]}</b>'
            '<span class="bar">'
            f'<i class="ok" style="width:{v["approved"] / top * 100:.1f}%"></i>'
            f'<i class="no" style="width:{v["rejected"] / top * 100:.1f}%"></i></span></li>')
    out.append("</ul>")
    out.append('<p class="key"><span class="sw ok"></span>approved '
               '<span class="sw no"></span>rejected</p>')
    if tr["reasons"]:
        why = ", ".join(f"{_e(k.replace('_', ' '))} {v}" for k, v in tr["reasons"].items())
        out.append(f'<p class="note">Rejected because: {why}.</p>')
    if not tr["enough"]:
        out.append(f'<p class="note">Fewer than {MIN_DECISIONS_FOR_TREND} reviews so far, '
                   "too few to read a trend. The percentage firms up as reviews accumulate.</p>")
    return "".join(out)


def _evidence(data: dict) -> str:
    ev = data["evidence"]
    if not ev["judged"]:
        return '<p class="empty">Evidence stats appear after the first judged cluster.</p>'

    def meter(label: str, value: float | None) -> str:
        v = value or 0
        return (f'<li><span class="lbl">{_e(label)}</span><b>{v:.0%}</b>'
                f'<span class="bar"><i style="width:{v * 100:.1f}%"></i></span></li>')

    return (
        f'<p class="sub">{ev["avg_items"]} pieces of evidence per judged cluster on average</p>'
        '<ul class="rows meters">'
        + meter("Verdict cites its evidence", ev["citing_evidence"])
        + meter("Test re-run to check it", ev["with_rerun"])
        + meter("Has a screenshot", ev["with_screenshot"])
        + "</ul>")


def _models_html(data: dict) -> str:
    m = data["models"]
    if not m["calls"]:
        return ('<p class="empty">No model calls are logged. Triage with a model configured and '
                "each call is written to the audit log.</p>")
    if m["cloud_calls"] == 0:
        verdict = (f'<p class="big">0</p><p class="sub">of {_plural(m["calls"], "model call")} '
                   f'left your network ({_e(", ".join(m["hosts"]))})</p>')
    else:
        verdict = (f'<p class="big">{m["cloud_calls"]}</p><p class="sub">of '
                   f'{_plural(m["calls"], "model call")} went to a cloud model '
                   f'({_e(", ".join(m["hosts"]))}); {m["human_approved"]} approved by a person</p>')
    rows = "".join(
        f'<tr><td>{_e(g["role"])}</td><td>{_e(g["model"])}</td><td>{_e(g["location"])}</td>'
        f'<td class="num">{g["calls"]}</td><td class="num">{g["images"]}</td>'
        f'<td>{_e(_when(g["first"]))}</td></tr>' for g in m["groups"])
    classes = ", ".join(f"{_e(k)} ({v})" for k, v in m["data_classes"].items()) or "none"
    p = m["policy"]
    return (
        '<div class="split2"><div>' + verdict
        + f'<p class="note">Data sent: {classes}.</p></div><div>'
        '<div class="scroll"><table><thead><tr><th>Role</th><th>Model</th><th>Where</th>'
        '<th class="num">Calls</th><th class="num">Images</th><th>First call</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
        f'<p class="note">Policy for cloud models allows '
        f'{_e(", ".join(p["may_leave"]) or "nothing")} and never sends '
        f'{_e(", ".join(p["never_leaves"]))}. The audit log lists calls that were made; a call '
        "the policy refused is stopped before it is logged, so it is not counted here.</p>"
        "</div></div>")


def _runs(data: dict) -> tuple[str, str]:
    runs = data["runs"]
    if not runs:
        return ('<p class="empty">Run history starts with your next <code>tessella triage</code>. '
                "Each run adds one row, so repeat failures can be told apart from new ones.</p>"), ""
    last = runs[-12:]
    top = max(r["failures"] for r in last) or 1
    w, h, gap = 22, 64, 6
    bars = "".join(
        f'<rect x="{i * (w + gap)}" y="{h - max(r["failures"] / top * h, 2):.1f}" width="{w}" '
        f'height="{max(r["failures"] / top * h, 2):.1f}" rx="3"><title>{_e(_when(r["at"]))}: '
        f'{r["failures"]} failures, {r["clusters"]} clusters</title></rect>'
        for i, r in enumerate(last))
    svg = (f'<svg class="runs" viewBox="0 0 {len(last) * (w + gap)} {h}" role="img" '
           f'aria-label="Failures per run, oldest on the left">{bars}</svg>')
    rows = "".join(
        f'<tr><td>{_e(_when(r["at"]))}</td><td class="num">{r["failures"]}</td>'
        f'<td class="num">{r["clusters"]}</td><td class="num">{r["recalled"]}</td>'
        f'<td class="num">{r["drafted"]}</td></tr>' for r in reversed(last))
    table = ('<div class="scroll"><table><thead><tr><th>Run</th><th class="num">Failures</th>'
             '<th class="num">Clusters</th><th class="num">Judged before</th>'
             f'<th class="num">Drafts</th></tr></thead><tbody>{rows}</tbody></table></div>')
    back = ""
    if data["recurring"]:
        items = "".join(
            f'<li><b>{r["runs"]} runs</b> <span>{_e(r["title"] or r["signature"][:8])}</span></li>'
            for r in data["recurring"][:6])
        back = f'<h3>Came back</h3><ul class="back">{items}</ul>'
        take = (f"{_plural(len(data['recurring']), 'cluster')} showed up in more than one run.")
    else:
        take = ("No cluster has appeared in more than one run." if len(runs) > 1
                else "One run recorded so far.")
    return f'<div class="split2 runs-split"><div>{svg}{back}</div><div>{table}</div></div>', take


BOARD_LABELS = {"on_board": "Already on the board", "likely_on_board": "Probably on the board",
                "new": "New: draft ready", "your_call": "Your call", "decided": "Decided",
                "pending": "Not analysed"}


def _ticket_links(links: list[dict]) -> str:
    out = []
    for link in links:
        url, key = _safe_url(link.get("url")), _e(link["key"])
        out.append(f'<a href="{_e(url)}" rel="noopener noreferrer">{key}</a>' if url else key)
    return ", ".join(out)


def _cases(data: dict, live: bool = False) -> str:
    """Every failure once: the latest run's first, each with its board status, its ticket and
    what to do; failures from earlier runs below, marked as such. Click a test for details."""
    if not data["clusters"]:
        return '<p class="empty">No cases stored.</p>'
    b = data["board"]
    order = {sig: i for i, sig in enumerate(b["latest"])}
    clusters = sorted(data["clusters"], key=lambda c: order.get(c["signature"], len(order)))
    rows, older_from = [], None
    for c in clusters:
        st = b["status"].get(c["signature"]) or {"status": "pending", "note": "", "links": [],
                                                 "latest": False}
        if not st["latest"] and older_from is None and b["latest"]:
            older_from = len(rows)
            rows.append('<tr class="sep"><td colspan="5">Not in the latest run</td></tr>')
        conf = f" {c['confidence']:.0%}" if c["confidence"] is not None else ""
        badge = (f'<span class="st st-{_e(st["status"])}">{_e(BOARD_LABELS[st["status"]])}</span>')
        for i, t in enumerate(c["tests"]):
            first = i == 0
            rows.append(
                f'<tr id="case-{_e(t["id"])}" data-case="{_e(t["id"])}"'
                f'{"" if st["latest"] or not b["latest"] else " class=old"}>'
                f'<td class="test">{_e(t["title"])}</td>'
                f'<td data-label="Verdict">{_swatch(c["category"])}{_e(LABELS[c["category"]])}{conf}</td>'
                f'<td data-label="Board">{badge if first else "same failure"}</td>'
                f'<td data-label="Ticket">{_ticket_links(st["links"]) if first else ""}</td>'
                f'<td class="todo">{_e(st["note"]) if first else ""}</td></tr>')
    more = ""
    if len(rows) > MAX_CASE_ROWS:
        more = (f'<p class="note">Showing {MAX_CASE_ROWS} of {len(rows)} rows, the latest run '
                "first.</p>")
        rows = rows[:MAX_CASE_ROWS]
    table = ('<div class="scroll"><table class="cases"><thead><tr><th>Test</th><th>Verdict</th>'
             '<th>Board</th><th>Ticket</th><th>What to do</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>{more}')
    switch = ('<div class="vt" role="group" aria-label="Show failures as">'
              '<button type="button" data-fview="table" aria-pressed="true">Table</button>'
              '<button type="button" data-fview="board" aria-pressed="false">Board</button></div>')
    return (switch + f'<div data-fview-pane="table">{table}</div>'
            f'<div data-fview-pane="board" hidden>{_kanban(data, live)}</div>')


# The triage board's columns, left to right: what needs a person first.
KANBAN = ["new", "likely_on_board", "your_call", "on_board", "decided", "pending"]
# Where a card can be dropped on the live page, and what that records. (action, reason, label)
DROPS = [("approve", "", "Approve and file"), ("reject", "duplicate", "Duplicate"),
         ("reject", "not_a_bug", "Not a bug"), ("reject", "test_bug", "Test bug"),
         ("reject", "flaky", "Flaky"), ("reject", "environment", "Environment"),
         ("reject", "test_data", "Test data")]


def _kanban(data: dict, live: bool) -> str:
    """The latest run as cards in columns by board status. On the live page (the queue's
    /report), a waiting card can be dragged onto a decision; the saved file is read-only."""
    b = data["board"]
    if not b["latest"]:
        return '<p class="empty">The board shows the latest run. Run <code>tessella triage</code>.</p>'
    by_sig = {c["signature"]: c for c in data["clusters"]}
    cols: dict[str, list[str]] = {k: [] for k in KANBAN}
    for sig in b["latest"]:
        c, st = by_sig.get(sig), b["status"].get(sig)
        if c is None or st is None:
            continue
        lead = c["lead"]
        open_ = c["state"] == "open"
        conf = f" {c['confidence']:.0%}" if c["confidence"] is not None else ""
        thumb = (f'<img class="thumb" loading="lazy" alt="" src="/cases/{_e(lead.id)}/image/0">'
                 if live and lead.screenshots and Path(lead.screenshots[0]).is_file() else "")
        links = _ticket_links(st["links"])
        decide = (f'<button type="button" class="decide" data-decide="{_e(lead.id)}" '
                  f'aria-haspopup="menu">Decide&#8230;</button>' if live and open_ else "")
        cols[st["status"]].append(
            f'<article class="card" data-case="{_e(lead.id)}" data-open="{int(open_)}" '
            f'data-draft="{int(bool(lead.draft))}" draggable="{"true" if live and open_ else "false"}">'
            f'{thumb}<p class="ct">{_e(c["tests"][0]["title"])}</p>'
            f'<p class="cm">{_swatch(c["category"])}{_e(LABELS[c["category"]])}{conf}'
            + (f' · {_plural(len(c["tests"]), "test")}' if len(c["tests"]) > 1 else "")
            + "</p>"
            + (f'<p class="cl">{links}</p>' if links else "")
            + f'<p class="cn">{_e(st["note"])}</p>{decide}</article>')
    columns = "".join(
        f'<section class="col col-{k}{"" if cols[k] else " empty"}" aria-label="{_e(BOARD_LABELS[k])}">'
        f'<h3><span class="st st-{k}">{_e(BOARD_LABELS[k])}</span> <b>{len(cols[k])}</b></h3>'
        + ("".join(cols[k]) or '<p class="none">None</p>') + "</section>"
        for k in KANBAN if cols[k] or k in KANBAN[:4])
    drops = ""
    if live:
        zones = "".join(
            f'<div class="drop" data-action="{a}" data-reason="{r}"'
            f'{" data-needs-draft" if a == "approve" else ""}>{_e(label)}</div>'
            for a, r, label in DROPS)
        drops = (f'<div class="drops" aria-label="Drop a card here to decide it">{zones}</div>'
                 '<p class="note">Drag a waiting card onto a decision, or use its Decide button. '
                 "You confirm each one; approving files it to the board.</p>")
    else:
        drops = ('<p class="note">Read-only in a saved report. Open it from the review queue '
                 "(<code>tessella queue</code>, then Report) to decide cards by dragging them.</p>")
    return f'{drops}<div class="kanban">{columns}</div>'


def _kpis(data: dict) -> str:
    """Four numbers at the top of the full page, in place of a sentence that repeated them."""
    b, time = data["board"], data["time"]
    c = b["counts"]
    to_review = c.get("new", 0) + c.get("likely_on_board", 0)
    if time["has_data"]:
        saved = (f'<span id="h-net">{_hours(time["net_min"])}</span>'
                 + (' <span class="basis b-assumed">estimate</span>' if time["assumed"] else ""))
    else:
        saved = "\u2014"

    def tile(label: str, value: str, sub: str = "", cls: str = "") -> str:
        return (f'<div class="kpi {cls}"><span class="kl">{_e(label)}</span>'
                f'<b class="kv">{value}</b>' + (f'<span class="ks">{_e(sub)}</span>' if sub else "")
                + "</div>")

    when = f"run of {_when(b['at'])}" if b["at"] else "no run recorded yet"
    return ('<div class="kpis">'
            + tile("Failures in the latest run", str(b["failures"]), when)
            + tile("Already on the board", str(c.get("on_board", 0)), "no new ticket needed",
                   "k-on_board")
            + tile("Drafts to review", str(to_review),
                   f"{c.get('likely_on_board', 0)} may be duplicates" if c.get("likely_on_board")
                   else "new bugs, fully drafted", "k-new")
            + (tile("Net time saved", saved, "all runs, after review time") if time["has_data"]
               else tile("Time saved", saved, "nothing reviewed yet"))
            + "</div>")


def _action(b: dict) -> str:
    """The full page's headline: what a person has to do about the latest run. The counts
    themselves are in the key numbers right under it."""
    c = b["counts"]
    new, likely, call = c.get("new", 0), c.get("likely_on_board", 0), c.get("your_call", 0)
    todo = []
    if new or likely:
        todo.append(f"Review {_plural(new + likely, 'draft')}"
                    + (f" ({likely} may already be on the board)" if likely else ""))
    if call:
        todo.append(f"decide {_plural(call, 'failure')} that {'is' if call == 1 else 'are'} "
                    "not a bug ticket")
    if not todo:
        return ("Nothing new to file: every failure in the latest run is already on the board "
                "or decided.")
    line = " and ".join(todo)
    return line[0].upper() + line[1:] + "."


def _tabs(shown: list[tuple[str, str]]) -> str:
    ids = {pid for pid, _ in shown}
    buttons = "".join(
        f'<button type="button" role="tab" id="tab-{tid}" data-tab-btn="{tid}" '
        f'aria-selected="{"true" if i == 0 else "false"}">{_e(label)}</button>'
        for i, (tid, label, pids) in enumerate(TABS) if ids & set(pids))
    return f'<nav class="tabs" role="tablist" aria-label="Report sections">{buttons}</nav>'


def _json_for_script(value: object) -> str:
    """JSON that is safe inside <script type="application/json">: no '<' to end the tag or
    open a comment, no '&'."""
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render(data: dict, live: bool = False, view: str = "full", csrf: str | None = None) -> str:
    """The page. `live` is set when the review queue serves it: it links back to the queue
    and offers an auto-refresh option. `view="management"` keeps only SUMMARY_PANELS."""
    if view not in VIEWS:
        view = "full"
    t = data["totals"]
    compare_body, compare_take = _compare_html(data)
    outcomes_body, _ = _outcomes(data)
    probe_body, probe_take = _probe_html(data)
    funnel_body, funnel_take = _funnel(t)
    runs_body, runs_take = _runs(data)
    all_panels = {
        "cases": lambda: _panel("cases", "Each failure against the tickets already filed, so "
                                "nothing known is filed twice. Select a test for its evidence.",
                                _cases(data, live)),
        "mosaic": lambda: _panel(
            "mosaic", "Every failure stored, from all runs: one tile per failing test, grouped by "
            "cluster. Colour is the verdict. "
            "Hover or tab to a tile to read it; select one to open its details.", _mosaic(data)),
        "funnel": lambda: _panel("funnel", "All runs: how much of the noise became something a person had "
                                 "to read.", funnel_body, _e(funnel_take)),
        "saved": lambda: _panel("saved", "What reviewing Tessella's work cost, against what doing "
                                "the same work by hand would have.", _saved(data)),
        "trust": lambda: _panel("trust", "How often a person approved what Tessella drafted.",
                                _trust(data)),
        "outcomes": lambda: _panel("outcomes", "Approving a draft says a person believed it. This "
                                   "is whether the ticket turned out to be worth filing.",
                                   outcomes_body),
        "compare": lambda: _panel("compare", "The latest run against the one before it.",
                                  compare_body, compare_take),
        "runs": lambda: _panel("runs", "One row per triage. Clusters that return run after run "
                               "are the ones worth fixing at the source.", runs_body,
                               _e(runs_take)),
        "evidence": lambda: _panel("evidence", "A verdict has to cite evidence that was collected "
                                   "before the model ran.", _evidence(data)),
        "models": lambda: _panel("models", "Every model call, from the audit log. Local means it "
                                 "stayed on your machine or network.", _models_html(data)),
        "probe": lambda: _panel("probe", "Recorded flows read and the changes planned for them.",
                                probe_body, probe_take),
    }
    shown = [(pid, title) for pid, title in PANELS
             if view == "full" or pid in SUMMARY_PANELS]
    if view == "management":
        shown.sort(key=lambda pt: SUMMARY_PANELS.index(pt[0]))
    panels = "".join(all_panels[pid]() for pid, _ in shown)
    controls = "".join(
        f'<li data-panel-row="{pid}"><label><input type="checkbox" data-show="{pid}" checked> '
        f'{_e(title)}</label><span class="mv">'
        f'<button type="button" data-move="up" data-for="{pid}" aria-label="Move {_e(title)} up">Up</button>'
        f'<button type="button" data-move="down" data-for="{pid}" aria-label="Move {_e(title)} down">Down</button>'
        "</span></li>" for pid, title in shown)
    sections = {
        "title": (f'Tessella summary for {_e(data["project"])}' if view == "management"
                  else f'Tessella report for {_e(data["project"])}'),
        "view": view,
        "summary": ",".join(SUMMARY_PANELS),
        "details": _json_for_script(data["details"] if view == "full" else {}),
        "project": _e(data["project"]),
        "generated": _e(_when(data["generated_at"])),
        "source": f'<span title="{_e(data["source"])}">{_e(Path(data["source"]).name)}</span>',
        "headline": (_action(data["board"]) if view == "full" and data["board"]["has"]
                     else _headline(t, data["time"])),
        "kpis": _kpis(data) if view == "full" else "",
        "tabs": _tabs(shown) if view == "full" else "",
        "nav": '<a class="navlink" href="/">Review queue</a>' if live else "",
        "live": "true" if live else "false",
        # The queue's form token, so the live board can record a decision. Only the queue
        # serves this, and only on its own origin: another site cannot read the page.
        "csrf": _e(csrf) if live and csrf and view == "full" else "",
        "footer": ("Served by your review queue: screenshots come from it and decisions go only "
                   "to it. Nothing is fetched from elsewhere." if live else
                   "Nothing on this page was fetched. It is one file with no scripts or fonts from "
                   "elsewhere, so it can be attached to a CI run or sent as is."),
        "panels": panels,
        "panel_controls": controls,
    }
    # One pass over the template, so text inside a value (a test title that happens to
    # contain "{{cases}}") is never expanded as a placeholder.
    return re.sub(r"\{\{(\w+)\}\}", lambda m: sections.get(m.group(1), m.group(0)),
                  TEMPLATE.read_text())


def write(settings: Settings, out: Path, view: str = "full") -> Path:
    data = build(settings, Store(settings.storage.path))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data, view=view), encoding="utf-8")
    return out
