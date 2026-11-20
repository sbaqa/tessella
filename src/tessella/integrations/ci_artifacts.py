"""Download what `tessella snapshot` left on a CI run: the database and its screenshots.

Understands links to GitHub Actions, GitLab CI, Azure DevOps, Jenkins and CircleCI runs, and for
any other CI a file or folder you downloaded. The link is the permission: only its API host, and
the storage host that host redirects to, are contacted, for this one download. Tokens come from
the environment and go only to the provider's own hosts."""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from tessella import net

ARTIFACT = "tessella-db"  # the name CI publishes it under
DATABASE = "tessella.sqlite"
_LIMIT = 1024 * 1024 * 1024  # a run's results are megabytes; this stops a wrong link early
# Where providers keep the bytes. A redirect anywhere else is refused.
_STORAGE = (".blob.core.windows.net", ".githubusercontent.com", ".visualstudio.com", ".amazonaws.com",
            ".storage.googleapis.com", ".circle-artifacts.com")


class NoAccess(RuntimeError):
    """A token is missing or was refused; the message says which one."""


def _basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


def _need(*names: str) -> str:
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    raise NoAccess(f"Set {names[0]} in your environment to download from this CI.")


def _github_token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    gh = shutil.which("gh")
    if gh:
        done = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, check=False)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    raise NoAccess("Set GITHUB_TOKEN (a token with read access to Actions) or sign in with `gh auth login`.")


def _get(url: str, headers: dict[str, str], own: tuple[str, ...]) -> bytes:
    """GET with redirects by hand: a token goes only to the provider's own hosts, and each
    host is admitted for this one request."""
    for _ in range(6):
        host = urlsplit(url).hostname or ""
        if not (host in own or host.endswith(_STORAGE)):
            raise net.EgressBlocked(f"refused a redirect to {host}")
        sent = headers if host in own or host.endswith(".visualstudio.com") else {}
        with net.client([host], timeout=120, headers=sent) as c, c.stream("GET", url) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code in (401, 403):
                raise NoAccess(f"{host} refused the token (HTTP {r.status_code}).")
            if r.status_code == 404:
                raise FileNotFoundError(f"not found on {host}; the artifact may have expired.")
            r.raise_for_status()
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > _LIMIT:
                    raise ValueError("that download is too large to be a Tessella run")
            return bytes(body)
    raise ValueError("too many redirects")


def _json(url: str, headers: dict[str, str], own: tuple[str, ...]):
    import json

    return json.loads(_get(url, headers, own))


def _github(url, m) -> list[bytes]:
    parts = urlsplit(url)
    api = "https://api.github.com" if parts.hostname == "github.com" else f"https://{parts.netloc}/api/v3"
    headers = {"Authorization": f"Bearer {_github_token()}", "Accept": "application/vnd.github+json"}
    own = (urlsplit(api).hostname, )
    listing = _json(f"{api}/repos/{m['repo']}/actions/runs/{m['run']}/artifacts?name={ARTIFACT}", headers, own)
    found = [a for a in listing.get("artifacts", []) if not a.get("expired")]
    if not found:
        raise FileNotFoundError(f"run {m['run']} has no {ARTIFACT} artifact, or it has expired")
    return [_get(found[0]["archive_download_url"], headers, own)]


def _gitlab(url, m) -> list[bytes]:
    parts = urlsplit(url)
    api = f"https://{parts.netloc}/api/v4/projects/{quote(m['project'], safe='')}"
    headers = {"PRIVATE-TOKEN": _need("GITLAB_TOKEN")}
    own = (parts.hostname, )
    if m.get("job"):
        ids = [m["job"]]
    else:
        jobs = _json(f"{api}/pipelines/{m['pipeline']}/jobs?per_page=100", headers, own)
        ids = [j["id"] for j in jobs if any(a.get("file_type") == "archive" for a in j.get("artifacts", []))]
    return [_get(f"{api}/jobs/{i}/artifacts", headers, own) for i in ids]


def _azure(url, m) -> list[bytes]:
    parts = urlsplit(url)
    build = parse_qs(parts.query).get("buildId", [""])[0]
    if not build.isdigit():
        raise ValueError("that Azure DevOps link has no buildId")
    headers = {"Authorization": _basic("", _need("AZURE_DEVOPS_PAT", "AZURE_DEVOPS_EXT_PAT"))}
    own = (parts.hostname, "dev.azure.com")
    base = f"https://{parts.netloc}/{m['path']}/_apis/build/builds/{build}/artifacts"
    found = _json(f"{base}?artifactName={ARTIFACT}&api-version=7.1", headers, own)
    return [_get(found["resource"]["downloadUrl"], headers, own)]


