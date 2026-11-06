"""Phase 3: logs by test time, git diff since green, pre-flight, the investigator loop,
the verified fixer, trace bodies and screenshot redaction."""

import json
import subprocess
import sys
import zipfile
from datetime import datetime

import pytest

from tessella import pipeline, redact_image
from tessella.collect.traces import TraceReader
from tessella.config import Logs, ModelSettings, Settings
from tessella.draft import patch
from tessella.integrations import llm
from tessella.investigate import git_diff, logs, tools
from tessella.memory.store import Store
from tessella.models import Case, Category, Draft, DraftKind, Evidence, EvidenceKind, Origin
from tessella.verdict import agent

LOG = """2026-10-18 07:56:57,665 WARNING POST /api/auth/login -> 401 in 1 ms
2026-10-18 07:56:58,302 INFO POST /api/orders/1001/pay -> 200 in 51 ms
2026-10-18 07:56:58,443 ERROR POST /api/orders/1001/cancel -> 500 unhandled error
Traceback (most recent call last):
  File "shop/store.py", line 190, in cancel
    share = REFUND_RULES[order.discount_code]
KeyError: 'MOSAIC10'
2026-10-18 07:56:58,600 INFO POST /api/auth/login password=hunter2 -> 200 in 1 ms
2026-10-18 07:57:30,000 ERROR something unrelated
"""


def _case(**kw):
    base = {"id": "c1", "origin": Origin.test_failure,
            "title": "tests/test_orders.py::test_cancel",
            "test_id": "tests/test_orders.py::test_cancel",
            "message": "AssertionError: cancel returned 500",
            "evidence": [Evidence(kind=EvidenceKind.report, summary="failed")]}
    return Case(**(base | kw))


# --- logs -----------------------------------------------------------------------------

def test_logs_are_read_from_the_tests_own_window(tmp_path):
    path = tmp_path / "app.log"
    path.write_text(LOG)
    case = _case(started_at=datetime(2026, 10, 18, 7, 56, 58, 388000),
                 failed_at=datetime(2026, 10, 18, 7, 56, 58, 450000))
    ev = logs.log_evidence(case, Logs(path=str(path)), [], ["password"])
    assert "KeyError: 'MOSAIC10'" in ev.summary and "Traceback" in ev.excerpt
    assert "something unrelated" not in ev.excerpt  # outside the window
    assert "07:56:56" not in ev.excerpt  # before the test started (minus the pad)


def test_plain_lines_alone_make_no_evidence(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("2026-10-18 07:56:58,302 INFO GET /api/products -> 200 in 1 ms\n")
    case = _case(started_at=datetime(2026, 10, 18, 7, 56, 58), failed_at=datetime(2026, 10, 18, 7, 56, 59))
    assert logs.log_evidence(case, Logs(path=str(path)), [], []) is None


def test_log_lines_are_redacted_and_a_needle_filters(tmp_path):
    path = tmp_path / "app.log"
    path.write_text(LOG)
    case = _case(started_at=datetime(2026, 10, 18, 7, 56, 58), failed_at=datetime(2026, 10, 18, 7, 56, 59))
    ev = logs.log_evidence(case, Logs(path=str(path), window_seconds=1), [], ["password"],
                           needle="auth/login", widen=3)
    assert "hunter2" not in ev.excerpt and "[REDACTED]" in ev.excerpt
    assert "cancel" not in ev.excerpt


def test_timestamps_with_zones_become_local_time():
    t = logs.parse_time("2026-10-18T03:56:58.443Z level=error")
    assert t == datetime(2026, 10, 18, 3, 56, 58, 443000).replace(
        tzinfo=__import__("datetime").UTC).astimezone().replace(tzinfo=None)


def test_loki_query_uses_the_window_and_the_allowlist(monkeypatch):
    respx = pytest.importorskip("respx")
    import httpx

    cfg = Logs(source="loki", url="http://loki.local:3100", query='{app="shop"}')
    case = _case(started_at=datetime(2026, 10, 18, 7, 56, 58), failed_at=datetime(2026, 10, 18, 7, 56, 59))
    with respx.mock:
        route = respx.get("http://loki.local:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(200, json={"data": {"result": [{"values": [
                [str(int(datetime(2026, 10, 18, 7, 56, 58, 500000).timestamp() * 1e9)),
                 "ERROR cancel failed\nKeyError: 'X'"]]}]}}))
        ev = logs.log_evidence(case, cfg, ["loki.local:3100"], [])
    assert route.calls[0].request.url.params["query"] == '{app="shop"}'
    assert "KeyError" in ev.summary
    with pytest.raises(Exception, match="not in network.allow"):
        logs.log_evidence(case, cfg, [], [])


