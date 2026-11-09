import json

import httpx
import pytest
import respx

from tessella.integrations import ci_comment
from tessella.models import Case, Category, Origin, Verdict

ALLOW = ["api.github.com", "gitlab.example.com"]


def _cases():
    bug = Case(id="a", origin=Origin.test_failure, title="test_total | promo", message="boom",
               signature="s1")
    bug.verdict = Verdict(category=Category.product_bug, confidence=0.82, root_cause="promo ignored")
    flaky = Case(id="b", origin=Origin.test_failure, title="test_login", message="timeout",
                 signature="s2")
    return [bug, flaky]


def test_summary_has_marker_counts_and_one_row_per_cluster():
    body = ci_comment.summary(_cases(), "https://queue.lan")
    assert body.startswith(ci_comment.MARKER)
    assert "| test_total \\| promo | 1 | product_bug (82%) | promo ignored | not filed |" in body
    assert "| test_login | 1 | Timeout, not judged | timeout | not filed |" in body
    assert "https://queue.lan" in body
    assert "No failures" in ci_comment.summary([])


def test_detect_github_pull_request(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}))
    env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/r", "GITHUB_REF": "refs/heads/x",
           "GITHUB_EVENT_PATH": str(event), "GITHUB_TOKEN": "t"}
    t = ci_comment.detect(env)
    assert t.list_url == "https://api.github.com/repos/o/r/issues/7/comments"
    assert ci_comment.host_of(t) == "api.github.com"
    assert ci_comment.detect({**env, "GITHUB_TOKEN": ""}) is None
    assert "token" in ci_comment.why_not({**env, "GITHUB_TOKEN": ""})
    push = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/r", "GITHUB_REF": "refs/heads/main",
            "GITHUB_TOKEN": "t"}
    assert ci_comment.detect(push) is None
    assert "not for a pull request" in ci_comment.why_not(push)
    assert ci_comment.detect({}) is None


def test_detect_gitlab_merge_request():
    env = {"GITLAB_CI": "true", "CI_PROJECT_ID": "group/app", "CI_MERGE_REQUEST_IID": "3",
           "CI_API_V4_URL": "https://gitlab.example.com/api/v4", "GITLAB_TOKEN": "t"}
    t = ci_comment.detect(env)
    assert t.list_url == "https://gitlab.example.com/api/v4/projects/group%2Fapp/merge_requests/3/notes"
    assert t.edit_method == "PUT" and t.headers == {"PRIVATE-TOKEN": "t"}


@respx.mock
def test_post_creates_then_edits_its_own_comment():
    env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/r", "GITHUB_REF": "refs/pull/7/merge",
           "GITHUB_TOKEN": "t"}
    t = ci_comment.detect(env)
    listing = respx.get("https://api.github.com/repos/o/r/issues/7/comments")
    create = respx.post("https://api.github.com/repos/o/r/issues/7/comments").mock(
        return_value=httpx.Response(201, json={"id": 1}))
    listing.mock(return_value=httpx.Response(200, json=[{"id": 5, "body": "someone else"}]))
    assert ci_comment.post(t, "body", ALLOW) == "created"
    assert create.called
    edit = respx.patch("https://api.github.com/repos/o/r/issues/comments/9").mock(
        return_value=httpx.Response(200, json={"id": 9}))
    listing.mock(return_value=httpx.Response(200, json=[{"id": 9, "body": ci_comment.MARKER + "old"}]))
    assert ci_comment.post(t, "new", ALLOW) == "updated"
    assert json.loads(edit.calls.last.request.content) == {"body": "new"}


def test_ci_command_comments_only_when_asked(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from tessella import cli
    from tessella import pipeline

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline, "triage", lambda s, **kw: pipeline.RunResult(cases=_cases()))
    monkeypatch.setattr(cli, "_start_project_ollama", lambda s: None)
    posted = []
    monkeypatch.setattr(ci_comment, "post", lambda t, body, allow: posted.append(body) or "created")
    for k in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(k, raising=False)
    runner = CliRunner()
    out = runner.invoke(cli.app, ["ci", "--comment"])
    assert out.exit_code == 0 and "Not commenting" in out.output and not posted
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/7/merge")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    out = runner.invoke(cli.app, ["ci", "--comment"])
    assert "add api.github.com to network.allow" in out.output and not posted
    (tmp_path / "tessella.yaml").write_text("network:\n  allow: [api.github.com]\n")
    out = runner.invoke(cli.app, ["ci", "--comment"])
    assert out.exit_code == 0 and len(posted) == 1, out.output
    out = runner.invoke(cli.app, ["ci"])  # ci.comment defaults to off
    assert len(posted) == 1


