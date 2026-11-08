"""Loads tessella.yaml into typed settings. Secrets come from environment variables only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class Inputs(BaseModel):
    reports: list[str] = Field(default_factory=lambda: ["playwright-report/", "test-results/*.xml"])
    traces: list[str] = Field(default_factory=lambda: ["test-results/**/trace.zip"])
    repo: str = "."
    # The commit to diff against. Default: the commit of the last triage that saw no
    # failures (recorded automatically in a git repository). Set e.g. "origin/main".
    green_ref: str | None = None


LOCAL_PROVIDERS = {"ollama", "ollama_chat", "vllm", "hosted_vllm", "lm_studio"}


class ModelSettings(BaseModel):
    provider: str = "ollama_chat"
    # Small enough for a CPU-only laptop with 16 GB RAM, reads images too (reader + vision).
    name: str = "qwen3.5:4b"
    # The project's own Ollama server (see `ollama:` below), so models live in the repo.
    base_url: str | None = "http://127.0.0.1:11435"
    # "local" = runs inside your network (laptop or team server); "cloud" = a vendor API.
    # Inferred from the provider when left out; set "local" for a self-hosted
    # OpenAI-compatible server.
    location: Literal["local", "cloud"] | None = None
    approve_each_call: bool = False  # ask a human before every call (cloud fixers)
    # A shared model server inside your network that wants an API key (vLLM started with
    # --api-key): the NAME of the environment variable that holds it, never the key itself.
    api_key_env: str | None = None
    max_steps_per_cluster: int = 4
    min_confidence: float = 0.6
    # Ollama only. think: false turns off a reasoning model's thinking phase, which costs
    # minutes on a CPU; None keeps the model's default. num_ctx: context window in tokens
    # (Ollama's default of 4096 cuts off longer evidence packs); None keeps the default.
    think: bool | None = None
    num_ctx: int | None = None
    # Upper bound on the evidence pack, in tokens, even when num_ctx has room for more: small
    # models judge a short, dense pack better and faster. None fills the context window.
    max_evidence_tokens: int | None = 3000
    # Seconds one model call may take before it is abandoned and the cluster marked `unknown`.
    # Generous on purpose: a 4B model on a CPU can need minutes for a full evidence pack.
    timeout_s: float = 600
    # Ollama only: how long the model stays in memory after a call, so the next cluster
    # does not wait for a reload. None leaves Ollama's own default (5 minutes).
    keep_alive: str | None = "30m"
    # Reader: classify first, then write the ticket fields in a second, smaller call (only
    # for product and test bugs). One job per call suits small local models; a large model
    # can do both at once with two_step: false.
    two_step: bool = True
    # Reader: a verdict this close to min_confidence is asked again (votes - 1 more samples)
    # and the majority wins; agreement becomes part of the confidence. 1 turns this off.
    votes: int = 3
    vote_margin: float = 0.15
    # Clusters judged at the same time. Match your server's parallel slots
    # (OLLAMA_NUM_PARALLEL, vLLM's batch); 1 is right for a CPU laptop.
    parallel: int = 1
    # Vision: screenshots are scaled so the long side is at most this many pixels before
    # they are sent. Small vision models read 1024 px as well as 4K, many times faster.
    max_image_px: int = 1024
    # Reader: when the first answer is unsure (unknown, or within vote_margin of
    # min_confidence), the reader may ask for this many more pieces of evidence from a
    # fixed menu (a log window, a diff, more of the test, a screenshot region, a re-run)
    # before it answers again. 0 turns the investigator off.
    investigate_steps: int = 2

    @property
    def is_cloud(self) -> bool:
        if self.location:
            return self.location == "cloud"
        return self.provider not in LOCAL_PROVIDERS


class Models(BaseModel):
    """Model roles. 'Local reads, cloud writes' is one sensible split:
    reader (sees logs and evidence) and vision (sees screenshots) local,
    fixer (writes patches from a redacted summary) cloud."""

    reader: ModelSettings = Field(default_factory=ModelSettings)
    vision: ModelSettings | None = None
    fixer: ModelSettings | None = None
    # Turns failures into vectors for near-duplicate recall and few-shot examples
    # (memory/vectors.py). A small local embedding model, e.g. Ollama's nomic-embed-text.
    embedder: ModelSettings | None = None

    def get(self, role: str) -> ModelSettings | None:
        return getattr(self, role, None)


class DataPolicy(BaseModel):
    """What may be sent to a cloud model. Local models see everything.

    A data class goes to the cloud only if it is in may_leave AND not in never_leaves."""

    may_leave: list[str] = Field(
        default_factory=lambda: ["redacted_summary", "failing_test_file", "diff_hunk"]
    )
    never_leaves: list[str] = Field(
        default_factory=lambda: ["raw_logs", "stack_traces", "screenshots", "test_data"]
    )
    audit_log: str = ".tessella/model_calls.jsonl"
    audit_payloads: bool = False  # also store the exact text sent (larger, but fully reviewable)

    def allows(self, data_class: str) -> bool:
        return data_class in self.may_leave and data_class not in self.never_leaves


class Rerun(BaseModel):
    command: str = "pytest {test_id} -q"
    attempts: int = 2
    # Appended for the investigator's "re-run with more detail" tool and for checking a
    # fixer's patch: e.g. "-vv --tb=long" for pytest, "--trace on" for Playwright.
    detail_args: str = "-vv --tb=long"


class Logs(BaseModel):
    """Backend logs read around the time a test failed. file: a local log file. loki and
    elasticsearch: queried through the network allowlist (add their host to network.allow)."""

    source: Literal["file", "loki", "elasticsearch"] = "file"
    path: str | None = None
    url: str | None = None
    # Seconds of log read before the test started and after it failed. Without test times
    # in the report, this many seconds either side of the end of the run.
    window_seconds: int = 60
    query: str | None = None  # loki: a LogQL selector such as {app="shop"}; es: a query string
    index: str = "logs-*"  # elasticsearch index pattern
    timestamp_field: str = "@timestamp"  # elasticsearch
    token_env: str | None = None  # env var holding a bearer token for loki/elasticsearch
    utc: bool = False  # file logs: timestamps are UTC, not this machine's local time
    max_lines: int = 60


class Identity(BaseModel):
    login_url: str | None = None
    username: str | None = None
    password_env: str | None = None

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_env) if self.password_env else None


class Scope(BaseModel):
    include: list[str] = Field(default_factory=lambda: ["/*"])
    exclude: list[str] = Field(default_factory=list)


class Probe(BaseModel):
    base_url: str | None = None
    scope: Scope = Field(default_factory=Scope)
    identities: dict[str, Identity] = Field(default_factory=dict)
    mutators: list[str] = Field(
        default_factory=lambda: ["boundaries", "types", "identity", "order", "concurrency"]
    )
    # Imported Schemathesis/ZAP findings below this severity are left out of triage.
    min_severity: Literal["info", "low", "medium", "high", "critical"] = "low"


class Jira(BaseModel):
    url: str | None = None  # site base, e.g. https://acme.atlassian.net (not a board URL)
    project: str | None = None  # project key, e.g. KAN
    # Atlassian Cloud authenticates with the account email + an API token (Basic auth).
    # Leave email unset for self-hosted Jira Server/Data Center, which uses a Bearer PAT.
    email: str | None = None
    token_env: str = "JIRA_TOKEN"
    issue_type: str = "Bug"  # must match an issue type that exists in the project
    # Duplicate search: during triage, issues in the project that read like a drafted
    # failure are listed on the draft as possible duplicates (never skipped or linked).
    # duplicate_threshold: cosine similarity, when models.embedder is set; otherwise
    # duplicate_text_threshold: share of the issue summary's words the failure also uses.
    search_duplicates: bool = True
    duplicate_threshold: float = 0.82
    duplicate_text_threshold: float = 0.6
    duplicate_limit: int = 3

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env)


class GitHub(BaseModel):
    repo: str | None = None  # "owner/repo"
    token_env: str = "GITHUB_TOKEN"
    labels: list[str] = Field(default_factory=list)

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env)


class Fix(BaseModel):
    """The fixer agent for product bugs (needs `models.fixer`). It reads the code a failure
    points to, proposes a patch, and checks it in a temporary copy of the repository: the
    failing test must pass on every re-run, and `suite_command` must fail no test that did
    not already fail without the patch. Nothing is applied to your files; a checked fix is
    shown on the ticket and `tessella fix apply` puts it on a git branch."""

    product: bool = True  # also fix product bugs, not only test bugs
    steps: int = 8  # agent turns (read, search, edit) before it gives up
    # Starts the app under test from the copy, so the patched code is what the tests hit:
    # e.g. "python -m uvicorn shop.app:app --port {port}". None: the tests start it themselves.
    app_command: str | None = None
    # The environment variable the tests read the app's address from, e.g. DEMO_SHOP_URL.
    app_url_env: str | None = None
    app_ready_path: str = "/"  # polled until it answers, after app_command starts
    # The rest of the suite, run without and with the patch, e.g. "pytest -q -m 'not ui'".
    # None: only the failing test is checked, and the ticket says so.
    suite_command: str | None = None


class Ci(BaseModel):
    """`tessella ci`: a summary comment on the pull request (GitHub Actions) or merge request
    (GitLab CI) the job runs for, edited in place on each run. Off unless turned on here or
    with `tessella ci --comment`. The API host (api.github.com, or your GitLab host) must be
    in network.allow; the token is read from the env var named here (default GITHUB_TOKEN on
    GitHub, GITLAB_TOKEN on GitLab)."""

    comment: bool = False
    token_env: str | None = None
    # Linked from the comment, e.g. a team queue: https://tessella.lan:8484
    queue_url: str | None = None
    # Stop asking the model after this many minutes; failures left over are reported as not
    # judged. A CPU runner is slow, and a CI job has a time limit. None: no limit.
    max_minutes: float | None = None


class OllamaLocal(BaseModel):
    """A project-scoped `ollama serve`, so a project's models live under its own
    .tessella/ instead of the user's shared ~/.ollama. On by default: Tessella starts it
    when a role points at its port. models_dir is found from the nearest folder upward
    that has it, so subfolders of the project share one copy."""

    enabled: bool = True
    port: int = 11435
    models_dir: str = ".tessella/models"


class Memory(BaseModel):
    """What Tessella learns from past reviews. Needs `models.embedder`; without one, recall
    is exact-signature only and no examples are shown."""

    path: str = ".tessella/vectors.sqlite"  # derived data: deleting it only costs re-embedding
    # A failure at least this similar to one the team already decided is marked as a
    # possible duplicate of it (cited evidence), not silently skipped.
    similar_threshold: float = 0.9
    # Reviewed failures most like this one, shown to the reader as calibration examples.
    examples: int = 2
    example_threshold: float = 0.75


class Cache(BaseModel):
    """Replies to deterministic (temperature 0) model calls, keyed by model and exact
    prompt, so re-triaging unchanged evidence costs no model time. Stays on this machine."""

    enabled: bool = True
    path: str = ".tessella/model_cache.sqlite"


class Preflight(BaseModel):
    """URLs checked once before any model call. If one is down, every failure in the run
    is labelled environment with that evidence, instead of a pile of bug drafts."""

    urls: list[str] = Field(default_factory=list)


class Screenshots(BaseModel):
    """Screenshots attached to a filed ticket leave this machine, so text in them that
    matches the redaction rules is blacked out first (OCR, `pip install tessella[ocr]` and
    the tesseract program). auto: redact when OCR is available, else attach as they are.
    required: never attach a screenshot that could not be redacted. off: attach as they are."""

    redact: Literal["auto", "required", "off"] = "auto"
    path: str = ".tessella/redacted"


class Network(BaseModel):
    allow: list[str] = Field(default_factory=lambda: ["localhost:11435"])
    # Offline mode (`tessella --offline`) refuses every connection that is not loopback,
    # whatever `allow` says. Hosts inside your own network that it may still reach, such as
    # a team model server: "gpu-box.lan:8000".
    offline_allow: list[str] = Field(default_factory=list)


class Team(BaseModel):
    """A review queue several people use over your network (`tessella queue --host`).
    Each reviewer signs in with their name and a password kept in an environment variable
    on the queue server; the decision records who made it."""

    # reviewer name -> NAME of the environment variable that holds their password
    reviewers: dict[str, str] = Field(default_factory=dict)

    def password(self, name: str) -> str | None:
        env = self.reviewers.get(name)
        return os.environ.get(env) if env else None


class Mcp(BaseModel):
    """`tessella mcp`: what a coding assistant may read. Turn excerpts off (summaries,
    verdicts and tickets only) when the assistant sends what it reads to a cloud model."""

    excerpts: bool = True


class Storage(BaseModel):
    path: str = ".tessella/tessella.sqlite"


class Settings(BaseModel):
    inputs: Inputs = Field(default_factory=Inputs)
    models: Models = Field(default_factory=Models)
    data_policy: DataPolicy = Field(default_factory=DataPolicy)
    rerun: Rerun = Field(default_factory=Rerun)
    logs: Logs = Field(default_factory=Logs)
    probe: Probe = Field(default_factory=Probe)
    jira: Jira = Field(default_factory=Jira)
    github: GitHub = Field(default_factory=GitHub)
    ci: Ci = Field(default_factory=Ci)
    fix: Fix = Field(default_factory=Fix)
    board: Literal["jira", "github"] | None = None
    ollama: OllamaLocal = Field(default_factory=OllamaLocal)
    network: Network = Field(default_factory=Network)
    redact: list[str] = Field(
        default_factory=lambda: ["Authorization", "Cookie", "password", "card_number"]
    )
    storage: Storage = Field(default_factory=Storage)
    cache: Cache = Field(default_factory=Cache)
    memory: Memory = Field(default_factory=Memory)
    preflight: Preflight = Field(default_factory=Preflight)
    screenshots: Screenshots = Field(default_factory=Screenshots)
    mcp: Mcp = Field(default_factory=Mcp)
    team: Team = Field(default_factory=Team)

    @model_validator(mode="before")
    @classmethod
    def _single_model_to_reader(cls, data):
        """Older configs have one `model:` block; it becomes the reader role."""
        if isinstance(data, dict) and "model" in data and "models" not in data:
            data = dict(data)
            data["models"] = {"reader": data.pop("model")}
        return data

    @property
    def model(self) -> ModelSettings:
        """The reader role, kept for code that predates model roles."""
        return self.models.reader


# The absolute path of the config file the last load() looked for, so messages can name
# the file actually in use (--show-thinking). Set even when the file is missing.
loaded_path: Path | None = None


def load(path: str | Path | None = None) -> Settings:
    """Load settings from a file, or defaults when the file is missing."""
    global loaded_path
    p = Path(path or "tessella.yaml")
    loaded_path = p.resolve()
    if not p.exists():
        return Settings()
    data = yaml.safe_load(p.read_text()) or {}
    return Settings.model_validate(data)
