"""`tessella --show-thinking`: print what a local model thinks while it answers, live on
stderr, and keep one line per call in `.tessella/thinking.jsonl`.

Off by default. When off, `watch` returns None, `effective` changes nothing and nothing
here runs. When on, every Ollama call is sent with think: true for that run, overriding
the config (`effective`), so there is thinking to show. Calls can run in
parallel (reader.parallel), so lines are printed whole under a lock and carry the call
tag whenever more than one call is in flight.
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console

from tessella import config
from tessella.config import ModelSettings

enabled = False
log_path = ".tessella/thinking.jsonl"
console = Console(stderr=True, highlight=False)

_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_WIDTH = 100  # a line of thinking is printed at a newline or at about this many characters

_lock = threading.Lock()
_calls = 0
_active = 0
_hinted = False
_forced = False
_OLLAMA = {"ollama", "ollama_chat"}

# The model role of the call in progress (set by llm.complete), named in messages.
role: ContextVar[str | None] = ContextVar("thinking_role", default=None)


def enable(on: bool = True) -> None:
    global enabled, _hinted, _forced
    enabled, _hinted, _forced = on, False, False


def config_path() -> str:
    """The absolute path of the tessella.yaml this run loaded, for messages."""
    p = config.loaded_path
    if p is None:
        return "(none loaded)"
    return str(p) if p.exists() else f"{p} (not found, defaults)"


def effective(ms: ModelSettings, role_name: str | None = None) -> ModelSettings:
    """`ms` as it is sent: with --show-thinking, an Ollama role gets think: true for this
    run whatever the config says (said once, naming the config and its value). The reply
    cache keys on this, so a forced-think reply is never mixed with a config-think one."""
    if not enabled or ms.provider not in _OLLAMA:
        return ms
    global _forced
    with _lock:
        if not _forced:
            _forced = True
            value = "unset" if ms.think is None else str(ms.think).lower()
            name = role_name or role.get()
            who = f" for {name}" if name else ""
            _print(f"--show-thinking: thinking turned on for this run (config "
                   f"{config_path()} has think: {value}{who})")
    return ms if ms.think is True else ms.model_copy(update={"think": True})


def _print(text: str, style: str = "dim") -> None:
    console.print(text, style=style, markup=False, soft_wrap=True)


def cached(ms: ModelSettings) -> None:
    """A reply served from the reply cache: no model ran, so there is nothing to show."""
    if enabled:
        with _lock:
            _print(f"── {ms.name} (cached reply: no thinking) ──")


def watch(ms: ModelSettings) -> Call | None:
    """A watcher for one model call, or None when --show-thinking is off."""
    return Call(ms) if enabled else None


class Call:
    """One model call being watched: its header, its thinking as it arrives, the answer."""

    def __init__(self, ms: ModelSettings) -> None:
        global _calls, _active
        with _lock:
            _calls += 1
            _active += 1
            self.tag = f"call {_calls}"
            _print(f"── {ms.name} thinking [{self.tag}] ──")
        self.ms = ms
        self.role = role.get()
        self.live = ms.provider in {"ollama", "ollama_chat"}  # Ollama can stream it
        self.prompt_tokens: int | None = None
        self.eval_tokens: int | None = None
        self._parts: list[str] = []
        self._line = ""
        self._answering = False
        self._closed = False
        self._started = time.monotonic()

    def think(self, text: str) -> None:
        """A piece of thinking: printed line by line as it arrives."""
        if not text:
            return
        self._parts.append(text)
        self._line += text
        while "\n" in self._line or len(self._line) >= _WIDTH:
            if "\n" in self._line:
                line, self._line = self._line.split("\n", 1)
            else:
                cut = self._line.rfind(" ", 0, _WIDTH)
                cut = cut + 1 if cut > 0 else _WIDTH
                line, self._line = self._line[:cut], self._line[cut:]
            self._emit(line.rstrip())

    def answer(self) -> None:
        """The first piece of the answer arrived: thinking is over."""
        if self._answering:
            return
        self._answering = True
        self._flush()
        with _lock:
            _print(f"── answering [{self.tag}] ──")

    def finish(self, text: str, tokens: int) -> None:
        """The call returned `text` (before strip_thinking). Shows any <think> block in it,
        hints once if the model did not think, and writes the jsonl line."""
        for block in _THINK_BLOCK.findall(text):
            self.think(block.strip() + "\n")
        self._flush()
        answer = _THINK_BLOCK.sub("", text).strip()
        thought = "".join(self._parts).strip()
        seconds = round(time.monotonic() - self._started, 2)
        self._close()
        global _hinted
        with _lock:
            if not thought and not _hinted:
                _hinted = True
                who = f"{self.ms.name} ({self.role})" if self.role else self.ms.name
                why = (" even with think on; this model or server may not support it"
                       if self.live else "; this server takes no think option")
                _print(f"{who} returned no thinking{why} (config {config_path()})")
            _print(f"── done [{self.tag}] {seconds}s ──")
        record = {
            "at": datetime.now(UTC).isoformat(),
            "model": self.ms.name,
            "host": urlsplit(self.ms.base_url or "").hostname or "",
            "call": self.tag,
            "thinking": thought,
            "answer": answer[:2000],
            "prompt_tokens": self.prompt_tokens,
            "eval_tokens": self.eval_tokens,
            "tokens": tokens,
            "seconds": seconds,
        }
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def fail(self, exc: BaseException) -> None:
        self._flush()
        self._close()
        with _lock:
            _print(f"── failed [{self.tag}]: {str(exc)[:200]} ──")

    def _close(self) -> None:
        global _active
        if not self._closed:
            self._closed = True
            with _lock:
                _active -= 1

    def _flush(self) -> None:
        if self._line.strip():
            self._emit(self._line.rstrip())
        self._line = ""

    def _emit(self, line: str) -> None:
        with _lock:
            _print(f"[{self.tag}] {line}" if _active > 1 else f"  {line}")