# --- git diff ---------------------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "shop").mkdir(parents=True)
    (r / "shop" / "store.py").write_text("RULES = {'mosaic10': 1.0}\n")
    (r / "README.md").write_text("x\n")
    _git(r, "init", "-q")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "green")
    return r


def test_diff_shows_files_on_the_stack_only(repo):
    green = git_diff.head(str(repo))
    (repo / "shop" / "store.py").write_text("RULES = {'MOSAIC10': 1.0}\n")
    (repo / "README.md").write_text("y\n")
    case = _case(stack=f'File "{repo}/shop/store.py", line 1, in cancel')
    ev = git_diff.for_case(case, str(repo), green)
    assert "shop/store.py" in ev.summary and "README" not in ev.excerpt
    assert "+RULES = {'MOSAIC10': 1.0}" in ev.excerpt


def test_diff_says_when_nothing_on_the_stack_changed(repo):
    green = git_diff.head(str(repo))
    (repo / "README.md").write_text("y\n")
    ev = git_diff.for_case(_case(stack=f'File "{repo}/shop/store.py", line 1'), str(repo), green)
    assert "none of them on this failure's stack" in ev.summary


def test_no_diff_outside_git(tmp_path):
    assert git_diff.for_case(_case(), str(tmp_path), "abc") is None
    assert git_diff.head(str(tmp_path)) is None


def test_green_runs_record_their_commit():
    store = Store(":memory:")
    store.record_run(1, 1, 0, 0, {}, git_commit="a" * 40)
    store.record_run(0, 0, 0, 0, {}, git_commit="b" * 40)
    store.record_run(2, 1, 0, 0, {}, git_commit="c" * 40)
    assert store.last_green_commit() == "b" * 40


# --- pre-flight -------------------------------------------------------------------------

def test_preflight_problem_reaches_every_cluster_and_labels_it(tmp_path, monkeypatch):
    from tessella.investigate import preflight

    s = Settings()
    s.preflight.urls = ["http://shop.local/health"]
    problem = Evidence(kind=EvidenceKind.preflight, summary="http://shop.local/health returned 503")
    monkeypatch.setattr(preflight, "check", lambda urls, allow: [problem])
    lead = _case()
    pipeline._gather(lead, s, rerun=False, preflight=pipeline._preflight(s))
    assert lead.evidence[-1].summary == problem.summary
    assert agent.heuristic(lead).category == Category.environment


# --- investigator loop ------------------------------------------------------------------

def _reader_settings(tmp_path, **kw):
    s = Settings.model_validate({"models": {"reader": {"provider": "ollama_chat", "name": "m",
                                                       "base_url": "http://127.0.0.1:11435", **kw}},
                                 "network": {"allow": ["localhost:11435"]}})
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    s.inputs.repo = str(tmp_path)
    return s


def test_unsure_reader_asks_for_a_tool_and_reads_again(tmp_path, monkeypatch):
    s = _reader_settings(tmp_path, votes=1, two_step=False)
    s.logs.path = str(tmp_path / "app.log")
    (tmp_path / "app.log").write_text(LOG)
    case = _case(started_at=datetime(2026, 10, 18, 7, 56, 58), failed_at=datetime(2026, 10, 18, 7, 56, 59))
    replies = iter([
        json.dumps({"category": "unknown", "confidence": 0.3, "root_cause": "?", "evidence_used": [0],
                    "actual_result": "", "expected_result": "", "steps_to_reproduce": "",
                    "environment": "", "suggested_fix": ""}),
        json.dumps({"tool": "log_window", "argument": "cancel", "reason": "backend error?"}),
        json.dumps({"category": "product_bug", "confidence": 0.9, "root_cause": "KeyError in refund",
                    "evidence_used": [0, 1], "actual_result": "500", "expected_result": "200",
                    "steps_to_reproduce": "cancel", "environment": "API", "suggested_fix": "fix key"}),
    ])
    schemas = []

    def call(ms, messages, schema=None, temperature=0, allow=None):
        schemas.append(schema)
        return next(replies), 5
    monkeypatch.setattr(llm, "_call", call)
    v = agent.decide(case, s, rerun=False)
    assert v.category == Category.product_bug and 1 in v.evidence_used
    assert case.evidence[1].kind == EvidenceKind.log
    assert schemas[1]["properties"]["tool"]["enum"] == ["log_window", "done"]  # only what works


