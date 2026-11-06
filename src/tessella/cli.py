"""Command line: triage, probe, ci, queue, report, calibrate, sync, merge, export, doctor,
eval, models, reindex, export-dataset."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from tessella import __version__, config

app = typer.Typer(help="Tessella: your local quality investigator.", no_args_is_help=True)
console = Console()


@app.callback()
def _main(offline: bool = typer.Option(
        False, "--offline", envvar="TESSELLA_OFFLINE",
        help="Refuse every network connection that is not loopback (or network.offline_allow), "
             "from any code in the process, and say so."),
        show_thinking: bool = typer.Option(
        False, "--show-thinking", envvar="TESSELLA_SHOW_THINKING",
        help="Turn thinking on for this run (think: true for every Ollama call, whatever the "
             "config says) and print each local model's thinking live on stderr while it "
             "answers; keep it in .tessella/thinking.jsonl.")):
    if offline:
        from tessella import egress

        egress.enable()
    if show_thinking:
        from tessella.integrations import thinking

        thinking.enable()


def _offline_verdict(fail: bool = True) -> None:
    """In offline mode, say whether anything tried to reach the network. Exit 3 if so,
    so a CI job fails on the first attempt."""
    from tessella import egress

    if not egress.active():
        return
    if egress.attempts:
        seen = ", ".join(dict.fromkeys(egress.attempts))
        console.print(f"[red]Offline mode: {len(egress.attempts)} connection attempt(s) "
                      f"refused:[/] {seen}")
        if fail:
            raise typer.Exit(3)
    else:
        console.print("[green]Offline mode: no connection left this machine.[/]")


def _settings(cfg: Optional[Path], reports: list[str] | None = None,
              traces: list[str] | None = None, models: bool = False) -> config.Settings:
    """The settings; `models` also starts the project's Ollama server when a role uses it."""
    s = config.load(cfg)
    from tessella import egress

    if egress.active() and s.network.offline_allow:
        egress.enable(s.network.offline_allow)
    if reports:
        s.inputs.reports = reports
    if traces:
        s.inputs.traces = traces
    if models:
        _start_project_ollama(s)
    return s


def _uses_project_ollama(s: config.Settings) -> bool:
    """Does any role call the project's own Ollama (its port on this machine)?"""
    from urllib.parse import urlsplit

    from tessella.integrations.local_llm import OLLAMA_PROVIDERS

    if not s.ollama.enabled:
        return False
    for role in ("reader", "vision", "fixer", "embedder"):
        ms = s.models.get(role)
        if ms is None or ms.provider not in OLLAMA_PROVIDERS or not ms.base_url:
            continue
        url = urlsplit(ms.base_url)
        if url.hostname in ("localhost", "127.0.0.1", "::1") and url.port == s.ollama.port:
            return True
    return False


def _start_project_ollama(s: config.Settings) -> None:
    """Start the project's Ollama server if a role uses it. If it cannot start, say why
    and carry on: model calls then fail one cluster at a time, and --no-model works."""
    if not _uses_project_ollama(s):
        return
    from tessella.integrations import ollama_runtime

    try:
        ollama_runtime.ensure_running(s.ollama, repo=s.inputs.repo)
    except RuntimeError as exc:
        console.print(f"[yellow]Project model server not started:[/] {exc}")


@app.command()
def init(target: Optional[Path] = typer.Option(None, "--config", "-c", help="Where to write the config.")):
    """Interactively set up tessella.yaml: models, board, and the installs/pulls they need."""
    from tessella.setup.wizard import run as run_wizard

    run_wizard(target)


_PAST = {"approve": "approved", "edit": "approved", "reject": "rejected"}


def _when(at) -> str:
    return f"{at.day} {at:%b}"


def _ticket_cell(lead, decision):
    """What became of a cluster: its earlier decision and ticket (the key links to the
    ticket in terminals that support it), a new draft, or a case left for review."""
    from rich.style import Style
    from rich.text import Text

    if decision is not None:
        done = f"{_PAST.get(decision.action, decision.action)} {_when(decision.at)}"
        if decision.reason:
            done += f" ({decision.reason})"
        if not decision.ticket_key:
            return Text(done + (" · no ticket" if decision.action != "reject" else ""))
        key = Text(decision.ticket_key,
                   style=Style(link=decision.ticket_url) if decision.ticket_url else "")
        return Text.assemble(key, f" · {done}")
    if lead.draft:
        return Text("new draft → queue")
    if lead.verdict:
        return Text(f"not filed ({lead.verdict.category}) → queue")
    return Text("→ queue")


def _decided_label(signature: str, decision) -> str:
    return decision.ticket_key or f"{_PAST.get(decision.action, decision.action)} {signature[:8]}"


def _triage_json(result) -> str:
    import json

    from tessella import board

    status = {r.signature: r for r in board.classify(result)}
    rows = []
    for sig, members in result.clusters.items():
        lead, d = members[0], result.decisions.get(sig)
        rows.append({
            "board": status[sig].status, "existing_tickets": status[sig].tickets,
            "note": status[sig].note,
            "signature": sig, "tests": len(members), "example": lead.title,
            "test_id": lead.test_id or None,
            "screenshots": sum(len(m.screenshots) for m in members),
            "category": lead.verdict.category if lead.verdict else None,
            "confidence": lead.verdict.confidence if lead.verdict else None,
            "draft": lead.draft is not None,
            "decision": d.action if d else None,
            "reason": d.reason if d and d.reason else None,
            "decided_at": d.at.isoformat() if d else None,
            "decided_by": d.by if d else None,
            "ticket_key": d.ticket_key if d else None,
            "ticket_url": d.ticket_url if d else None,
        })
    return json.dumps({"failures": len(result.cases), "recalled": result.recalled,
                       "summary": board.headline(result, list(status.values())),
                       "clusters": rows}, indent=2)


