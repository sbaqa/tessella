import json

import httpx
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
    assert "| test_login | 1 | pending | timeout | not filed |" in body
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
