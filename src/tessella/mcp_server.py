"""MCP server: Tessella's cases, evidence and verdicts for a developer's coding assistant.

`tessella mcp` speaks the Model Context Protocol over stdio, so a local coding agent (in an
editor or a terminal) can ask "why does this test fail?" and get Tessella's cited evidence
instead of re-investigating. Install with `pip install tessella[mcp]`.

Read-only on purpose: no tool approves, rejects or files anything. Those stay with a person
in the review queue. Everything served was redacted when it was collected; set
`mcp.excerpts: false` to serve only summaries when the assistant uses a cloud model.
"""

from __future__ import annotations

from tessella.config import Settings
from tessella.memory.store import Store
from tessella.models import Case

INSTRUCTIONS = """Tessella triages failed tests and imported API/security findings. Each case
has a verdict (product_bug, test_bug, environment, test_data, flaky, unknown) that cites
numbered evidence: re-runs, log lines, git diffs, test source, screenshots read by a model.
Use list_cases or cases_for_file to find a case, then get_case for its evidence. Verdicts
are a model's reading of evidence; cite the evidence, not the verdict alone."""

STACK_LINES = 40


def _row(case: Case, decision=None) -> dict:
    v = case.verdict
    return {
        "id": case.id,
        "title": case.title,
        "test_id": case.test_id,
        "origin": str(case.origin),
        "signature": case.signature,
        "category": str(v.category) if v else None,
        "confidence": round(v.confidence, 2) if v else None,
        "root_cause": v.root_cause if v else None,
        "severity": case.severity,
        "decision": (f"{decision.action}" + (f" as {decision.ticket_key}" if decision.ticket_key
                                              else "")) if decision else None,
    }


class Tools:
    """The tool functions, kept free of the MCP library so they can be tested directly."""

    def __init__(self, settings: Settings, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(settings.storage.path)

    def _decisions(self) -> dict[str, object]:
        return {case.id: d for case, d in self.store.recent_decisions(limit=10_000)}

    def list_cases(self, state: str = "open", category: str | None = None,
                   limit: int = 50) -> list[dict]:
        """Cases Tessella has judged, newest first. state: open (waiting for review),
        decided, or all. category: only cases with this verdict, e.g. product_bug."""
        decided = self._decisions()
        cases = self.store.open_cases() if state == "open" else self.store.all_cases()[::-1]
        if state == "decided":
            cases = [c for c in cases if c.id in decided]
        if category:
            cases = [c for c in cases if c.verdict and c.verdict.category == category]
        return [_row(c, decided.get(c.id)) for c in cases[: max(1, min(limit, 500))]]

    def get_case(self, case_id: str) -> dict:
        """One case in full: message, the top of the stack, every piece of evidence with its
        number, the verdict (which cites those numbers), the drafted ticket and any test
        patch, and the reviewer's decision."""
        case = self.store.get_case(case_id)
        if case is None:
            return {"error": f"no case {case_id}; use list_cases to find ids"}
        excerpts = self.settings.mcp.excerpts
        out = _row(case, self._decisions().get(case.id))
        out.update({
            "message": case.message,
            "stack": "\n".join(case.stack.splitlines()[:STACK_LINES]) if excerpts else "",
            "source": case.source,
            "reproduce": case.reproduce if excerpts else "",
            "evidence": [{"n": i, "kind": str(e.kind), "summary": e.summary,
                          "ref": e.ref, **({"excerpt": e.excerpt} if excerpts else {})}
                         for i, e in enumerate(case.evidence)],
        })
        v = case.verdict
        if v:
            out["verdict"] = {"category": str(v.category), "confidence": v.confidence,
                              "root_cause": v.root_cause, "evidence_used": v.evidence_used,
                              "expected_result": v.expected_result,
                              "suggested_fix": v.suggested_fix, "agreement": v.agreement}
        if case.draft:
            out["draft"] = {"title": case.draft.title, "body": case.draft.body,
                            "patch": case.draft.patch, "patch_status": case.draft.patch_status}
        return out

    def find_cases(self, text: str, limit: int = 20) -> list[dict]:
        """Cases whose title, test id or failure message contains `text` (any case)."""
        needle = text.lower().strip()
        decided = self._decisions()
        hits = [c for c in self.store.all_cases()[::-1]
                if needle and needle in f"{c.title}\n{c.test_id}\n{c.message}".lower()]
        return [_row(c, decided.get(c.id)) for c in hits[: max(1, min(limit, 200))]]

    def cases_for_file(self, path: str, limit: int = 20) -> list[dict]:
        """Cases whose test id or stack mentions this source file, e.g. the file you are
        editing: tests/test_checkout.py or src/shop/cart.py."""
        name = path.replace("\\", "/").strip("/")
        tail = name.split("/")[-1]
        decided = self._decisions()
        hits = []
        for c in self.store.all_cases()[::-1]:
            text = f"{c.test_id}\n{c.stack}".replace("\\", "/")
            if name in text or (tail and f"/{tail}" in text) or text.startswith(tail):
                hits.append(c)
        return [_row(c, decided.get(c.id)) for c in hits[: max(1, min(limit, 200))]]

    def recent_runs(self, limit: int = 10) -> list[dict]:
        """The latest triage runs: failures, clusters, drafts, the commit, when."""
        runs = self.store.runs()
        return [{k: r.get(k) for k in ("at", "failures", "clusters", "recalled", "drafted",
                                       "git_commit", "duration_s")}
                for r in runs[-max(1, min(limit, 100)):]][::-1]


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP


def build(settings: Settings, store: Store | None = None):
    tools = Tools(settings, store)
    server = _server_class()("tessella", instructions=INSTRUCTIONS)
    for fn in (tools.list_cases, tools.get_case, tools.find_cases, tools.cases_for_file,
               tools.recent_runs):
        server.tool(name=fn.__name__, description=fn.__doc__)(fn)
    return server


def serve(settings: Settings) -> None:
    build(settings).run("stdio")