@app.command()
def triage(
    reports: list[str] = typer.Argument(None, help="Report folders/files (Playwright, Allure, pytest-html, JUnit)."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
    no_rerun: bool = typer.Option(False, "--no-rerun", help="Use artifacts only (fast)."),
    no_model: bool = typer.Option(False, "--no-model", help="Collect and cluster only."),
    as_json: bool = typer.Option(False, "--json", help="Print the clusters as JSON, with each "
                                 "earlier decision and its ticket."),
):
    """Explain failures in this run's reports and fill the review queue."""
    from tessella.pipeline import triage as run

    s = _settings(cfg, reports, models=not no_model)
    result = run(s, rerun=not no_rerun, use_model=not no_model)
    if as_json:
        print(_triage_json(result))
        _offline_verdict()
        return
    decided = [_decided_label(sig, d) for sig, d in result.decisions.items()]
    table = Table(title=f"{len(result.cases)} failures → {len(result.clusters)} clusters "
                        f"({len(decided)} already decided"
                        + (f": {', '.join(decided)}" if decided else "") + ")")
    for col in ("Cluster", "Tests", "Example", "Screens", "Verdict", "Ticket"):
        table.add_column(col)
    for sig, members in result.clusters.items():
        v = members[0].verdict
        shots = sum(len(m.screenshots) for m in members)
        table.add_row(sig[:8], str(len(members)), members[0].title[:60], str(shots) if shots else "—",
                      f"{v.category} {v.confidence:.0%}" if v else "—",
                      _ticket_cell(members[0], result.decisions.get(sig)))
    console.print(table)
    from tessella import board

    rows = board.classify(result)
    console.print(f"[bold]{board.headline(result, rows)}[/]", highlight=False)
    for r in rows:
        if r.status != board.Status.pending:
            console.print(f"  {r.signature[:8]}  {r.note}", highlight=False)
    if decided:
        sig, first = next(iter(result.decisions.items()))
        console.print("To review a decided failure again: tessella forget "
                      f"{first.ticket_key or sig[:8]}", highlight=False)
    if result.flows:
        console.print(f"{len(result.flows)} flow(s) collected from traces/HAR for probing.")
    _offline_verdict()


@app.command()
def probe(
    traces: list[str] = typer.Argument(None, help="Playwright trace.zip files or HAR recordings."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
    flow: Optional[str] = typer.Option(None, help="Only probe the flow with this name."),
):
    """Explore recorded flows for bugs the tests missed (v2)."""
    from tessella.collect import collect_flows
    from tessella.probe.linker import link
    from tessella.probe.mutators import all_mutations

    s = _settings(cfg, traces=traces, models=False)
    flows = [f for f in collect_flows(s.inputs.traces, s.redact) if not flow or f.name == flow]
    others = [n for n in s.probe.identities if n != "user_a"] + ["anonymous"]
    from collections import Counter

    from tessella.memory.store import Store

    store = Store(s.storage.path)
    for f in flows:
        link(f)
        planned = list(all_mutations(f, s.probe.mutators, others))
        store.record_probe(f.name, len(f.steps), len(f.variables),
                           dict(Counter(m.mutator for m in planned)))
        console.print(f"[bold]{f.name}[/]: {len(f.steps)} steps, {len(f.variables)} linked "
                      f"variables, {len(planned)} mutations planned")
    console.print("Tessella plans changes but does not send them. To find bugs in these "
                  "flows, run Schemathesis (`--report junit`) or OWASP ZAP (`-J report.json`) "
                  "and pass the report to `tessella triage`.")


@app.command()
def ci(reports: list[str] = typer.Argument(None), cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
       comment: Optional[bool] = typer.Option(None, "--comment/--no-comment", help="Post the summary on the pull/merge request (default: ci.comment)."),
       queue_url: Optional[str] = typer.Option(None, "--queue-url", help="Review queue link for the comment (default: ci.queue_url).")):
    """Run triage (and later probe) in CI, print one summary and optionally comment it on the PR. Always exits 0."""
    from tessella.integrations import ci_comment
    from tessella.pipeline import triage as run

    try:
        s = _settings(cfg, reports, models=True)
        result = run(s)
        body = ci_comment.summary(result.cases, queue_url or s.ci.queue_url, result)
        console.print(body)
        if comment if comment is not None else s.ci.comment:
            _post_ci_comment(s, body)
    except Exception as exc:  # CI must never fail the build because of Tessella
        console.print(f"[red]Tessella error:[/] {exc}")
    _offline_verdict(fail=False)
    raise typer.Exit(0)


def _post_ci_comment(s: config.Settings, body: str) -> None:
    """Comment on the PR/MR, or say plainly why not. Never raises."""
    from tessella import egress
    from tessella.integrations import ci_comment
    from tessella.net import is_allowed

    if egress.active():
        console.print("[yellow]Not commenting:[/] offline mode.")
        return
    target = ci_comment.detect(token_env=s.ci.token_env)
    if target is None:
        console.print(f"[yellow]Not commenting:[/] {ci_comment.why_not(token_env=s.ci.token_env)}.")
        return
    host = ci_comment.host_of(target)
    if not is_allowed(target.list_url, s.network.allow):
        console.print(f"[yellow]Not commenting:[/] add {host} to network.allow in tessella.yaml.")
        return
    try:
        how = ci_comment.post(target, body, s.network.allow)
        console.print(f"Comment {how} on the {target.host} {'merge' if target.host == 'GitLab' else 'pull'} request.")
    except Exception as exc:  # noqa: BLE001 - a comment is never worth a failed build
        console.print(f"[yellow]Could not comment on {target.host}:[/] {exc}")


@app.command()
def queue(cfg: Optional[Path] = typer.Option(None, "--config", "-c"), port: int = 8484,
          host: str = typer.Option("127.0.0.1", "--host", help="Listen address. Anything but "
                                   "this machine (e.g. 0.0.0.0 for a team) needs team.reviewers."),
          tls_cert: Optional[Path] = typer.Option(None, "--tls-cert", help="Serve HTTPS with this certificate."),
          tls_key: Optional[Path] = typer.Option(None, "--tls-key", help="... and this private key."),
          no_sign_in: bool = typer.Option(False, "--no-sign-in", help="Listen beyond loopback without reviewers. Only inside a container whose port is published to 127.0.0.1.")):
    """Open the review queue: on this machine, or for a team with each reviewer signed in."""
    from tessella.queue.app import serve

    serve(_settings(cfg, models=False), port, host=host, tls_cert=str(tls_cert) if tls_cert else None,
          tls_key=str(tls_key) if tls_key else None, no_sign_in=no_sign_in)


@app.command()
def report(
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
    out: Path = typer.Option(Path(".tessella/report.html"), "--out", "-o", help="Where to write the page."),
    open_it: bool = typer.Option(False, "--open", help="Open the page in your browser afterwards."),
    view: str = typer.Option("full", "--view", help="'full', or 'management' for the short page: headline, time saved, tickets, trend."),
):
    """Write an offline HTML dashboard of what Tessella has done: failures, clusters, reviews, models."""
    from tessella import report as report_page

    if view not in report_page.VIEWS:
        console.print(f"[yellow]Unknown view '{view}'.[/] Choose one of: {', '.join(report_page.VIEWS)}.")
        raise typer.Exit(1)
    # config.load, not _settings: building a report must not start an Ollama server.
    path = report_page.write(config.load(cfg), out, view=view)
    console.print(f"Report written to [bold]{path}[/]")
    if open_it:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())


@app.command()
def sync(cfg: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Check what happened to the Jira tickets Tessella filed (fixed, closed as not a bug, ...). Read-only."""
    from tessella import outcomes
    from tessella.memory.store import Store

    s = config.load(cfg)  # like report: no Ollama needed
    store = Store(s.storage.path)
    if not store.filed_tickets():
        console.print("No Jira tickets filed yet, so there is nothing to check.")
        return
    if not (s.jira.url and s.jira.token):
        console.print("[yellow]Jira is not configured (jira.url, and the environment variable named "
                      "by jira.token_env), so no ticket was checked.[/]")
        raise typer.Exit(1)
    result = outcomes.sync(s, store)
    console.print(f"Checked {result['checked']} ticket(s), {result['changed']} changed"
                  + (f"; {result['skipped']} could not be read and keep their last status."
                     if result["skipped"] else "."))
    counts: dict[str, int] = {}
    for o in store.outcomes():
        counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
    for name in outcomes.OUTCOMES:
        if counts.get(name):
            console.print(f"  {outcomes.LABELS[name]}: {counts[name]}")


@app.command()
def merge(
    other: Path = typer.Argument(..., help="Another tessella.sqlite: a CI artifact or a teammate's copy."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Bring another Tessella database into this one: new failures, decisions, runs, timings. Adding the same file twice changes nothing."""
    from tessella.memory.store import Store

    store = Store(config.load(cfg).storage.path)
    try:
        added = store.merge_from(other)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1)
    shown = {k: v for k, v in added.items() if v}
    console.print("Merged: " + (", ".join(f"{v} {k}" for k, v in shown.items()) if shown
                                else "nothing new in that file."))


@app.command()
def calibrate(
    signature: Optional[str] = typer.Argument(None, help="Cluster to time (signature or prefix). Default: the next one you have not seen in the queue."),
    minutes: Optional[float] = typer.Option(None, "--minutes", help="Record a time you already know, instead of using the stopwatch."),
    no_ticket: bool = typer.Option(False, "--no-ticket", help="With --minutes: that investigation did not end in a ticket."),
    note: str = typer.Option("", "--note", help="With --minutes: where the time comes from."),
    show: bool = typer.Option(False, "--list", help="Show the recorded samples and the baseline they give."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Time a manual investigation so the report's "time saved" uses your team's numbers."""
    from tessella import calibrate as cal
    from tessella.memory.store import Store

    store = Store(config.load(cfg).storage.path)
    if show:
        info = cal.summary(store)
        if not info["samples"]:
            console.print("No samples yet. Run [bold]tessella calibrate[/] to time one.")
            return
        table = Table(title="Timed manual investigations")
        for col in ("When", "Minutes", "Ended in", "Source", "Note"):
            table.add_column(col)
        for s in info["samples"]:
            table.add_row(s["at"][:16].replace("T", " "), f"{s['seconds'] / 60:.1f}",
                          "a ticket" if s["filed"] else "no ticket", s["source"], s["note"])
        console.print(table)
        for key, label in (("ticket", "ending in a ticket"), ("other", "ending without one")):
            part = info[key]
            console.print(f"Median {label}: "
                          + (f"{part['median_min']} min from {part['n']}" if part["n"] else "no samples, the report assumes a default"))
        console.print("Five or more samples of a kind give a steady median.")
        return

    if minutes is not None:
        try:
            cal.record_minutes(store, minutes, filed=not no_ticket, note=note, signature=signature)
        except ValueError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1)
        console.print(f"Recorded {minutes:g} min ({'no ticket' if no_ticket else 'ended in a ticket'}).")
        return

    case = cal.pick(store, signature)
    if case is None:
        console.print("No cluster left to time. Run [bold]tessella triage[/] first, or pass "
                      "`--minutes` to record a time you already know.")
        raise typer.Exit(1)
    plain = lambda text: console.print(text, markup=False, highlight=False)  # stack traces hold [brackets]
    try:
        result = cal.time_manual(case, ask=input, say=plain)
    except (EOFError, KeyboardInterrupt):
        console.print("\nCancelled, nothing recorded.")
        raise typer.Exit(1)
    if result is None:
        console.print("Cancelled, nothing recorded.")
        return
    seconds, filed, why = result
    store.add_manual_sample(seconds, filed, signature=case.signature, note=why, source="timer")
    total = len(store.manual_samples())
    console.print(f"Recorded {seconds / 60:.1f} min ({'ended in a ticket' if filed else 'no ticket'}). "
                  f"{total} sample{'s' if total != 1 else ''} so far; five of each kind give a steady median.")


@app.command()
def forget(
    which: Optional[str] = typer.Argument(None, help="Signature (full or prefix) or ticket key to drop. Omit to list all recorded decisions."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """List recorded decisions, or drop one so its failure returns to the review queue."""
    from tessella.memory.store import Store

    store = Store(_settings(cfg, models=False).storage.path)
    decided: dict[str, tuple[str, str | None]] = {}
    for case, d in store.recent_decisions(limit=1000):
        decided.setdefault(case.signature, (d.action, d.ticket_key))

    if not which:
        if not decided:
            console.print("No recorded decisions.")
            return
        table = Table(title=f"{len(decided)} recorded decision(s)")
        for col in ("Signature", "Action", "Ticket"):
            table.add_column(col)
        for sig, (action, key) in decided.items():
            table.add_row(sig, action, key or "—")
        console.print(table)
        console.print("Drop one with [bold]tessella forget <signature|ticket>[/].")
        return

    matches = {sig: v for sig, v in decided.items()
               if which == v[1] or sig == which or sig.startswith(which)}
    if not matches:
        console.print(f"[yellow]No decision matches '{which}'.[/] Run `tessella forget` to list them.")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print(f"[yellow]'{which}' matches several signatures:[/] {', '.join(matches)}")
        raise typer.Exit(1)
    sig, (action, key) = next(iter(matches.items()))
    store.forget(sig)
    console.print(f"Forgot {sig} ({action}{', ' + key if key else ''}). "
                  "It returns to the queue on the next triage or queue load.")


@app.command()
def export(approved: bool = typer.Option(True, "--approved"), out: Path = typer.Option(Path("tests/tessella"), "-o")):
    """Export regression tests from approved probe findings (v2)."""
    console.print(f"[yellow]Not implemented yet (v2): would write tests to {out}[/]")


@app.command()
def mcp(cfg: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Serve cases, evidence and verdicts to a coding assistant over MCP (stdio). Read-only:
    nothing can be approved or filed through it. Needs `pip install tessella[mcp]`."""
    try:
        from tessella.mcp_server import serve
    except ImportError:
        console.print("[red]The MCP server needs the mcp extra:[/] pip install 'tessella[mcp]'")
        raise typer.Exit(1)
    serve(config.load(cfg))


@app.command()
def bundle(
    out: Path = typer.Option(Path("tessella-bundle.tar"), "--out", "-o", help="The bundle to write."),
    project: Path = typer.Option(Path("."), "--project", help="The Tessella source folder (has pyproject.toml)."),
    extras: str = typer.Option("queue,html,vision,mcp", "--extras", help="Optional parts to include, comma-separated."),
    model: list[str] = typer.Option(None, "--model", "-m", help="Ollama model to include; repeat. Default: the local Ollama models your roles use."),
    no_models: bool = typer.Option(False, "--no-models", help="Leave the model files out."),
    models_dir: Optional[Path] = typer.Option(None, "--models-dir", help="Where those models are. Default: the project's .tessella/models (ollama.models_dir); with ollama.enabled false, $OLLAMA_MODELS or ~/.ollama/models."),
    python_version: Optional[str] = typer.Option(None, "--python-version", help="Target Python, e.g. 3.12, when the offline machine differs from this one."),
    platform: Optional[str] = typer.Option(None, "--platform", help="Target platform tag, e.g. manylinux2014_x86_64 or macosx_14_0_arm64."),
    ollama: Optional[Path] = typer.Option(None, "--ollama", help="Also carry the Ollama program: its binary, or the Linux release .tgz."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Write one archive that installs Tessella, its dependencies and your local models on a
    machine with no internet. Needs the internet here (PyPI) to download the wheels."""
    from tessella import bundle as b
    from tessella.integrations.local_llm import OLLAMA_PROVIDERS

    s = config.load(cfg)
    names = list(model or [])
    if not names and not no_models:
        roles = [s.models.reader, s.models.vision, s.models.fixer, s.models.embedder]
        names = list(dict.fromkeys(r.name for r in roles
                                   if r is not None and r.provider in OLLAMA_PROVIDERS))
    if no_models:
        names = []
    if models_dir is None and s.ollama.enabled:
        from tessella.integrations import ollama_runtime

        models_dir = ollama_runtime.models_dir(s.ollama, s.inputs.repo)
    try:
        manifest = b.build(out, project.resolve(), [e for e in extras.split(",") if e], names,
                           models_dir, python_version, platform, ollama)
    except b.BundleError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    wheels = sum(1 for f in manifest["files"] if f.startswith(b.WHEELHOUSE + "/"))
    size = out.stat().st_size / 2**20
    console.print(f"Wrote {out} ({size:,.0f} MB): {wheels} wheels for Python "
                  f"{manifest['python']} on {manifest['platform']}, models: "
                  f"{', '.join(names) or 'none'}{', Ollama' if ollama else ''}.")
    console.print(f"SHA-256 of the bundle: {b.sha256(out)} (compare it after copying)")
    console.print("On the offline machine: tar -xf the bundle, then "
                  "python3 tessella-bundle/install.py --venv .venv")


@app.command("install")
def install_(
    source: Path = typer.Option(..., "--from", help="The bundle written by `tessella bundle`."),
    venv: Optional[Path] = typer.Option(None, "--venv", help="Create this virtual environment and install into it. Default: this Python."),
    models_dir: Optional[Path] = typer.Option(None, "--models-dir", help="Where the project's Ollama server keeps models. Default: the project's .tessella/models."),
    ollama_dir: Path = typer.Option(Path(".tessella/bin"), "--ollama-dir", help="Where to put the Ollama program, if the bundle has one."),
):
    """Install or upgrade from an offline bundle. Every file is checked against the bundle's
    SHA-256 manifest first; nothing is installed if one does not match. No network is used."""
    from tessella import bundle as b
    from tessella.integrations import ollama_runtime

    if models_dir is None:
        models_dir = ollama_runtime.models_dir(config.OllamaLocal())
    try:
        b.install(source, venv=venv, models_dir=models_dir, ollama_dir=ollama_dir,
                  say=console.print)
    except b.BundleError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)


@app.command()
def baseline(
    image: Path = typer.Argument(..., help="Screenshot from a green run."),
    test: str = typer.Option(..., "--test", help="Test title exactly as the report shows it."),
):
    """Save a green-run screenshot as the visual baseline for one test."""
    from tessella.investigate.visual import save_baseline

    target = save_baseline(test, image)
    console.print(f"Baseline for [bold]{test}[/] saved to {target}")


@app.command()
def doctor(egress: bool = typer.Option(False, "--egress"),
           models: bool = typer.Option(False, "--models", help="Check each local model server: "
                                       "reachable, model pulled, images, context, memory."),
           board: bool = typer.Option(False, "--board", help="Check that approving can file to "
                                      "Jira/GitHub: sign-in, project, issue type. Files nothing."),
           cfg: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Check the setup. --egress proves unexpected network calls are blocked; --models checks
    that every local model role can actually answer; --board that filing can work (exit code
    1 if a check fails)."""
    import socket

    from tessella import net
    from tessella.integrations.llm import describe_roles

    s = _settings(cfg, models=models)
    console.print(f"tessella {__version__}; allowlist: {', '.join(s.network.allow) or '(empty)'}")
    console.print("[bold]Model roles[/]")
    for role, desc in describe_roles(s):
        console.print(f"  {role}: {desc}")
    p = s.data_policy
    console.print("[bold]Data policy for cloud models[/]")
    console.print(f"  may leave:    {', '.join(c for c in p.may_leave if p.allows(c)) or '(nothing)'}")
    console.print(f"  never leaves: {', '.join(p.never_leaves)}")
    console.print(f"  audit log:    {p.audit_log}{' (with payloads)' if p.audit_payloads else ''}")
    if egress:
        console.print("[bold]Through Tessella's HTTP client[/]")
        for url, blocked in net.egress_report(s.network.allow):
            mark = "[green]blocked[/]" if blocked else "[red]REACHABLE[/]"
            console.print(f"  {url}: {mark}")
        from tessella import egress as offline

        console.print("[bold]Any code in the process (raw sockets, DNS)[/]")
        if not offline.active():
            console.print("  [yellow]not guarded[/]: run with --offline (or TESSELLA_OFFLINE=1) "
                          "to refuse every non-loopback connection")
        else:
            for host in ("example.com", "1.1.1.1"):
                try:
                    socket.create_connection((host, 443), timeout=3).close()
                    console.print(f"  {host}: [red]REACHABLE[/]")
                except net.EgressBlocked:
                    console.print(f"  {host}: [green]blocked[/]")
                except OSError as exc:
                    console.print(f"  {host}: [yellow]not reached ({exc})[/]")
            offline.attempts.clear()  # these were the test; they are not a leak
    if models:
        from tessella.integrations import model_health

        console.print("[bold]Model servers[/]")
        checks = model_health.check(s)
        for c in checks:
            mark = {True: "[green]ok[/]", False: "[red]FAIL[/]", None: "[yellow]note[/]"}[c.ok]
            console.print(f"  {c.role}: {mark} {c.message}")
        if any(c.ok is False for c in checks):
            raise typer.Exit(1)
    if board:
        from tessella.integrations import board_check

        console.print(f"[bold]Board ({s.board or 'not set'})[/]")
        checks = board_check.check(s)
        for c in checks:
            mark = {True: "[green]ok[/]", False: "[red]FAIL[/]", None: "[yellow]note[/]"}[c.ok]
            console.print(f"  {c.name}: {mark} {c.message}")
        if any(c.ok is False for c in checks):
            raise typer.Exit(1)


@app.command("eval")
def eval_(
    reports: list[str] = typer.Argument(None, help="Report folders/files to judge. Default: inputs.reports."),
    golden: Path = typer.Option(Path("tessella.golden.yaml"), "--golden", "-g", help="The expected categories."),
    model: list[str] = typer.Option(None, "--model", "-m", help="Reader model name to try; repeat to compare. Default: the configured reader."),
    runs: int = typer.Option(1, "--runs", "-n", min=1, help="Judge each failure this many times."),
    rerun: bool = typer.Option(False, "--rerun/--no-rerun", help="Re-run failing tests as evidence (slow)."),
    cache: bool = typer.Option(False, "--cache", help="Reuse cached model replies (off: every run asks the model)."),
    as_json: bool = typer.Option(False, "--json", help="Print the full results as JSON."),
    show_prompts: bool = typer.Option(False, "--show-prompts", help="Also print every prompt sent and reply received, after each failure."),
    min_accuracy: Optional[float] = typer.Option(None, "--min-accuracy", help="Exit 1 if any model scores below this (0 to 1)."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Measure how well the reader judges your failures, against a golden file. Writes nothing to the store."""
    from tessella import evaluate

    try:
        entries = evaluate.load_golden(golden)
    except (OSError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    s = _settings(cfg, reports, models=True)
    results = evaluate.run(s, entries, models=model or None, runs=runs, rerun=rerun, use_cache=cache)
    evaluate.append_history(results, golden, runs)
    evaluate.write_calls(results)
    if as_json:
        print(evaluate.as_json(results))
    else:
        if results and results[0].missing:
            console.print(f"[yellow]No failure in these reports matches:[/] {', '.join(results[0].missing)}")
        table = Table(title=f"{len(entries) - len(results[0].missing) if results else 0} golden failures x {runs} run(s), prompts {evaluate.prompt_version()}")
        cols = ("Model", "Accuracy", "Unknown", "Brier", "Fields", "Facts", "s/cluster")
        for col in cols + (("Consistent",) if runs > 1 else ()):
            table.add_column(col, no_wrap=col == "Model")
        pct = lambda x: "—" if x is None else f"{x:.0%}"
        for r in results:
            m = r.metrics
            row = [r.model, pct(m.get("accuracy")), pct(m.get("unknown_rate")),
                   "—" if m.get("brier") is None else f"{m['brier']:.2f}",
                   pct(m.get("field_completeness")), pct(m.get("facts_hit_rate")),
                   "—" if m.get("seconds_per_cluster") is None else f"{m['seconds_per_cluster']:.1f}"]
            table.add_row(*row, *((pct(m.get("consistency")),) if runs > 1 else ()))
        console.print(table)
        for r in results:
            for (want, got), n in evaluate.confusion(r.results).most_common(5):
                console.print(f"  {r.model}: expected {want}, got {got} ({n}x)")
        for r in results:
            _eval_failures(r, runs, show_prompts)
        console.print(f"Appended to {evaluate.HISTORY}. Brier: 0 is perfectly calibrated, lower is better.")
        for r in results:
            console.print(f"Exact prompts and replies: {r.calls_path}")
        console.print("To review each failure and approve or reject its ticket, run `tessella queue`.")
    if min_accuracy is not None and any((r.metrics.get("accuracy") or 0) < min_accuracy for r in results):
        raise typer.Exit(1)


def _eval_failures(r, runs: int, show_prompts: bool) -> None:
    """The per-failure table for one model, and with --show-prompts each call it made."""
    from rich.markup import escape

    from tessella import evaluate

    table = Table(title=f"{r.model}: each failure", show_lines=True)
    table.add_column("Test", max_width=36, overflow="fold")
    table.add_column("Error", max_width=40, overflow="fold")
    table.add_column("Expected")
    table.add_column("Got")
    table.add_column("Confidence", justify="right")
    table.add_column("Root cause", max_width=50)
    table.add_column("s", justify="right")
    for g in r.results:
        mark = "[green]✓[/]" if g.correct else "[red]✗[/]"
        cited = f" [dim](cites {', '.join(str(i) for i in g.evidence_used)})[/]" if g.evidence_used else ""
        test = g.title or g.match
        table.add_row(escape(test) + (f" [dim](run {g.run})[/]" if runs > 1 else ""),
                      escape(g.error or "—"), g.expected, f"{mark} {g.got}",
                      f"{g.confidence:.2f}", escape(g.root_cause or "—") + cited,
                      f"{g.seconds:.1f}")
    console.print(table)
    if not show_prompts:
        return
    for g in r.results:
        console.print(f"[bold]── {escape(g.title or g.match)}{f' (run {g.run})' if runs > 1 else ''} ──[/]")
        calls = evaluate.calls_for(r, g)
        if not calls:
            console.print("  [dim](no model call: a rule decided it)[/]")
        for i, c in enumerate(calls, 1):
            how = "cached reply" if c.get("cached") else f"{c.get('tokens', 0)} tokens, {c.get('seconds', 0)}s"
            console.print(f"[dim]call {i}: {c['role']} {c['model']} schema {c.get('schema') or '—'} ({how})[/]")
            for m in c["messages"]:
                content = m["content"]
                if isinstance(content, list):
                    content = "\n".join(p.get("text") or p.get("placeholder", "") for p in content)
                console.print(f"  [{m['role']}]", style="dim", markup=False)
                console.print(content, style="dim", markup=False, highlight=False)
            if c.get("thinking"):
                console.print("  [thinking]", style="dim", markup=False)
                console.print(c["thinking"], style="dim", markup=False, highlight=False)
            console.print("  [reply]", style="dim", markup=False)
            console.print(c.get("error") or c.get("reply", ""), style="dim", markup=False, highlight=False)


fix_app = typer.Typer(help="Fixes the fixer agent checked, ready to put on a branch.", no_args_is_help=True)
app.add_typer(fix_app, name="fix")


@fix_app.command("list")
def fix_list(cfg: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Failures with a checked fix or test patch waiting to be applied."""
    from tessella.memory.store import Store

    s = config.load(cfg)
    rows = _fixes(Store(s.storage.path))
    if not rows:
        console.print("No fixes yet. Configure models.fixer (and fix.* for product bugs), then "
                      "run tessella triage.")
        return
    table = Table(title=f"{len(rows)} fix(es)")
    for col in ("Signature", "Ticket", "File", "Checked"):
        table.add_column(col)
    from tessella.draft.fixer_agent import target_of

    for case, key in rows:
        table.add_row(case.signature[:8], key or "—", target_of(case.draft.patch) or "?",
                      case.draft.patch_status)
    console.print(table)


def _fixes(store) -> list:
    """(case, ticket key) for every stored case whose draft carries a patch, newest first."""
    keys = {sig: d.ticket_key for sig, d in store.all_decisions() if d.ticket_key}
    seen, out = set(), []
    for case in reversed(store.all_cases()):
        if case.draft and case.draft.patch and case.signature not in seen:
            seen.add(case.signature)
            out.append((case, keys.get(case.signature)))
    return out


@fix_app.command("apply")
def fix_apply(which: str = typer.Argument(..., help="Ticket key (KAN-8) or signature prefix."),
              branch: Optional[str] = typer.Option(None, "--branch", help="Branch name. Default: tessella/fix-<ticket or signature>."),
              cfg: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Put a checked fix on a new git branch, as one commit. Never pushes; never touches a
    branch with uncommitted changes."""
    import subprocess

    from tessella.draft.fixer_agent import target_of
    from tessella.draft.patch import apply_diff
    from tessella.memory.store import Store

    s = config.load(cfg)
    matches = [(c, k) for c, k in _fixes(Store(s.storage.path))
               if which == k or c.signature.startswith(which)]
    if len(matches) != 1:
        console.print(f"[yellow]{'No fix matches' if not matches else 'Several fixes match'} "
                      f"'{which}'.[/] Run `tessella fix list`.")
        raise typer.Exit(1)
    case, key = matches[0]
    repo = Path(s.inputs.repo).resolve()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    top = git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        console.print("[red]Not a git repository:[/] run `git init` and commit first.")
        raise typer.Exit(1)
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        console.print("[red]You have uncommitted changes.[/] Commit or stash them first.")
        raise typer.Exit(1)
    path = target_of(case.draft.patch)
    target = (repo / path) if path else None
    patched = apply_diff(target.read_text(), case.draft.patch) if target and target.is_file() else None
    if patched is None:
        console.print(f"[red]The fix no longer applies to {path or 'its file'}[/]: the code "
                      "changed since it was checked. Run tessella triage again.")
        raise typer.Exit(1)
    name = branch or f"tessella/fix-{(key or case.signature[:8]).lower()}"
    made = git("switch", "-c", name)
    if made.returncode != 0:
        console.print(f"[red]Could not create branch {name}:[/] {made.stderr.strip()}")
        raise typer.Exit(1)
    target.write_text(patched)
    git("add", path)
    title = (case.draft.title.split("] ", 1)[-1])[:60]
    message = (f"Fix: {title}\n\n{case.draft.root_cause}\n\nChecked by Tessella: "
               f"{case.draft.patch_status}" + (f"\nTicket: {key}" if key else ""))
    done = git("commit", "-q", "-m", message)
    if done.returncode != 0:
        console.print(f"[red]Commit failed:[/] {done.stderr.strip() or done.stdout.strip()}")
        raise typer.Exit(1)
    console.print(f"Fix committed on branch [bold]{name}[/] ({path}). Review it with "
                  f"`git show {name}`; push and open a PR when you are happy. Nothing was pushed.")


models_app = typer.Typer(help="Choose and measure local models for this machine.", no_args_is_help=True)
app.add_typer(models_app, name="models")


@models_app.command("recommend")
def models_recommend(cfg: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Suggest the reader, vision and embedder models that fit this machine's memory."""
    from tessella.integrations import local_llm
    from tessella.setup import recommend as rec

    hw = rec.detect()
    s = config.load(cfg)
    ms = s.models.reader
    pulled = None
    if ms.provider in local_llm.OLLAMA_PROVIDERS:
        try:
            from tessella import net

            with net.client(s.network.allow, timeout=5) as c:
                tags = c.get(f"{local_llm._base(ms)}/api/tags").json().get("models") or []
            pulled = [t.get("name", "") for t in tags]
        except Exception:  # noqa: BLE001, S110 - Ollama not running: recommend without it
            pass
    gpu = {"nvidia": "NVIDIA", "amd": "AMD", "apple": "Apple silicon"}.get(hw.gpu or "", "none found")
    console.print(f"RAM {hw.ram_gb or 0:.0f} GB, {hw.cpus} CPU cores, GPU: {gpu}"
                  + (f" ({hw.vram_gb:.0f} GB)" if hw.vram_gb else ""))
    r = rec.recommend(hw, pulled)
    for note in r.notes:
        console.print(f"  {note}")
    if r.reader:
        console.print("\nPut this in tessella.yaml (and add the server to network.allow):")
        print(r.yaml(local_llm._base(ms) if ms.provider in local_llm.OLLAMA_PROVIDERS
                     else "http://127.0.0.1:11435"))  # plain print: rich would wrap the yaml
        console.print("Then check speed with `tessella models bench` and accuracy with `tessella eval`.")


@models_app.command("import")
def models_import(
    names: list[str] = typer.Argument(None, help="Models to copy, e.g. qwen3.5:4b. Default: the Ollama models your roles use."),
    source: Optional[Path] = typer.Option(None, "--from", help="An Ollama models folder. Default: $OLLAMA_MODELS, then the system service's (/usr/share/ollama/.ollama/models), then ~/.ollama/models."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Copy models you already pulled with another Ollama into this project's .tessella/models,
    so the project's own server runs them. Nothing is downloaded."""
    from tessella.bundle import BundleError
    from tessella.integrations import ollama_runtime
    from tessella.integrations.local_llm import OLLAMA_PROVIDERS

    s = config.load(cfg)
    if not names:
        roles = [s.models.get(r) for r in ("reader", "vision", "fixer", "embedder")]
        names = list(dict.fromkeys(ms.name for ms in roles
                                   if ms is not None and ms.provider in OLLAMA_PROVIDERS))
    target = ollama_runtime.models_dir(s.ollama, s.inputs.repo)
    failed = False
    for name in names:
        folder = source or ollama_runtime.find_installed(name)
        if folder is None:
            console.print(f"[red]{name}: not found in any Ollama models folder.[/] Pull it into "
                          f"the project instead: tessella models pull {name}")
            failed = True
            continue
        try:
            files, size = ollama_runtime.import_model(name, folder, s.ollama, s.inputs.repo)
        except PermissionError as exc:
            console.print(f"[red]{name}: cannot read {exc.filename}.[/] The system service's "
                          "files may need: sudo chmod -R a+rX /usr/share/ollama/.ollama/models")
            failed = True
            continue
        except BundleError as exc:
            console.print(f"[red]{exc}[/]")
            failed = True
            continue
        console.print(f"{name}: {files} file(s), {size / 2**30:.1f} GB copied from {folder} "
                      f"to {target}" if files else f"{name}: already in {target}")
    if failed:
        raise typer.Exit(1)
    console.print(f"Models for this project are in {target}; the project's server "
                  f"(port {s.ollama.port}) runs them.")


@models_app.command("pull")
def models_pull(
    names: list[str] = typer.Argument(None, help="Models to download, e.g. qwen3.5:4b. Default: the Ollama models your roles use."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Download models from the Ollama library into this project's .tessella/models."""
    from tessella.integrations import ollama_runtime
    from tessella.integrations.local_llm import OLLAMA_PROVIDERS

    s = config.load(cfg)
    if not names:
        roles = [s.models.get(r) for r in ("reader", "vision", "fixer", "embedder")]
        names = list(dict.fromkeys(ms.name for ms in roles
                                   if ms is not None and ms.provider in OLLAMA_PROVIDERS))
    try:
        ollama_runtime.ensure_running(s.ollama, repo=s.inputs.repo)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    for name in names:
        ollama_runtime.pull(name, s.ollama, s.inputs.repo)


@models_app.command("bench")
def models_bench(
    names: list[str] = typer.Argument(None, help="Ollama model names. Default: the configured reader (and vision, if different)."),
    runs: int = typer.Option(2, "--runs", "-n", min=1),
    golden: Optional[Path] = typer.Option(None, "--golden", "-g", help="Also run `tessella eval` with this golden file."),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Time each local model on this machine: load, prompt and generation speed."""
    from tessella.integrations import local_llm
    from tessella.setup import recommend as rec

    s = _settings(cfg, models=True)
    reader = s.models.reader
    if reader.provider not in local_llm.OLLAMA_PROVIDERS:
        console.print("[yellow]bench times Ollama models; the reader is not served by Ollama.[/]")
        raise typer.Exit(1)
    if not names:
        names = [reader.name] + [m.name for m in (s.models.vision,) if m and m.name != reader.name
                                 and m.provider in local_llm.OLLAMA_PROVIDERS]
    table = Table(title="Local model speed (a made-up failure; none of your data is sent)")
    for col in ("Model", "Load s", "Prompt tok/s", "Reply tok/s", "~s per judgement"):
        table.add_column(col)
    for name in names:
        ms = reader.model_copy(update={"name": name})
        try:
            st = rec.bench(ms, s.network.allow, runs)
        except Exception as exc:  # noqa: BLE001
            table.add_row(name, "[red]failed[/]", f"{type(exc).__name__}: {str(exc)[:60]}", "", "")
            continue
        fmt = lambda x: "—" if x is None else f"{x:.1f}"
        table.add_row(name, fmt(st["load_s"]), fmt(st["prompt_tps"]), fmt(st["gen_tps"]),
                      fmt(rec.estimate_seconds(st)))
    console.print(table)
    if golden:
        eval_(reports=None, golden=golden, model=names, runs=1, rerun=False, cache=False,
              as_json=False, show_prompts=False, min_accuracy=None, cfg=cfg)


@app.command()
def reindex(cfg: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Embed reviewed failures for similarity recall: after adding an embedder, a merge, or a model change."""
    from tessella.memory.store import Store
    from tessella.memory.vectors import reindex as run

    s = _settings(cfg, models=True)
    try:
        result = run(s, Store(s.storage.path))
    except ValueError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"Embedded {result['embedded']} reviewed failure(s) with {result['model']}.")


@app.command("export-dataset")
def export_dataset(
    out: Path = typer.Option(Path(".tessella/dataset.jsonl"), "--out", "-o"),
    cfg: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Write reviewed decisions as a chat-format JSONL dataset, for fine-tuning a local model."""
    from tessella import dataset
    from tessella.memory.store import Store

    s = config.load(cfg)
    counts = dataset.export(s, Store(s.storage.path), out)
    console.print(f"Wrote {counts['written']} example(s) to {out} ({counts['corrected']} correct the "
                  f"model; {counts['skipped']} decision(s) name no category and were left out).")
    console.print("[yellow]It holds redacted log lines and test source: keep it on your machine.[/]")


if __name__ == "__main__":
    app()
