"""Schemathesis and ZAP reports read as probe findings, judged and drafted like failures."""

from pathlib import Path

from tessella.collect import collect_cases
from tessella.collect.findings import SCHEMATHESIS, ZAP, at_least, redact_curl
from tessella.config import Settings
from tessella.models import Category, Origin
from tessella.pipeline import triage
from tessella.verdict import agent

FIXTURES = Path(__file__).parent / "fixtures"
ST = FIXTURES / "schemathesis_junit.xml"  # a real Schemathesis 4 report
ZAP_JSON = FIXTURES / "zap_report.json"


def _settings(tmp_path, *reports) -> Settings:
    s = Settings()
    s.inputs.reports = [str(r) for r in reports]
    s.inputs.traces = []
    s.storage.path = str(tmp_path / "t.sqlite")
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    return s


def test_schemathesis_report_is_not_read_as_test_failures():
    assert SCHEMATHESIS.can_read(ST)
    cases = collect_cases([str(ST)], [])
    assert {c.origin for c in cases} == {Origin.probe_finding}
    assert len(cases) == 2


def test_schemathesis_finding_fields():
    by_op = {c.title: c for c in SCHEMATHESIS.read(ST, ["Authorization"])}
    post = by_op["POST /orders (Schemathesis)"]
    assert "ST101 Server error" in post.message and "received 500" in post.message
    assert post.severity == "high"
    assert post.reproduce.startswith("curl -X POST") and "/orders" in post.reproduce
    assert post.evidence[0].ref == "schemathesis:schema_conformance,server_error"
    get = by_op["GET /orders/{oid} (Schemathesis)"]
    assert get.severity == "medium" and get.evidence[0].ref == "schemathesis:schema_conformance"


def test_several_examples_and_old_check_names(tmp_path):
    report = tmp_path / "st.xml"
    report.write_text(
        '<testsuites><testsuite name="schemathesis"><testcase name="DELETE /users/{id}">'
        '<failure type="failure" message="x">1. Test Case ID: aaa\n\n- Server error\n\n'
        "[502] Bad Gateway:\n\nReproduce with:\n\n"
        "    curl -X DELETE -H 'Authorization: Bearer s3cr3t' -H 'Accept: */*' "
        "http://api.local/users/1\n\n"
        "2. Test Case ID: bbb\n\n- ST401 Ignored authentication\n\n[200] OK:\n\n"
        "Reproduce with:\n\n    curl -X DELETE http://api.local/users/2</failure>"
        "</testcase></testsuite></testsuites>")
    first, second = SCHEMATHESIS.read(report, ["Authorization"])
    assert first.evidence[0].ref == "schemathesis:server_error"
    assert "s3cr3t" not in first.reproduce and "Accept: */*" in first.reproduce
    assert "s3cr3t" not in first.evidence[0].excerpt
    assert second.severity == "critical" and second.signature != first.signature


def test_redact_curl_masks_named_headers():
    out = redact_curl("curl -H 'X-Session: abc123' -H \"Cookie: sid=9\" http://a", ["X-Session"])
    assert "abc123" not in out and "sid=9" not in out


def test_zap_alerts():
    assert ZAP.can_read(ZAP_JSON)
    cases = {c.title: c for c in ZAP.read(ZAP_JSON, ["token"])}
    assert len(cases) == 3  # the alert marked false positive in ZAP is left out
    xss = cases["Cross Site Scripting (Reflected) (ZAP)"]
    assert xss.severity == "high" and xss.evidence[0].ref.endswith("confidence=4")
    assert "parameter q" in xss.reproduce and "<p>" not in xss.evidence[0].excerpt
    csp = cases["Content Security Policy (CSP) Header Not Set (ZAP)"]
    assert "2 instance(s)" in csp.message and "token=abc" not in csp.evidence[0].excerpt


def test_certain_findings_need_no_model():
    by_title = {c.title: c for c in collect_cases([str(ST), str(ZAP_JSON)], [])}
    v = agent.heuristic(by_title["POST /orders (Schemathesis)"])
    assert v.category == Category.product_bug and v.steps_to_reproduce.startswith("curl")
    assert agent.heuristic(by_title["Cross Site Scripting (Reflected) (ZAP)"]).category \
        == Category.product_bug
    # Conformance problems and unconfirmed alerts go to the reader model.
    assert agent.heuristic(by_title["GET /orders/{oid} (Schemathesis)"]) is None
    assert agent.heuristic(by_title["Content Security Policy (CSP) Header Not Set (ZAP)"]) is None


def test_triage_drafts_tickets_for_findings(tmp_path):
    s = _settings(tmp_path, ST, ZAP_JSON)
    result = triage(s, rerun=False, use_model=False)
    cases = {c.title: c for c in result.cases}
    assert "Server Leaks Information via \"X-Powered-By\" HTTP Response Header Field(s) (ZAP)" \
        not in cases  # informational, below the default min_severity "low"
    draft = cases["POST /orders (Schemathesis)"].draft
    assert draft.severity == "high"
    assert "curl -X POST" in draft.steps_to_reproduce
    assert cases["GET /orders/{oid} (Schemathesis)"].draft is None  # no model: no verdict


def test_min_severity(tmp_path):
    s = _settings(tmp_path, ZAP_JSON)
    s.probe.min_severity = "high"
    assert [c.title for c in triage(s, rerun=False, use_model=False).cases] \
        == ["Cross Site Scripting (Reflected) (ZAP)"]
    assert at_least("medium", "low") and not at_least("info", "low") and at_least(None, "high")