class _Result:
    def __init__(self, cases, **kw):
        self.cases, self.clusters = cases, {}
        for c in cases:
            self.clusters.setdefault(c.signature, []).append(c)
        self.decisions, self.jira_matches, self.reopened = {}, {}, {}
        self.history, self.used_model, self.board_checked, self.out_of_time = {}, True, False, 0
        self.__dict__.update(kw)


def _failure(sig="s", title="test_a", message="AssertionError: assert 1 == 2"):
    return Case(id=title, origin=Origin.test_failure, title=title, message=message, signature=sig)


def test_no_model_comment_groups_names_the_kind_and_says_how_to_add_a_model():
    cases = [_failure("s1", "test_a", "Timeout 3000ms exceeded."), _failure("s1", "test_b", "Timeout 3000ms exceeded."),
             _failure("s2", "test_c", "HTTP status 500 from server")]
    body = ci_comment.summary(cases, result=_Result(cases, used_model=False, board_checked=True))
    assert "Timeout, not judged" in body and "Server error (5xx), not judged" in body
    assert "no ticket for this failure yet" in body
    assert "This runner has no model" in body
    assert "**2 tests fail this way:**" in body and "- test_b" in body


def test_comment_says_what_the_board_has_without_a_model():
    from datetime import datetime

    from tessella.models import Action, Decision

    case = _failure("known")
    case.verdict = Verdict(category=Category.unknown, confidence=1.0, root_cause="Already filed as KAN-7.")
    filed = Decision(action=Action.approve, by="", at=datetime(2026, 11, 8), board="jira", ticket_key="KAN-7")
    body = ci_comment.summary([case], result=_Result([case], used_model=False, decisions={"known": filed}))
    assert "| already filed |" in body and "already filed as KAN-7" in body and "unknown (100%)" not in body


def test_comment_history_time_limit_and_run_link():
    cases = [_failure("old", "test_old"), _failure("fresh", "test_fresh")]
    body = ci_comment.summary(cases, result=_Result(cases, out_of_time=2, history={"old": (3, 5), "fresh": (0, 5)}),
                              run_link="https://ci/run/1")
    assert "Seen before" in body and "3 of last 5 runs" in body and "| new |" in body
    assert "Time limit reached: 2 failure(s)" in body and "no model" not in body
    assert "[CI run](https://ci/run/1)" in body


def test_comment_drafted_bug_folds_its_details_and_escapes_html():
    from tessella.models import Draft, DraftKind

    case = _failure()
    case.verdict = Verdict(category=Category.product_bug, confidence=0.9, root_cause="rounding")
    case.draft = Draft(kind=DraftKind.ticket, title="t", body="b", actual_result="</details><b>x",
                       expected_result="ok", suggested_fix="round half up", patch_status="verified")
    body = ci_comment.summary([case], result=_Result([case]))
    assert "**Suggested fix:** round half up" in body and "**Checked fix:** verified" in body
    assert "&lt;/details>" in body and body.count("</details>") == 1


def test_comment_is_capped():
    cases = [_failure(f"s{i}", f"test_{i}") for i in range(80)]
    body = ci_comment.summary(cases, result=_Result(cases))
    assert "And 30 more failures" in body and "test_79" not in body


def test_run_url():
    assert ci_comment.run_url({"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9"}) == \
        "https://github.com/o/r/actions/runs/9"
    assert ci_comment.run_url({"GITLAB_CI": "true", "CI_PIPELINE_URL": "https://g/p/1"}) == "https://g/p/1"
    assert ci_comment.run_url({}) is None


def _digest():
    bug = _failure("s1", "test_total", "AssertionError")
    bug.verdict = Verdict(category=Category.product_bug, confidence=0.9, root_cause="<b>rounding</b> in cart")
    from tessella.models import Draft, DraftKind

    bug.draft = Draft(kind=DraftKind.ticket, title="t", body="b", actual_result="1", expected_result="2",
                      suggested_fix="round half up")
    other = _failure("s2", "test_login", "Timeout 3000ms exceeded.")
    return ci_comment.digest([bug, other], result=_Result([bug, other], used_model=False))