def test_rerun_tool_is_offered_only_when_reruns_are_on(tmp_path):
    s = _reader_settings(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_orders.py").write_text("def test_cancel():\n    assert False\n")
    assert "rerun_detail" in tools.available(_case(), s, rerun=True)
    assert "rerun_detail" not in tools.available(_case(), s, rerun=False)
    assert "test_source" in tools.available(_case(), s)


def test_done_ends_the_investigation(tmp_path, monkeypatch):
    s = _reader_settings(tmp_path, votes=1, two_step=False)
    s.logs.path = str(tmp_path / "app.log")
    (tmp_path / "app.log").write_text(LOG)
    replies = iter([json.dumps({"category": "unknown", "confidence": 0.2, "root_cause": "?",
                                "evidence_used": [0], "actual_result": "", "expected_result": "",
                                "steps_to_reproduce": "", "environment": "", "suggested_fix": ""}),
                    json.dumps({"tool": "done", "argument": "", "reason": "nothing helps"})])
    calls = []
    monkeypatch.setattr(llm, "_call", lambda ms, m, **k: (calls.append(1), (next(replies), 1))[1])
    assert agent.decide(_case(), s, rerun=False).category == Category.unknown
    assert len(calls) == 2


# --- verified fixer ---------------------------------------------------------------------

DIFF = """--- a/tests/test_login.py
+++ b/tests/test_login.py
@@ -1,3 +1,3 @@
 def test_login():
-    button = "#login"
+    button = "#sign-in"
     assert button == "#sign-in"
"""


def test_apply_diff_finds_the_hunk_even_when_lines_moved():
    src = "import os\n\ndef test_login():\n    button = \"#login\"\n    assert button == \"#sign-in\"\n"
    out = patch.apply_diff(src, DIFF)
    assert '"#sign-in"\n    assert' in out and out.startswith("import os")
    assert patch.apply_diff("nothing here\n", DIFF) is None


def test_clean_diff_drops_fences_and_prose():
    assert patch.clean_diff(f"Here you go:\n```diff\n{DIFF}```\n").startswith("--- a/")


def _fix_repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_login.py").write_text(
        'def test_login():\n    button = "#login"\n    assert button == "#sign-in"\n')
    s = _reader_settings(tmp_path)
    s.models.fixer = ModelSettings(provider="ollama_chat", name="coder",
                                   base_url="http://127.0.0.1:11435")
    s.rerun.command = f"{sys.executable} -m pytest {{test_id}} -q -p no:cacheprovider"
    s.rerun.attempts = 1
    return s, _case(title="tests/test_login.py::test_login", test_id="tests/test_login.py::test_login")


def test_a_patch_that_makes_the_test_pass_is_kept(tmp_path, monkeypatch):
    s, case = _fix_repo(tmp_path)
    monkeypatch.setattr(llm, "_call", lambda ms, m, **k: (DIFF, 1))
    diff, status = patch.fix(case, s, rerun=True)
    assert diff.startswith("--- a/") and status.startswith("verified")
    assert '"#login"' in (tmp_path / "tests" / "test_login.py").read_text()  # yours untouched


def test_a_patch_that_does_not_help_is_dropped(tmp_path, monkeypatch):
    s, case = _fix_repo(tmp_path)
    bad = DIFF.replace('+    button = "#sign-in"', '+    button = "#nope"')
    monkeypatch.setattr(llm, "_call", lambda ms, m, **k: (bad, 1))
    diff, status = patch.fix(case, s, rerun=True)
    assert diff == "" and "still failed" in status


