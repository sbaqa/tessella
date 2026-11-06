"""Interactive first-run setup: asks a short series of questions, writes tessella.yaml,
and performs the installs/model pulls it decided are needed. Like `playwright install`."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt

from tessella.config import LOCAL_PROVIDERS, Inputs, OllamaLocal, Rerun

EXTRA_MODULES = {"llm": "litellm", "queue": "fastapi", "vision": "PIL"}


def _extra_installed(extra: str) -> bool:
    return importlib.util.find_spec(EXTRA_MODULES[extra]) is not None


def _install_extra(console: Console, extra: str) -> None:
    if _extra_installed(extra):
        return
    if not Confirm.ask(f"Install tessella[{extra}]?", default=True):
        console.print(f"[yellow]Skipped tessella[{extra}] — install later with "
                       f"`pip install tessella[{extra}]`.[/]")
        return
    subprocess.run([sys.executable, "-m", "pip", "install", f"tessella[{extra}]"], check=True)


def _ask_model(console: Console, role: str, default_name: str = "qwen3.5:4b") -> tuple[dict, bool]:
    """Returns (model settings dict, uses_local_ollama)."""
    local = Confirm.ask(f"Run the {role} role locally with Ollama?", default=True)
    if local:
        scope = Prompt.ask(
            f"Store {role}'s model in this project (.tessella/models) or use your "
            "existing global Ollama install?",
            choices=["project", "global"], default="project",
        )
        name = Prompt.ask(f"Model name for {role}", default=default_name)
        model = {"provider": "ollama_chat", "name": name}
        if scope == "global":  # the default base_url is the project's own server
            model["base_url"] = "http://localhost:11434"
        return model, scope == "project"
    provider = Prompt.ask(f"Provider for {role} (litellm-style, e.g. anthropic, openai)")
    name = Prompt.ask(f"Model name for {role}")
    base_url = Prompt.ask(f"Base URL for {role} (blank for the provider's default)", default="")
    console.print(f"[yellow]Remember to export the {provider} API key env var yourself — "
                  "it never goes in tessella.yaml.[/]")
    model = {"provider": provider, "name": name}
    if base_url:
        model["base_url"] = base_url
    return model, False


def _render_yaml(a: dict) -> str:
    lines = [
        "# Written by `tessella init`. Secrets always come from environment variables.",
        "inputs:",
        f"  reports: {a['reports']!r}",
        f"  traces: {a['traces']!r}",
        "  repo: .",
        "",
        "models:",
        "  reader:",
        f"    provider: {a['reader']['provider']}",
        f"    name: {a['reader']['name']}",
    ]
    if "base_url" in a["reader"]:
        lines.append(f"    base_url: {a['reader']['base_url']}")
    if a.get("vision"):
        lines += ["  vision:", f"    provider: {a['vision']['provider']}", f"    name: {a['vision']['name']}"]
        if "base_url" in a["vision"]:
            lines.append(f"    base_url: {a['vision']['base_url']}")
    lines += [
        "",
        "rerun:",
        f"  command: {a['rerun_command']!r}",
        f"  attempts: {a['rerun_attempts']}",
        "",
    ]
    if a.get("ollama_enabled"):
        lines += [
            "ollama:",
            "  enabled: true",
            f"  port: {a['ollama_port']}",
            f"  models_dir: {a['ollama_models_dir']!r}",
            "",
        ]
    else:  # on by default; this config uses a global Ollama or no local model
        lines += ["ollama:", "  enabled: false", ""]
    if a.get("board"):
        lines.append(f"board: {a['board']}")
        if a["board"] == "jira":
            j = a["jira"]
            lines.append(f"jira: {{ url: {j['url']}, project: {j['project']}, token_env: {j['token_env']} }}")
        elif a["board"] == "github":
            g = a["github"]
            labels = f", labels: {g['labels']!r}" if g.get("labels") else ""
            lines.append(f"github: {{ repo: {g['repo']}, token_env: {g['token_env']}{labels} }}")
        lines.append("")
    lines += [
        "network:",
        f"  allow: {a['network_allow']!r}",
        "",
    ]
    return "\n".join(lines)


def run(target: Path | None = None) -> None:
    console = Console()
    target = target or Path("tessella.yaml")
    if target.exists() and not Confirm.ask(f"Overwrite existing {target}?", default=False):
        console.print("Aborted.")
        return

    console.print("[bold]Inputs[/]")
    defaults = Inputs()
    reports = Prompt.ask("Report path(s)/glob(s), comma-separated", default=",".join(defaults.reports)).split(",")
    traces = Prompt.ask("Trace path(s)/glob(s), comma-separated", default=",".join(defaults.traces)).split(",")

    console.print("[bold]Rerun[/]")
    rerun_defaults = Rerun()
    rerun_command = Prompt.ask("Rerun command", default=rerun_defaults.command)
    rerun_attempts = IntPrompt.ask("Rerun attempts", default=rerun_defaults.attempts)

    console.print("[bold]Models[/]")
    reader, reader_local_project = _ask_model(console, "reader")
    if Confirm.ask("Use the same setup for the vision role?", default=True):
        vision, vision_local_project = dict(reader), reader_local_project
    elif Confirm.ask("Configure a vision role at all?", default=False):
        vision, vision_local_project = _ask_model(console, "vision")
    else:
        vision, vision_local_project = None, False

    needs_project_ollama = reader_local_project or vision_local_project
    ollama_models = {reader["name"]} | ({vision["name"]} if vision else set())
    ollama_port = 11435
    ollama_models_dir = ".tessella/models"
    network_allow = ["localhost:11434"]

    if needs_project_ollama:
        from tessella.integrations import ollama_runtime

        network_allow = [f"127.0.0.1:{ollama_port}"]  # the same spelling as base_url below
        reader["base_url"] = f"http://127.0.0.1:{ollama_port}"
        if vision:
            vision["base_url"] = f"http://127.0.0.1:{ollama_port}"
        if not ollama_runtime.binary_available():
            console.print("[red]Ollama isn't installed.[/] Install it from https://ollama.com/download, "
                          "then rerun `tessella init` (or pull the models yourself later).")
        else:
            console.print(f"Starting a project-scoped Ollama server on port {ollama_port} "
                          f"(models under {ollama_models_dir})...")
            cfg = OllamaLocal(enabled=True, port=ollama_port, models_dir=ollama_models_dir)
            ollama_runtime.ensure_running(cfg)
            for name in sorted(ollama_models):
                console.print(f"Pulling {name}...")
                ollama_runtime.pull(name, cfg)

    console.print("[bold]Board[/]")
    board = Prompt.ask("File approved tickets to", choices=["none", "jira", "github"], default="none")
    board = None if board == "none" else board
    jira = github = None
    if board == "jira":
        url = Prompt.ask("Jira URL")
        project = Prompt.ask("Jira project key")
        token_env = Prompt.ask("Env var holding the Jira API token", default="JIRA_TOKEN")
        jira = {"url": url, "project": project, "token_env": token_env}
        network_allow.append(url.split("//", 1)[-1].split("/", 1)[0])
    elif board == "github":
        repo = Prompt.ask("GitHub repo (owner/repo)")
        token_env = Prompt.ask("Env var holding the GitHub token", default="GITHUB_TOKEN")
        labels_raw = Prompt.ask("Labels, comma-separated (blank for none)", default="")
        labels = [l.strip() for l in labels_raw.split(",") if l.strip()]
        github = {"repo": repo, "token_env": token_env, "labels": labels}
        network_allow.append("api.github.com")

    console.print("[bold]Dependencies[/]")
    if any(role and role["provider"] not in LOCAL_PROVIDERS for role in (reader, vision)):
        _install_extra(console, "llm")  # cloud roles only: local models are called directly
    if Confirm.ask("Set up the review queue (tessella queue) now?", default=True):
        _install_extra(console, "queue")
    if vision:
        _install_extra(console, "vision")

    answers = {
        "reports": [r.strip() for r in reports],
        "traces": [t.strip() for t in traces],
        "rerun_command": rerun_command,
        "rerun_attempts": rerun_attempts,
        "reader": reader,
        "vision": vision,
        "ollama_enabled": needs_project_ollama,
        "ollama_port": ollama_port,
        "ollama_models_dir": ollama_models_dir,
        "board": board,
        "jira": jira,
        "github": github,
        "network_allow": network_allow,
    }
    target.write_text(_render_yaml(answers))

    console.print(f"\n[green]Wrote {target}.[/]")
    if needs_project_ollama:
        console.print(f"Project Ollama server: http://127.0.0.1:{ollama_port} "
                      f"(models in {ollama_models_dir})")
    console.print("Next: [bold]tessella triage[/] or [bold]tessella queue[/]")
