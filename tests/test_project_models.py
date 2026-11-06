"""The project's own Ollama: models in the repository's .tessella/models, found from any
subfolder, and copied in from a system Ollama without downloading."""

import hashlib
import json
from pathlib import Path

import pytest

from tessella import bundle
from tessella.config import OllamaLocal, Settings
from tessella.integrations import ollama_runtime


def _ollama_store(root: Path, model: str = "qwen3.5:4b") -> Path:
    blobs = root / "blobs"
    blobs.mkdir(parents=True)
    digests = []
    for content in (b"gguf" * 1000, b'{"model_family": "qwen35"}'):
        digest = hashlib.sha256(content).hexdigest()
        (blobs / f"sha256-{digest}").write_bytes(content)
        digests.append(f"sha256:{digest}")
    manifest = bundle.manifest_path(root, model)
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"config": {"digest": digests[1]},
                                    "layers": [{"digest": digests[0]}]}))
    return root


def test_defaults_point_at_the_projects_own_server():
    s = Settings()
    assert s.ollama.enabled and s.ollama.port == 11435
    assert s.models.reader.name == "qwen3.5:4b"
    assert s.models.reader.base_url == "http://127.0.0.1:11435"
    assert s.network.allow == ["localhost:11435"]


def test_subfolders_share_the_repository_models(tmp_path):
    repo = tmp_path / "tessella"
    (repo / ".tessella/models").mkdir(parents=True)
    demo = repo / "examples/demo_shop"
    (demo / ".tessella").mkdir(parents=True)  # its own store, but no models of its own
    cfg = OllamaLocal()
    assert ollama_runtime.project_root(cfg, demo) == repo
    assert ollama_runtime.models_dir(cfg, demo) == repo / ".tessella/models"
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    assert ollama_runtime.models_dir(cfg, elsewhere) == elsewhere / ".tessella/models"


def test_the_projects_own_ollama_program_comes_first(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_runtime.shutil, "which", lambda _: "/usr/bin/ollama")
    assert ollama_runtime.binary(tmp_path) == "/usr/bin/ollama"
    local = tmp_path / ".tessella/bin/ollama"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    (tmp_path / ".tessella/models").mkdir()
    assert ollama_runtime.binary(tmp_path) == str(local)


def test_import_copies_a_model_from_the_system_ollama(tmp_path):
    system = _ollama_store(tmp_path / "usr-share-ollama")
    repo = tmp_path / "repo"
    repo.mkdir()
    assert ollama_runtime.find_installed("qwen3.5:4b", [str(system)]) == system
    assert ollama_runtime.find_installed("qwen3.5:9b", [str(system)]) is None
    files, size = ollama_runtime.import_model("qwen3.5:4b", system, OllamaLocal(), repo)
    assert files == 3 and size > 4000
    assert bundle.manifest_path(repo / ".tessella/models", "qwen3.5:4b").is_file()
    again = ollama_runtime.import_model("qwen3.5:4b", system, OllamaLocal(), repo)
    assert again[0] == 1  # only the small manifest; blobs already there are kept


def test_models_import_command(tmp_path, monkeypatch):
    typer_testing = pytest.importorskip("typer.testing")
    from tessella.cli import app

    system = _ollama_store(tmp_path / "system")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text("storage: {path: t.sqlite}\n")
    result = typer_testing.CliRunner().invoke(
        app, ["models", "import", "--from", str(system), "-c", "c.yaml"])
    assert result.exit_code == 0, result.output
    assert "qwen3.5:4b" in result.output
    assert bundle.manifest_path(tmp_path / ".tessella/models", "qwen3.5:4b").is_file()
    missing = typer_testing.CliRunner().invoke(app, ["models", "import", "qwen3.5:122b",
                                                     "--from", str(tmp_path / "nowhere")])
    assert missing.exit_code == 1


def test_a_config_on_another_server_does_not_start_the_project_one():
    from tessella.cli import _uses_project_ollama

    s = Settings()
    assert _uses_project_ollama(s)
    s.models.reader.base_url = "http://localhost:11434"
    s.models.vision = None
    assert not _uses_project_ollama(s)