def _jenkins(url, m) -> list[bytes]:
    parts = urlsplit(url)
    headers = {"Authorization": _basic(_need("JENKINS_USER"), _need("JENKINS_TOKEN"))}
    return [_get(f"{parts.scheme}://{parts.netloc}{m['build']}/artifact/*zip*/archive.zip", headers, (parts.hostname, ))]


def _circle(url, m) -> list[tuple[str, bytes]]:
    vcs = {"github": "gh", "bitbucket": "bb"}.get(m["vcs"], m["vcs"])
    headers = {"Circle-Token": _need("CIRCLE_TOKEN")}
    own = ("circleci.com", )
    items = _json(f"https://circleci.com/api/v2/project/{vcs}/{m['slug']}/{m['job']}/artifacts", headers, own)
    return [(i["path"], _get(i["url"], headers, own)) for i in items.get("items", [])
            if i["path"].endswith(DATABASE) or "/screenshots/" in f"/{i['path']}"]


_PROVIDERS = (
    (re.compile(r"/(?P<repo>[^/]+/[^/]+)/actions/runs/(?P<run>\d+)"), _github),
    (re.compile(r"/(?P<project>.+?)/-/pipelines/(?P<pipeline>\d+)"), _gitlab),
    (re.compile(r"/(?P<project>.+?)/-/jobs/(?P<job>\d+)"), _gitlab),
    (re.compile(r"/(?P<path>.+?)/_build/results"), _azure),
    (re.compile(r"/pipelines/(?P<vcs>[^/]+)/(?P<slug>[^/]+/[^/]+)/\d+/workflows/[^/]+/jobs/(?P<job>\d+)"), _circle),
    (re.compile(r"(?P<build>(?:/[^/]+)*?/job/[^/]+(?:/job/[^/]+)*/\d+)"), _jenkins),
)


def _match(link: str):
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not a link to a CI run: {link}")
    for pattern, provider in _PROVIDERS:
        m = pattern.match(parts.path)
        if m:
            return provider, m.groupdict()
    raise ValueError("not a CI run link Tessella knows. Download the "
                     f"{ARTIFACT} artifact and give its path instead.")


def host_of(link: str) -> str:
    """The host a link would download from. Raises ValueError for one it cannot read."""
    _match(link)
    return urlsplit(link).hostname or ""


def _safe(name: str) -> PurePosixPath | None:
    """Only `tessella.sqlite` and `screenshots/<case>/<image>`, wherever the artifact nests
    them. Nothing else in an archive is written, and nothing can land outside `folder`."""
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if ".." in parts:
        return None
    if parts and parts[-1] == DATABASE:
        return PurePosixPath(DATABASE)
    if "screenshots" in parts:
        tail = parts[parts.index("screenshots"):]
        if len(tail) == 3:
            return PurePosixPath(*tail)
    return None


def _store(files, folder: Path) -> bool:
    found = False
    for name, data in files:
        target = _safe(name)
        if target is None:
            continue
        path = folder / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        found = found or str(target) == DATABASE
    return found


def _unzip(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if not info.is_dir() and _safe(info.filename) is not None:
                yield info.filename, archive.read(info)


def _from_disk(path: Path, folder: Path) -> bool:
    if path.is_dir():
        return _store(((str(f.relative_to(path)), f.read_bytes()) for f in path.rglob("*") if f.is_file()), folder)
    if zipfile.is_zipfile(path):
        return _store(_unzip(path.read_bytes()), folder)
    return _store([(DATABASE, path.read_bytes())], folder)


def fetch(source: str, folder: Path) -> None:
    """Put the run's database and screenshots in `folder`. Raises with a plain reason."""
    path = Path(source)
    if "://" not in source:
        if not path.exists():
            raise FileNotFoundError(f"no such file: {source}")
        found = _from_disk(path, folder)
    else:
        provider, m = _match(source)
        got = provider(source, m)
        files = got if got and isinstance(got[0], tuple) else [f for blob in got for f in _unzip(blob)]
        found = _store(files, folder)
    if not found:
        raise FileNotFoundError(f"no {DATABASE} in that {'artifact' if '://' in source else 'path'}")
    if not (folder / DATABASE).read_bytes().startswith(b"SQLite format 3"):
        raise ValueError(f"{DATABASE} in that artifact is not a database")
