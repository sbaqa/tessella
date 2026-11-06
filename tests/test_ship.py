"""Phase 4: offline mode, the offline bundle, the MCP server and the team setup."""

import asyncio
import base64
import hashlib
import io
import json
import socket
import subprocess
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest

from tessella import bundle, egress, net
from tessella.config import ModelSettings, Settings
from tessella.memory.store import Store
from tessella.models import (
    Case,
    Category,
    Draft,
    DraftKind,
    Evidence,
    EvidenceKind,
    Origin,
    Verdict,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def offline():
    egress.attempts.clear()
    egress.enable()
    yield
    egress.disable()
    egress.attempts.clear()


# --- offline mode ---------------------------------------------------------------------------

def test_offline_mode_refuses_any_outside_connection(offline):
    with socket.socket() as raw, pytest.raises(net.EgressBlocked):
        raw.connect(("203.0.113.9", 443))  # a raw socket, not net.client
    with pytest.raises(net.EgressBlocked):
        socket.getaddrinfo("example.com", 443)  # a DNS lookup
    with pytest.raises(net.EgressBlocked):
        httpx.get("http://198.51.100.1/")  # a library's own client
    assert egress.attempts == ["203.0.113.9:443", "example.com", "198.51.100.1"]


def test_offline_mode_lets_loopback_through(offline):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    with socket.create_connection(server.getsockname(), timeout=1):
        pass
    server.close()
    socket.getaddrinfo("localhost", 80)
    assert egress.attempts == []


def test_offline_allow_names_a_server_inside_your_network(offline):
    egress.enable(["198.51.100.7:8000"])
    sock = socket.socket()
    sock.setblocking(False)
    try:
        sock.connect(("198.51.100.7", 8000))  # not refused by the hook (nothing listens)
    except (BlockingIOError, OSError) as exc:
        assert not isinstance(exc, net.EgressBlocked)
    finally:
        sock.close()
    with pytest.raises(net.EgressBlocked):
        socket.create_connection(("198.51.100.8", 8000), timeout=1)


def test_offline_triage_fails_when_something_reaches_out(tmp_path):
    """A log source outside the machine is allowed by network.allow, so net.client lets the
    call through; offline mode still refuses it, and the run exits 3 instead of passing."""
    typer_testing = pytest.importorskip("typer.testing")
    from tessella.cli import app

    report = tmp_path / "junit.xml"
    report.write_text('<testsuite timestamp="2026-10-30T10:00:00"><testcase classname="a" '
                      'name="t1" time="1"><failure message="boom"/></testcase></testsuite>')
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"storage: {{path: {tmp_path / 't.sqlite'}}}\n"
                   "inputs: {traces: []}\n"
                   "logs: {source: loki, url: 'http://logs.example.com:3100', query: '{app=\"x\"}'}\n"
                   "network: {allow: ['logs.example.com:3100']}\n")
    try:
        result = typer_testing.CliRunner().invoke(
            app, ["--offline", "triage", str(report), "-c", str(cfg), "--no-rerun", "--no-model"])
    finally:
        egress.disable()
    assert result.exit_code == 3, result.output
    assert "logs.example.com" in result.output
    egress.attempts.clear()


# --- offline bundle -------------------------------------------------------------------------

def _wheel(dest: Path, name="tinypkg", version="1.0") -> Path:
    """A minimal valid pure-Python wheel."""
    path = dest / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    files = {f"{name}/__init__.py": "VALUE = 42\n",
             f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
             f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                              "Tag: py3-none-any\n"}
    record = []
    for rel, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=")
        record.append(f"{rel},sha256={digest.decode()},{len(text)}")
    files[f"{info}/RECORD"] = "\n".join(record + [f"{info}/RECORD,,"]) + "\n"
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in files.items():
            z.writestr(rel, text)
    return path


def _models(root: Path) -> Path:
    """An Ollama models folder holding one model: a manifest and two blobs."""
    blobs = root / "blobs"
    blobs.mkdir(parents=True)
    digests = []
    for content in (b"weights" * 100, b'{"config": 1}'):
        digest = hashlib.sha256(content).hexdigest()
        (blobs / f"sha256-{digest}").write_bytes(content)
        digests.append(f"sha256:{digest}")
    manifest = bundle.manifest_path(root, "tiny:1b")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"config": {"digest": digests[1]},
                                    "layers": [{"digest": digests[0]}]}))
    return root


