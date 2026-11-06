"""Offline bundle: everything a machine with no internet needs to run Tessella locally.

`tessella bundle` runs on a connected machine and writes one .tar holding
- wheelhouse/: Tessella and every dependency as wheels, for one Python version and platform;
- requirements.lock: each of those wheels pinned by version and SHA-256;
- models/: the Ollama model files your local roles use (manifests and blobs);
- bin/: optionally the Ollama program itself;
- install.py: this file, which needs only the Python standard library;
- MANIFEST.json: the SHA-256 of every file above.

On the offline machine, `python3 install.py` (or `tessella install --from bundle.tar` to
upgrade an existing install) checks every hash first and refuses a bundle that does not
match, then installs the wheels with `pip --no-index --require-hashes` and copies the models
where Ollama finds them. Nothing in it opens a network connection.

Keep this module to the standard library: it is copied into the bundle as install.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

FORMAT = 1
MANIFEST = "MANIFEST.json"
LOCK = "requirements.lock"
WHEELHOUSE = "wheelhouse"
MODELS = "models"
BIN = "bin"
DEFAULT_REGISTRY = "registry.ollama.ai"
PROJECT_MODELS = ".tessella/models"  # where the project's own Ollama server keeps models


class BundleError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- Ollama model files ----------------------------------------------------------------------

def manifest_path(models_dir: Path, model: str) -> Path:
    """Where Ollama keeps a model's manifest: qwen3.5:4b ->
    manifests/registry.ollama.ai/library/qwen3.5/4b; user/m -> .../user/m/latest;
    hf.co/org/m:q4 -> manifests/hf.co/org/m/q4."""
    name, _, tag = model.partition(":")
    if "/" in tag:  # a registry port, e.g. host:5000/m
        name, tag = model, ""
    parts = name.split("/")
    if len(parts) == 1:
        parts = [DEFAULT_REGISTRY, "library", parts[0]]
    elif len(parts) == 2:
        parts = [DEFAULT_REGISTRY, *parts]
    return models_dir.joinpath("manifests", *parts, tag or "latest")


def model_files(models_dir: Path, model: str) -> list[Path]:
    """The manifest and every blob it names, all relative to models_dir."""
    manifest = manifest_path(models_dir, model)
    if not manifest.is_file():
        raise BundleError(f"model {model} is not in {models_dir} (expected {manifest}); "
                          "pull it there first")
    data = json.loads(manifest.read_text())
    digests = [data.get("config", {}).get("digest")] + [
        layer.get("digest") for layer in data.get("layers", [])]
    out = [manifest.relative_to(models_dir)]
    for digest in filter(None, digests):
        blob = models_dir / "blobs" / digest.replace(":", "-")
        if not blob.is_file():
            raise BundleError(f"model {model}: blob {digest} is missing from {models_dir}")
        out.append(blob.relative_to(models_dir))
    return out


def default_models_dir() -> Path:
    return Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")


# -- build (connected machine) ---------------------------------------------------------------

def _wheel_name_version(filename: str) -> tuple[str, str]:
    """Distribution name and version from a wheel filename (PEP 427)."""
    if not filename.endswith(".whl"):
        raise BundleError(f"{filename} is not a wheel; offline installs need wheels only")
    name, version = filename[:-4].split("-")[:2]
    return name.replace("_", "-").lower(), version


def lock_lines(wheelhouse: Path) -> list[str]:
    lines = []
    for whl in sorted(wheelhouse.glob("*.whl")):
        name, version = _wheel_name_version(whl.name)
        lines.append(f"{name}=={version} --hash=sha256:{sha256(whl)}")
    return lines


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BundleError(f"{' '.join(cmd[:4])} ... failed:\n{(proc.stderr or proc.stdout)[-2000:]}")


def fetch_wheels(project: Path, extras: list[str], dest: Path, python_version: str | None,
                 platform: str | None, python: str = sys.executable) -> None:
    """Build Tessella's wheel from `project`, then download every dependency as a wheel.
    The only step that needs the internet (PyPI, or your own index via pip's config)."""
    dest.mkdir(parents=True, exist_ok=True)
    _run([python, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", str(dest), str(project)])
    built = sorted(dest.glob("tessella-*.whl"))
    if not built:
        raise BundleError(f"no Tessella wheel was built from {project}")
    target = str(built[-1]) + (f"[{','.join(extras)}]" if extras else "")
    cmd = [python, "-m", "pip", "download", "--quiet", "--only-binary=:all:", "-d", str(dest),
           target]
    if python_version:
        cmd += ["--python-version", python_version]
    if platform:
        cmd += ["--platform", platform]
    _run(cmd)


def build(out: Path, project: Path, extras: list[str], models: list[str],
          models_dir: Path | None = None, python_version: str | None = None,
          platform: str | None = None, ollama: Path | None = None,
          fetch=fetch_wheels) -> dict:
    """Write the bundle to `out`; returns its manifest. `fetch` fills the wheelhouse (tests
    pass a stand-in so they need no network)."""
    from tessella import __version__

    if not (project / "pyproject.toml").is_file():
        raise BundleError(f"{project} has no pyproject.toml; run from the Tessella source "
                          "folder or pass --project")
    models_dir = models_dir or default_models_dir()
    with tempfile.TemporaryDirectory(prefix="tessella-bundle-") as tmp:
        root = Path(tmp) / "tessella-bundle"
        wheelhouse = root / WHEELHOUSE
        fetch(project, extras, wheelhouse, python_version, platform)
        (root / LOCK).write_text("\n".join(lock_lines(wheelhouse)) + "\n")
        for model in models:
            for rel in model_files(models_dir, model):
                target = root / MODELS / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(models_dir / rel, target)
        if ollama:
            (root / BIN).mkdir()
            shutil.copy2(ollama, root / BIN / ollama.name)
        shutil.copy2(Path(__file__), root / "install.py")
        files = {str(p.relative_to(root)): sha256(p)
                 for p in sorted(root.rglob("*")) if p.is_file()}
        manifest = {
            "format": FORMAT,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tessella": __version__,
            "python": python_version or f"{sys.version_info.major}.{sys.version_info.minor}",
            "platform": platform or f"{sys.platform}-{_platform.machine()}",
            "extras": extras,
            "models": models,
            "ollama": ollama.name if ollama else None,
            "files": files,
        }
        (root / MANIFEST).write_text(json.dumps(manifest, indent=1))
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w") as tar:
            tar.add(root, arcname=root.name)
    return manifest


# -- install (offline machine) ---------------------------------------------------------------

def verify(root: Path) -> list[str]:
    """Problems with an unpacked bundle: a file missing, changed, or not in the manifest."""
    try:
        manifest = json.loads((root / MANIFEST).read_text())
    except (OSError, ValueError) as exc:
        return [f"{MANIFEST} is missing or unreadable: {exc}"]
    if manifest.get("format") != FORMAT:
        return [f"bundle format {manifest.get('format')} is not supported (expected {FORMAT})"]
    problems = []
    expected = manifest.get("files", {})
    for rel, digest in expected.items():
        path = root / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
        elif sha256(path) != digest:
            problems.append(f"changed: {rel}")
    for path in root.rglob("*"):
        rel = str(path.relative_to(root))
        if path.is_file() and rel != MANIFEST and rel not in expected:
            problems.append(f"not in the manifest: {rel}")
    return problems


def unpack(bundle: Path, into: Path) -> Path:
    with tarfile.open(bundle) as tar:
        tar.extractall(into, filter="data")  # no absolute paths, no links out of `into`
    tops = [p for p in into.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise BundleError(f"{bundle} is not a Tessella bundle")
    return tops[0]


def install_models(root: Path, models_dir: Path) -> int:
    """Copy model manifests and blobs; a blob already there with the same size is kept."""
    src = root / MODELS
    copied = 0
    for path in sorted(src.rglob("*")) if src.is_dir() else []:
        if not path.is_file():
            continue
        target = models_dir / path.relative_to(src)
        if target.is_file() and target.stat().st_size == path.stat().st_size \
                and "blobs" in target.parts:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def install_ollama(root: Path, dest: Path) -> Path | None:
    files = sorted((root / BIN).glob("*")) if (root / BIN).is_dir() else []
    if not files:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    src = files[0]
    if src.name.endswith((".tgz", ".tar.gz")):  # the Linux release archive: bin/ and lib/
        with tarfile.open(src) as tar:
            tar.extractall(dest, filter="data")
        found = next((p for p in dest.rglob("ollama") if p.is_file()), None)
    else:
        found = dest / src.name
        shutil.copy2(src, found)
    if found:
        found.chmod(found.stat().st_mode | 0o111)
    return found


def install(bundle: Path, venv: Path | None = None, models_dir: Path | None = None,
            ollama_dir: Path | None = None, python: str = sys.executable,
            say=print) -> dict:
    """Verify, then install wheels and models. Raises BundleError before changing anything
    if the bundle does not match its manifest."""
    with tempfile.TemporaryDirectory(prefix="tessella-install-") as tmp:
        root = unpack(bundle, Path(tmp))
        problems = verify(root)
        if problems:
            raise BundleError("the bundle does not match its manifest, nothing was installed:\n  "
                              + "\n  ".join(problems[:20]))
        manifest = json.loads((root / MANIFEST).read_text())
        say(f"Bundle verified: {len(manifest['files'])} files, Tessella {manifest['tessella']}, "
            f"Python {manifest['python']}, {manifest['platform']}.")
        if venv:
            import venv as venv_mod

            venv_mod.EnvBuilder(with_pip=True).create(venv)  # ensurepip works offline
            python = str(venv / ("Scripts" if os.name == "nt" else "bin") / "python")
        _run([python, "-m", "pip", "install", "--no-index", "--find-links",
              str(root / WHEELHOUSE), "--require-hashes", "-r", str(root / LOCK)])
        packages = [ln for ln in (root / LOCK).read_text().splitlines() if ln.strip()]
        say(f"Installed {len(packages)} packages into {python}.")
        copied = 0
        if manifest.get("models"):
            models_dir = models_dir or Path(PROJECT_MODELS)
            copied = install_models(root, models_dir)
            say(f"Models {', '.join(manifest['models'])}: {copied} file(s) copied to {models_dir}.")
        binary = install_ollama(root, ollama_dir or Path(".tessella/bin"))
        if binary:
            say(f"Ollama installed at {binary}.")
        return {"python": python, "models_dir": str(models_dir) if models_dir else None,
                "models_copied": copied, "ollama": str(binary) if binary else None,
                "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    """`python3 install.py` inside an unpacked bundle, or `python3 install.py bundle.tar`."""
    p = argparse.ArgumentParser(description="Install Tessella from an offline bundle.")
    p.add_argument("bundle", nargs="?", help="the bundle .tar (default: the folder this "
                   "script is in, if it is an unpacked bundle)")
    p.add_argument("--venv", help="create this virtual environment and install into it")
    p.add_argument("--models-dir", help="where the project's Ollama server keeps models "
                   f"(default: {PROJECT_MODELS})")
    p.add_argument("--ollama-dir", default=".tessella/bin",
                   help="where to put the Ollama program, if the bundle has one")
    args = p.parse_args(argv)
    bundle = Path(args.bundle) if args.bundle else None
    if bundle is None:
        here = Path(__file__).resolve().parent
        if not (here / MANIFEST).is_file():
            p.error("give the bundle .tar, or run install.py from an unpacked bundle")
        with tempfile.TemporaryDirectory() as tmp:  # re-pack so one code path verifies all
            bundle = Path(tmp) / "bundle.tar"
            with tarfile.open(bundle, "w") as tar:
                tar.add(here, arcname=here.name)
            return _main_install(bundle, args)
    return _main_install(bundle, args)


def _main_install(bundle: Path, args) -> int:
    try:
        install(bundle, venv=Path(args.venv) if args.venv else None,
                models_dir=Path(args.models_dir) if args.models_dir else None,
                ollama_dir=Path(args.ollama_dir))
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
