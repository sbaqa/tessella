"""Phase 0 hardening: loopback allowlist, re-run ids, queue form token, model failures,
and the `doctor --models` checks."""

import json
import sys

import httpx
import pytest

from tessella import net
from tessella.collect.allure import READER as ALLURE
from tessella.config import ModelSettings, Settings
from tessella.integrations import llm, model_health
from tessella.investigate import rerun
from tessella.models import Case, Category, Evidence, EvidenceKind, Origin
from tessella.pipeline import triage
from tessella.verdict import agent


def _case(title="tests/test_pay.py::test_pay", test_id=""):
    return Case(id="c1", origin=Origin.test_failure, title=title, test_id=test_id,
                message="boom", evidence=[Evidence(kind=EvidenceKind.report, summary="failed")])


# --- loopback allowlist ---------------------------------------------------------------

def test_loopback_spellings_are_one_host():
    assert net.is_allowed("http://127.0.0.1:11435", ["localhost:11435"])
    assert net.is_allowed("http://localhost:11435", ["127.0.0.1:11435"])
    assert net.is_allowed("http://[::1]:11435", ["localhost:11435"])
    assert not net.is_allowed("http://127.0.0.1:9999", ["localhost:11435"])  # port still counts
    assert not net.is_allowed("http://example.com:11435", ["localhost:11435"])


def test_init_style_config_lets_the_reader_through(tmp_path):
    s = Settings.model_validate({
        "models": {"reader": {"provider": "ollama_chat", "name": "m",
                              "base_url": "http://127.0.0.1:11435"}},
        "network": {"allow": ["localhost:11435"]},
    })
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    assert llm.check("reader", s, []).name == "m"


# --- re-run ids -----------------------------------------------------------------------

def test_reader_id_wins_and_path_titles_are_used_as_is(tmp_path):
    assert rerun.test_id_for(_case(test_id="tests/a.py::t[chromium]")) == "tests/a.py::t[chromium]"
    assert rerun.test_id_for(_case("tests/a.py::t")) == "tests/a.py::t"
    assert rerun.test_id_for(_case("e2e/cart.spec.ts:12")) == "e2e/cart.spec.ts:12"


def test_junit_dotted_title_maps_to_the_file_and_class(tmp_path):
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "api" / "test_orders.py").write_text("")
    case = _case("tests.api.test_orders.TestCancel::test_refund")
    assert rerun.test_id_for(case, tmp_path) == "tests/api/test_orders.py::TestCancel::test_refund"


def test_title_with_no_runnable_id_is_not_rerun():
    ev = rerun.rerun(_case("Checkout > pays with a card"), "pytest {test_id} -q")
    assert ev.summary.startswith("Not re-run") and "consistent" not in ev.summary


def test_command_that_never_ran_the_test_is_not_a_consistent_failure(tmp_path):
    missing = rerun.rerun(_case(), "no-such-binary-xyz {test_id}", attempts=1)
    assert missing.summary.startswith("Could not re-run") and "could not start" in missing.summary
    fake = tmp_path / "pytest"  # exit code 4 = usage error, e.g. a node id that doesn't exist
    fake.write_text("#!/bin/sh\nexit 4\n")
    fake.chmod(0o755)
    usage = rerun.rerun(_case(), f"{fake} {{test_id}}", attempts=2)
    assert usage.summary == "Could not re-run: exit 4, the test did not run"
    says_so = f"{sys.executable} -c \"print('no tests ran'); raise SystemExit(1)\""
    nothing = rerun.rerun(_case(), says_so, attempts=1)
    assert nothing.summary.startswith("Could not re-run")


def test_real_failures_and_passes_still_count():
    fail = rerun.rerun(_case(), f"{sys.executable} -c 'raise SystemExit(1)'", attempts=2)
    ok = rerun.rerun(_case(), f"{sys.executable} -c 'pass'", attempts=2)
    assert fail.summary.startswith("Failed on all 2") and ok.summary.startswith("Passed on all 2")


def test_allure_pytest_result_carries_a_node_id(tmp_path):
    folder = tmp_path / "data" / "test-cases"
    folder.mkdir(parents=True)
    (folder / "x.json").write_text(json.dumps({
        "name": "test_cancel[chromium]", "fullName": "tests.ui.test_shop#test_cancel",
        "status": "failed", "statusMessage": "boom",
        "labels": [{"name": "framework", "value": "pytest"},
                   {"name": "package", "value": "tests.ui.test_shop"}]}))
    [case] = ALLURE.read(tmp_path, [])
    assert case.test_id == "tests/ui/test_shop.py::test_cancel[chromium]"