CTX = {"repo": "o/r", "change": "PR #2", "change_url": "https://github.com/o/r/pull/2", "branch": "b", "commit": "abc1234"}
LINKS = [("Tessella report", "https://github.com/o/r/actions/runs/1/artifacts/2"), ("CI run", "https://github.com/o/r/actions/runs/1")]


def test_reports_section_links_directly_to_the_artifacts():
    body = ci_comment.markdown(_digest(), "https://ci/run", [("Tessella report", "https://x/artifacts/2"), ("Allure report", "https://x/artifacts/3")])
    assert "**Reports:** [Tessella report](https://x/artifacts/2) · [Allure report](https://x/artifacts/3) · [CI run](https://ci/run)" in body
    assert body.index("**Reports:**") < body.index("| Failure |")


def test_digest_survives_a_file():
    d = _digest()
    assert json.loads(json.dumps(d)) == d


def test_email_is_structured_and_escapes_everything():
    from tessella.integrations import ci_email

    d = _digest()
    html = ci_email.html_body(d, CTX, LINKS)
    assert "<b>rounding" not in html and "&lt;b&gt;rounding" in html
    assert 'href="https://github.com/o/r/actions/runs/1/artifacts/2"' in html and ">Tessella report</a>" in html
    assert "Product bug" in html and "Suggested fix" in html and "round half up" in html and "o/r" in html
    text = ci_email.text_body(d, CTX, LINKS)
    assert "Tessella report: https://github.com/o/r/actions/runs/1/artifacts/2" in text and "REPORTS" in text
    subject = ci_email.subject(d, CTX)
    assert subject.startswith("[Tessella] o/r PR #2: 2 failed tests:") and "1 product bug" in subject


def test_email_never_leaves_for_a_host_outside_the_allowlist(monkeypatch):
    import smtplib

    from tessella.config import Email
    from tessella.integrations import ci_email

    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("connected"))
    with pytest.raises(PermissionError, match="network.allow"):
        ci_email.send(Email(to=["a@b.c"], host="smtp.evil.test"), "s", "t", "<p>h</p>", ["smtp.gmail.com"])


def test_email_logs_in_from_the_environment_and_sends_both_parts(monkeypatch):
    import smtplib

    from tessella.config import Email
    from tessella.integrations import ci_email

    seen = {}

    class Fake:
        def __init__(self, host, port, timeout):
            seen["at"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            seen["tls"] = True

        def login(self, user, password):
            seen["login"] = (user, password)

        def send_message(self, message):
            seen["message"] = message

    monkeypatch.setattr(smtplib, "SMTP", Fake)
    monkeypatch.setenv("SMTP_USERNAME", "me@x.y")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    ci_email.send(Email(to=["a@b.c", "d@e.f"], host="smtp.x.y"), "subject", "plain", "<p>rich</p>", ["smtp.x.y"])
    m = seen["message"]
    assert seen["at"] == ("smtp.x.y", 587) and seen["tls"] and seen["login"] == ("me@x.y", "secret")
    assert m["To"] == "a@b.c, d@e.f" and m["From"] == "me@x.y" and m["Subject"] == "subject"
    assert {p.get_content_type() for p in m.iter_parts()} == {"text/plain", "text/html"}


def test_notify_never_fails_and_writes_the_ci_summary_page(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from tessella import cli

    digest = tmp_path / "d.json"
    digest.write_text(json.dumps(_digest()))
    page = tmp_path / "summary.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    r = CliRunner().invoke(cli.app, ["notify", str(digest), "--link", "Tessella report=https://x/1", "--link", "Empty=", "--email"])
    assert r.exit_code == 0 and "Not emailing" in r.output
    text = page.read_text()
    assert "[Tessella report](https://x/1)" in text and "Empty" not in text
    missing = CliRunner().invoke(cli.app, ["notify", str(tmp_path / "none.json")])
    assert missing.exit_code == 0 and "Tessella error" in missing.output


def test_a_failed_model_install_is_not_reported_as_no_model(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from tessella import cli

    (tmp_path / "junit.xml").write_text(
        '<testsuite tests="1" failures="1"><testcase classname="t" name="a">'
        '<failure message="boom">boom</failure></testcase></testsuite>')
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli.app, ["ci", "--no-model", "--no-rerun", "--model-failed", "--digest", "d.json", "junit.xml"])
    assert r.exit_code == 0
    note = json.loads((tmp_path / "d.json").read_text())["note"]
    assert note.startswith("The model could not be installed")
    assert "Add a model" not in ci_comment.markdown(json.loads((tmp_path / "d.json").read_text()))