def _build(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'tessella'\n")

    def fetch(project, extras, dest, python_version, platform):
        dest.mkdir(parents=True)
        _wheel(dest)

    out = tmp_path / "bundle.tar"
    bundle.build(out, project, ["queue"], ["tiny:1b"], _models(tmp_path / "models"),
                 fetch=fetch)
    return out


def test_model_paths_follow_ollama_layout(tmp_path):
    assert bundle.manifest_path(tmp_path, "qwen3.5:4b") == \
        tmp_path / "manifests/registry.ollama.ai/library/qwen3.5/4b"
    assert bundle.manifest_path(tmp_path, "team/coder") == \
        tmp_path / "manifests/registry.ollama.ai/team/coder/latest"
    assert bundle.manifest_path(tmp_path, "hf.co/org/m:Q4_K_M") == \
        tmp_path / "manifests/hf.co/org/m/Q4_K_M"
    with pytest.raises(bundle.BundleError):
        bundle.model_files(tmp_path, "missing:1b")


def test_bundle_installs_offline_into_a_new_venv(tmp_path):
    out = _build(tmp_path)
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert "tessella-bundle/install.py" in names and "tessella-bundle/MANIFEST.json" in names
    models_dir = tmp_path / "installed-models"
    result = bundle.install(out, venv=tmp_path / "venv", models_dir=models_dir,
                            say=lambda *_: None)
    check = subprocess.run([result["python"], "-c", "import tinypkg; print(tinypkg.VALUE)"],
                           check=False, capture_output=True, text=True)
    assert check.stdout.strip() == "42"
    assert bundle.manifest_path(models_dir, "tiny:1b").is_file()
    assert len(list((models_dir / "blobs").iterdir())) == 2
    lock = result["manifest"]["files"]["requirements.lock"]
    assert len(lock) == 64  # the lock itself is hashed in the manifest


def test_a_changed_bundle_installs_nothing(tmp_path):
    out = _build(tmp_path)
    tampered = tmp_path / "tampered.tar"
    with tarfile.open(out) as src, tarfile.open(tampered, "w") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read() if member.isfile() else None
            if member.name.endswith("install.py"):
                data += b"\n# changed\n"
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
    with pytest.raises(bundle.BundleError, match="changed: install.py"):
        bundle.install(tampered, venv=tmp_path / "venv", models_dir=tmp_path / "m",
                       say=lambda *_: None)
    assert not (tmp_path / "venv").exists() and not (tmp_path / "m").exists()


def test_install_py_runs_with_the_standard_library_only(tmp_path):
    """install.py is this module copied into the bundle: it must not import Tessella."""
    out = _build(tmp_path)
    with tarfile.open(out) as tar:
        tar.extractall(tmp_path / "x", filter="data")
    script = tmp_path / "x/tessella-bundle/install.py"
    proc = subprocess.run(["python3", "-I", "-S", str(script), "--help"],
                          check=False, capture_output=True, text=True)
    assert proc.returncode == 0 and "offline bundle" in proc.stdout


# --- MCP server -----------------------------------------------------------------------------

def _store(tmp_path) -> Store:
    store = Store(tmp_path / "t.sqlite")
    case = Case(id="c1", origin=Origin.test_failure, title="test_checkout",
                test_id="tests/test_checkout.py::test_checkout", message="KeyError: 'TILE25'",
                stack='File "src/shop/cart.py", line 12, in total', signature="sig1",
                evidence=[Evidence(kind=EvidenceKind.log, summary="backend log",
                                   excerpt="KeyError: 'TILE25'")],
                verdict=Verdict(category=Category.product_bug, confidence=0.8,
                                root_cause="cart has no price for TILE25", evidence_used=[0]),
                draft=Draft(kind=DraftKind.ticket, title="Checkout fails", body="..."))
    store.save_case(case)
    store.save_case(Case(id="c2", origin=Origin.test_failure, title="test_login",
                         message="timeout", signature="sig2"))
    return store


def test_mcp_tools_serve_cases_and_evidence(tmp_path):
    from tessella.mcp_server import Tools

    tools = Tools(Settings(), _store(tmp_path))
    rows = tools.list_cases(category="product_bug")
    assert [r["id"] for r in rows] == ["c1"] and rows[0]["category"] == "product_bug"
    case = tools.get_case("c1")
    assert case["evidence"][0]["excerpt"] == "KeyError: 'TILE25'"
    assert case["verdict"]["evidence_used"] == [0] and case["draft"]["title"] == "Checkout fails"
    assert [r["id"] for r in tools.cases_for_file("src/shop/cart.py")] == ["c1"]
    assert [r["id"] for r in tools.cases_for_file("test_checkout.py")] == ["c1"]
    assert [r["id"] for r in tools.find_cases("timeout")] == ["c2"]
    assert "error" in tools.get_case("nope")


def test_mcp_can_serve_summaries_only(tmp_path):
    from tessella.mcp_server import Tools

    s = Settings()
    s.mcp.excerpts = False
    case = Tools(s, _store(tmp_path)).get_case("c1")
    assert "excerpt" not in case["evidence"][0] and case["stack"] == ""


def test_mcp_server_lists_read_only_tools(tmp_path):
    pytest.importorskip("mcp")
    from tessella.mcp_server import build

    server = build(Settings(), _store(tmp_path))
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"list_cases", "get_case", "find_cases", "cases_for_file", "recent_runs"}