def test_no_rerun_means_not_verified(tmp_path, monkeypatch):
    s, case = _fix_repo(tmp_path)
    monkeypatch.setattr(llm, "_call", lambda ms, m, **k: (DIFF, 1))
    diff, status = patch.fix(case, s, rerun=False)
    assert diff and status.startswith("not verified")


# --- traces -----------------------------------------------------------------------------

def test_trace_bodies_are_read_from_resources_and_redacted(tmp_path):
    folder = tmp_path / "t1"
    folder.mkdir()
    snap = {"type": "resource-snapshot", "snapshot": {
        "request": {"method": "POST", "url": "http://shop/api/auth/login", "headers": [],
                    "postData": {"text": "", "_file": "resources/req.json"}},
        "response": {"status": 200, "content": {"mimeType": "application/json",
                                                "_sha1": "resp.json"}}}}
    with zipfile.ZipFile(folder / "trace.zip", "w") as z:
        z.writestr("trace.network", json.dumps(snap))
        z.writestr("resources/req.json", json.dumps({"username": "anna", "password": "anna-pass"}))
        z.writestr("resources/resp.json", json.dumps({"token": "tok-1"}))
    [flow] = TraceReader().read(folder / "trace.zip", ["password", "token"])
    step = flow.steps[0]
    assert step.body == {"username": "anna", "password": "[REDACTED]"}
    assert step.response == {"token": "[REDACTED]"}


# --- screenshot redaction ---------------------------------------------------------------

def _words(*lines):
    out = []
    for n, line in enumerate(lines):
        x = 0
        for w in line.split():
            out.append((w, (x, n * 20, x + 10 * len(w), n * 20 + 15), (1, 1, n)))
            x += 10 * len(w) + 5
    return out


def test_secret_words_are_found_by_rule_label_and_line():
    words = _words("Signed in as anna", "password: hunter2", "Card 4111 1111 1111 1111",
                   "Authorization Bearer abc.def")
    boxes = redact_image.secret_boxes(words, ["password"])
    masked = {w[0] for w in words if w[1] in boxes}
    assert "hunter2" in masked and "anna" not in masked
    assert {"4111", "1111"} <= masked and "Card" not in masked
    assert "abc.def" in masked or "Bearer" in masked


def test_upload_uses_redacted_copies_or_leaves_out_when_required(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image

    shot = tmp_path / "shot.png"
    Image.new("RGB", (200, 60), "white").save(shot)
    draft = Draft(kind=DraftKind.ticket, title="t", body="b", images=[str(shot)])
    s = Settings()
    s.screenshots.path = str(tmp_path / "red")

    monkeypatch.setattr(redact_image, "ocr_words", lambda p: None)  # no OCR here
    assert redact_image.for_upload(draft, s).images == [str(shot)]  # auto: as before
    s.screenshots.redact = "required"
    assert redact_image.for_upload(draft, s).images == []

    monkeypatch.setattr(redact_image, "ocr_words", lambda p: _words("password: hunter2"))
    [copy] = redact_image.for_upload(draft, s).images
    assert copy != str(shot) and Image.open(copy).getpixel((100, 5)) == (0, 0, 0)


def test_real_ocr_masks_secrets_and_keeps_the_rest(tmp_path):
    """Runs only where pytesseract and the tesseract program are installed."""
    pytesseract = pytest.importorskip("pytesseract")
    try:
        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001
        pytest.skip("tesseract is not installed")
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default(size=28)
    img = Image.new("RGB", (900, 220), "white")
    draw = ImageDraw.Draw(img)
    for i, text in enumerate(["Signed in as anna", "password: hunter2", "Card 4111 1111 1111 1111"]):
        draw.text((20, 20 + i * 60), text, fill="black", font=font)
    shot = tmp_path / "shot.png"
    img.save(shot)
    redacted, masked = redact_image.redact_image(str(shot), ["password"], tmp_path / "out")
    words = [w[0] for w in redact_image.ocr_words(redacted)]
    assert masked >= 3  # tesseract may read the card number as one or several words
    assert "hunter2" not in words and not any(w.startswith(("4111", "1111")) for w in words)
    assert "anna" in words and "password:" in words
