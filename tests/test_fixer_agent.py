"""The fixer agent: looks at the code, proposes a patch, checks it in a copy, tries again
when told what failed, and only hands over a fix that passed. Tests never call a model."""

import json
import subprocess
import sys

from tessella.config import ModelSettings, Settings
from tessella.draft import fixer_agent
from tessella.integrations import llm
from tessella.models import Case, Category, Draft, DraftKind, Evidence, EvidenceKind, Origin, Verdict

APP = '''RULES = {"mosaic10": 1.0}


def refund(code):
    return RULES[code]
'''
TEST = '''from app import refund


def test_refund_upper_case_code():
    assert refund("MOSAIC10") == 1.0


def test_refund_known_code():
    assert refund("mosaic10") == 1.0
'''
GOOD = {"old": "    return RULES[code]", "new": "    return RULES[code.lower()]"}
WRONG = {"old": "return RULES[code]", "new": "return RULES.get(code, 0.0)"}  # loose indentation


def _setup(tmp_path, suite=True):
    (tmp_path / "app.py").write_text(APP)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(TEST)
    (tmp_path / "conftest.py").write_text("")
    s = Settings.model_validate({
        "models": {"reader": {"name": "m"}, "fixer": {"provider": "ollama_chat", "name": "coder",
                                                     "base_url": "http://127.0.0.1:11435"}},
        "network": {"allow": ["localhost:11435"]}})
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    s.inputs.repo = str(tmp_path)
    s.rerun.command = f"{sys.executable} -m pytest {{test_id}} -q -p no:cacheprovider"
    s.rerun.attempts = 1
    if suite:
        s.fix.suite_command = f"{sys.executable} -m pytest -q -p no:cacheprovider tests"
    tb = 'Traceback:\n  File "app.py", line 5, in refund\n    return RULES[code]\nKeyError: \'MOSAIC10\''
    case = Case(id="c1", origin=Origin.test_failure, title="tests/test_app.py::test_refund_upper_case_code",
                test_id="tests/test_app.py::test_refund_upper_case_code", message="KeyError: 'MOSAIC10'",
                signature="sig1",
                evidence=[Evidence(kind=EvidenceKind.log, summary="1 error", excerpt=tb)],
                verdict=Verdict(category=Category.product_bug, confidence=0.9,
                                root_cause="the refund lookup is case sensitive", evidence_used=[0]))
    return s, case


def _replies(monkeypatch, *steps):
    sent, it = [], iter(steps)

    def call(ms, messages, **kw):
        sent.append(messages[-1]["content"])
        return json.dumps(next(it)), 1

    monkeypatch.setattr(llm, "_call", call)
    return sent


def test_the_brief_shows_the_code_the_traceback_points_to(tmp_path):
    s, case = _setup(tmp_path)
    assert fixer_agent.code_pointers(case, tmp_path) == [("app.py", 5)]
    assert "RULES[code]" in fixer_agent._brief(case, tmp_path)


def test_a_failed_patch_is_reported_back_and_the_next_one_is_kept(tmp_path, monkeypatch):
    s, case = _setup(tmp_path)
    sent = _replies(monkeypatch,
                    {"thought": "look", "tool": "search", "query": "RULES"},
                    {"thought": "default", "tool": "edit", "path": "app.py", **WRONG},
                    {"thought": "lower it", "tool": "edit", "path": "app.py", **GOOD})
    diff, status = fixer_agent.run(case, s)
    assert status.startswith("verified") and "suite fails no new test" in status
    assert diff.startswith("--- a/app.py\n+++ b/app.py\n@@") and "+    return RULES[code.lower()]" in diff
    assert "app.py:1:" in sent[1]  # the search result reached the model
    assert "the test still fails" in sent[2]  # so did the failed patch
    assert (tmp_path / "app.py").read_text() == APP  # your files are untouched


def test_a_patch_that_breaks_another_test_is_not_kept(tmp_path, monkeypatch):
    s, case = _setup(tmp_path)
    breaks = {"old": "return RULES[code]", "new": "return 1.0 if code.isupper() else RULES['x']"}
    _replies(monkeypatch, *[{"thought": "t", "tool": "edit", "path": "app.py", **breaks}] * 2)
    s.fix.steps = 2
    diff, status = fixer_agent.run(case, s)
    assert diff == "" and "other tests fail" in status and "test_refund_known_code" in status


def test_it_never_patches_tests_or_files_outside_the_repository(tmp_path, monkeypatch):
    s, case = _setup(tmp_path)
    sent = _replies(monkeypatch,
                    {"thought": "t", "tool": "edit", "path": "tests/test_app.py", **GOOD},
                    {"thought": "t", "tool": "read_file", "path": "../../etc/passwd"},
                    {"thought": "no", "tool": "give_up"})
    diff, status = fixer_agent.run(case, s)
    assert diff == "" and status.startswith("no fix: the fixer gave up")
    assert "not the tests" in sent[1] and "No readable file" in sent[2]


def test_without_a_suite_the_status_says_so(tmp_path, monkeypatch):
    s, case = _setup(tmp_path, suite=False)
    _replies(monkeypatch, {"thought": "t", "tool": "edit", "path": "app.py", **GOOD})
    diff, status = fixer_agent.run(case, s)
    assert diff and "suite was not run" in status


def test_fix_apply_puts_the_fix_on_a_branch(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from tessella import cli
    from tessella.memory.store import Store

    s, case = _setup(tmp_path)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    diff = fixer_agent.unified("app.py", APP, APP.replace("RULES[code]", "RULES[code.lower()]"))
    case.draft = Draft(kind=DraftKind.ticket, title="[test_failure] KeyError", body="b",
                       root_cause="case sensitive", patch=diff, patch_status="verified: ...")
    db = tmp_path / ".tessella" / "t.sqlite"
    db.parent.mkdir()
    Store(db).save_case(case)
    (tmp_path / "tessella.yaml").write_text(f"storage: {{path: {db}}}\ninputs: {{repo: {tmp_path}}}\n")
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(cli.app, ["fix", "apply", "sig1"])
    assert out.exit_code == 0, out.output
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "-1", "--format=%s%n%b", "tessella/fix-sig1"],
                         capture_output=True, text=True).stdout
    assert log.startswith("Fix: KeyError") and "Checked by Tessella: verified" in log
    assert "code.lower()" in (tmp_path / "app.py").read_text()


def test_edits_match_loose_indentation_and_refuse_ambiguity():
    text = "def f():\n    x = 1\n    return x\n"
    assert fixer_agent.replace_once(text, "x = 1", "x = 2") == text.replace("x = 1", "x = 2")
    loose = fixer_agent.replace_once(text, "x = 1\nreturn x", "x = 2\nreturn x")
    assert loose == "def f():\n    x = 2\n    return x\n"
    assert fixer_agent.replace_once(text, "x", "y").startswith("\0")  # several matches
    assert fixer_agent.replace_once(text, "    2  x = 1", "x = 3").startswith("\0") is False