# --- queue form token -----------------------------------------------------------------

def test_queue_refuses_a_decision_without_its_form_token(tmp_path):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from tessella.memory.store import Store
    from tessella.queue.app import create_app

    s = Settings()
    s.storage.path = str(tmp_path / "t.sqlite")
    Store(s.storage.path).save_case(_case())
    app = create_app(s)
    client = TestClient(app)
    assert app.state.csrf in client.get("/").text
    forged = client.post("/cases/c1/decide", data={"action": "approve"}, follow_redirects=False)
    assert forged.status_code == 403
    assert not Store(s.storage.path).decided("c1")
    form = {"action": "reject", "reason": "flaky", "csrf": app.state.csrf}
    real = client.post("/cases/c1/decide", data=form,
                       follow_redirects=False)
    assert real.status_code == 303 and Store(s.storage.path).decided("c1")


# --- model failures -------------------------------------------------------------------

def _boom(ms, messages, **_):
    raise httpx.ConnectError("connection refused")


def test_model_server_down_gives_unknown_not_a_crash(tmp_path, monkeypatch):
    s = Settings()
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    monkeypatch.setattr(llm, "_call", _boom)
    v = agent.decide(_case(), s)
    assert v.category == Category.unknown and "ConnectError" in v.root_cause


def test_one_failing_cluster_does_not_stop_the_run(tmp_path, monkeypatch):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite><testcase classname="a" name="t1"><failure message="one"/></testcase>'
        '<testcase classname="b" name="t2"><failure message="two"/></testcase></testsuite>')
    s = Settings()
    s.inputs.reports = [str(report)]
    s.storage.path = str(tmp_path / "t.sqlite")
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    calls = []

    def flaky_heuristic(case):
        calls.append(case.title)
        if len(calls) == 1:
            raise RuntimeError("investigation blew up")

    monkeypatch.setattr(agent, "heuristic", flaky_heuristic)
    result = triage(s, rerun=False, use_model=False)
    verdicts = [m[0].verdict for m in result.clusters.values()]
    assert len(calls) == 2  # the second cluster was still investigated
    assert verdicts[0].category == Category.unknown and "RuntimeError" in verdicts[0].root_cause


def test_timeout_is_passed_to_the_model_call():
    assert llm.call_options(ModelSettings(timeout_s=42))["timeout"] == 42


# --- doctor --models ------------------------------------------------------------------

def _ollama_settings(**kw):
    return Settings.model_validate({
        "models": {"reader": {"provider": "ollama_chat", "name": "qwen3.5:4b",
                              "base_url": "http://127.0.0.1:11435", "num_ctx": 8192, **kw},
                   "vision": {"provider": "ollama_chat", "name": "text-only",
                              "base_url": "http://127.0.0.1:11435"}},
        "network": {"allow": ["localhost:11435"]},
    })


def test_doctor_models_reports_pulled_missing_and_capabilities(monkeypatch):
    respx = pytest.importorskip("respx")
    monkeypatch.setattr(model_health, "_free_memory_bytes", lambda: 64 * 2**30)
    with respx.mock:
        respx.get("http://127.0.0.1:11435/api/tags").mock(return_value=httpx.Response(
            200, json={"models": [{"name": "qwen3.5:4b", "size": 3 * 2**30},
                                  {"name": "text-only:latest", "size": 2**30}]}))
        respx.post("http://127.0.0.1:11435/api/show").mock(side_effect=lambda req: httpx.Response(
            200, json={"capabilities": ["completion"] + (
                ["vision"] if json.loads(req.content)["model"] == "qwen3.5:4b" else []),
                "model_info": {"qwen35.context_length": 4096}}))
        checks = model_health.check(_ollama_settings())
    text = [(c.role, c.ok, c.message) for c in checks]
    assert ("reader", True, "qwen3.5:4b is pulled on http://127.0.0.1:11435") in text
    assert any(r == "reader" and ok is False and "context length 4096" in m for r, ok, m in text)
    assert any(r == "vision" and ok is False and "cannot read images" in m for r, ok, m in text)


def test_doctor_models_says_when_the_server_is_down():
    respx = pytest.importorskip("respx")
    with respx.mock:
        respx.get("http://127.0.0.1:11435/api/tags").mock(side_effect=httpx.ConnectError("down"))
        [check, *_] = model_health.check(_ollama_settings())
    assert check.ok is False and "is the model server running" in check.message


def test_doctor_models_flags_a_host_missing_from_the_allowlist():
    s = _ollama_settings(base_url="http://10.0.0.5:11434")
    [check, *_] = model_health.check(s)
    assert check.ok is False and "not in network.allow" in check.message