# --- team setup -----------------------------------------------------------------------------

def test_model_server_key_comes_from_the_environment(monkeypatch):
    respx = pytest.importorskip("respx")
    from tessella.integrations import local_llm

    ms = ModelSettings(provider="hosted_vllm", name="m", base_url="http://gpu.lan:8000/v1",
                       api_key_env="TEAM_MODEL_KEY")
    with pytest.raises(RuntimeError, match="TEAM_MODEL_KEY"):
        local_llm.headers(ms)
    monkeypatch.setenv("TEAM_MODEL_KEY", "k3y")
    with respx.mock:
        route = respx.post("http://gpu.lan:8000/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]}))
        text, _ = local_llm.chat(ms, [{"role": "user", "content": "x"}], ["gpu.lan:8000"])
    assert text == "hi"
    assert route.calls[0].request.headers["authorization"] == "Bearer k3y"


def _team_settings(tmp_path, monkeypatch) -> Settings:
    s = Settings()
    s.storage.path = str(tmp_path / "t.sqlite")
    s.team.reviewers = {"anna": "PW_ANNA", "bob": "PW_BOB"}
    monkeypatch.setenv("PW_ANNA", "s3cret")
    monkeypatch.setenv("PW_BOB", "other")
    return s


def test_team_queue_needs_a_signed_in_reviewer(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from tessella.queue.app import create_app

    s = _team_settings(tmp_path, monkeypatch)
    _store(tmp_path)
    app = create_app(s)
    client = TestClient(app)
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("anna", "wrong")).status_code == 401
    assert client.get("/", auth=("mallory", "s3cret")).status_code == 401
    page = client.get("/", auth=("anna", "s3cret"))
    assert page.status_code == 200 and "Signed in as <strong>anna</strong>" in page.text
    client.post("/cases/c2/decide", auth=("anna", "s3cret"), follow_redirects=False,
                data={"action": "reject", "reason": "flaky", "csrf": app.state.csrf,
                      "by": "bob"})
    _, decision = Store(s.storage.path).recent_decisions()[0]
    assert decision.by == "anna"  # the signed-in reviewer, not what the form claims


def test_queue_will_not_listen_beyond_this_machine_without_reviewers(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from tessella.queue.app import serve

    s = Settings()
    s.storage.path = str(tmp_path / "t.sqlite")
    with pytest.raises(SystemExit, match="without reviewers"):
        serve(s, host="0.0.0.0")
    s = _team_settings(tmp_path, monkeypatch)
    monkeypatch.delenv("PW_BOB")
    with pytest.raises(SystemExit, match="bob"):
        serve(s, host="0.0.0.0")
