"""A project-scoped Ollama server: models live under the project's .tessella/,
not the user's shared ~/.ollama, so a project's model footprint stays with it.

The project is found from wherever Tessella runs: the nearest folder, upward, whose
`.tessella/models` exists. So `examples/demo_shop` inside the repository uses the
repository's models and the same server, instead of starting a second copy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from tessella.config import OllamaLocal

# Where a system-wide or per-user Ollama keeps its models, for `tessella models import`.
SYSTEM_MODEL_DIRS = ("/usr/share/ollama/.ollama/models", "/var/lib/ollama/.ollama/models",
                     "~/.ollama/models")


def project_root(cfg: OllamaLocal, start: str | Path = ".") -> Path:
    """The folder that holds this project's models: `start` or the nearest parent with
    `<models_dir>`; `start` itself when none has one yet (the first pull creates it)."""
    if Path(cfg.models_dir).is_absolute():
        return Path(start).resolve()
    here = Path(start).resolve()
    for folder in (here, *here.parents):
        if (folder / cfg.models_dir).is_dir():
            return folder
    return here


def models_dir(cfg: OllamaLocal, start: str | Path = ".") -> Path:
    path = Path(cfg.models_dir).expanduser()
    return path if path.is_absolute() else project_root(cfg, start) / path


def binary(start: str | Path = ".", cfg: OllamaLocal | None = None) -> str | None:
    """The Ollama program: the project's own copy (.tessella/bin, put there by
    `tessella install --from`) first, then the one on PATH."""
    root = project_root(cfg or OllamaLocal(), start)
    for local in (root / ".tessella/bin/ollama", root / ".tessella/bin/bin/ollama"):
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    return shutil.which("ollama")


def binary_available(start: str | Path = ".") -> bool:
    return binary(start) is not None


def is_up(port: int) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{port}/api/tags", timeout=0.5)
        return True
    except httpx.HTTPError:
        return False


def _env(cfg: OllamaLocal, repo: str | Path) -> dict[str, str]:
    return {**os.environ, "OLLAMA_MODELS": str(models_dir(cfg, repo)),
            "OLLAMA_HOST": f"127.0.0.1:{cfg.port}"}


def ensure_running(cfg: OllamaLocal, repo: str | Path = ".", wait: float = 10.0) -> None:
    """Start the project-scoped `ollama serve` if it isn't already up. Idempotent."""
    if is_up(cfg.port):
        return
    program = binary(repo, cfg)
    if program is None:
        raise RuntimeError("Ollama is not installed (not on PATH, not in .tessella/bin): "
                           "install it from https://ollama.com/download or bring it in a "
                           "bundle (tessella bundle --ollama)")
    root = project_root(cfg, repo)
    models_dir(cfg, repo).mkdir(parents=True, exist_ok=True)
    tessella_dir = root / ".tessella"
    tessella_dir.mkdir(parents=True, exist_ok=True)
    log = (tessella_dir / "ollama.log").open("a")
    proc = subprocess.Popen(
        [program, "serve"], env=_env(cfg, repo), stdout=log, stderr=log,
        start_new_session=True,
    )
    (tessella_dir / "ollama.pid").write_text(str(proc.pid))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_up(cfg.port):
            return
        time.sleep(0.3)
    raise RuntimeError(
        f"ollama serve did not come up on port {cfg.port} within {wait}s; see {tessella_dir / 'ollama.log'}"
    )


def pull(model: str, cfg: OllamaLocal, repo: str | Path = ".") -> None:
    """Pull a model into the project-scoped server. Streams Ollama's own progress."""
    subprocess.run([binary(repo, cfg) or "ollama", "pull", model], env=_env(cfg, repo),
                   check=True)


def find_installed(model: str, sources: list[str] | None = None) -> Path | None:
    """The first models folder (yours or the system service's) that has `model`."""
    from tessella.bundle import manifest_path

    for raw in [os.environ.get("OLLAMA_MODELS", ""), *(sources or SYSTEM_MODEL_DIRS)]:
        if not raw:
            continue
        folder = Path(raw).expanduser()
        try:
            if manifest_path(folder, model).is_file():
                return folder
        except OSError:  # a folder we may not read
            continue
    return None


def import_model(model: str, source: Path, cfg: OllamaLocal,
                 repo: str | Path = ".") -> tuple[int, int]:
    """Copy one model (manifest and blobs) from another Ollama models folder into the
    project's, without downloading. A blob already there with the same size is kept.
    Returns (files copied, bytes copied)."""
    from tessella.bundle import model_files

    target = models_dir(cfg, repo)
    copied = size = 0
    for rel in model_files(source, model):
        dest = target / rel
        src = source / rel
        if dest.is_file() and dest.stat().st_size == src.stat().st_size and "blobs" in rel.parts:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(src, tmp)  # a cut-off copy never looks like a finished blob
        tmp.replace(dest)
        copied += 1
        size += dest.stat().st_size
    return copied, size
