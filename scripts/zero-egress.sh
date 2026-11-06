#!/usr/bin/env bash
# Proves that a Tessella run sends nothing off this machine, child processes included.
#
# Runs Tessella inside a new network namespace that has only loopback, with offline mode
# on, so there are two independent guards:
#   1. the kernel: no interface but `lo`, so every outside connection fails (any program);
#   2. offline mode: Python's audit hook refuses and records every non-loopback attempt,
#      and Tessella exits 3 if there was one, so the job fails instead of passing quietly.
# The model server runs inside the same namespace: the project's Ollama (models from
# .tessella/models, port 11435, the program from .tessella/bin or PATH), or
# MODEL_SERVER="your command" for llama.cpp, vLLM, a stand-in, ...
#
# Usage (Linux, no root needed where unprivileged user namespaces are enabled):
#   bash scripts/zero-egress.sh triage --no-rerun     # from the project folder
#   OLLAMA_MODELS=/elsewhere/models OLLAMA_PORT=11434 bash scripts/zero-egress.sh triage
# Exit code: Tessella's own; 3 means something tried to reach the network.
set -euo pipefail

if [ "$#" -eq 0 ]; then set -- triage --no-rerun; fi
TESSELLA="${TESSELLA:-tessella}"
PYTHON="${PYTHON:-python3}"

exec unshare --user --map-root-user --net -- env TESSELLA="$TESSELLA" PYTHON="$PYTHON" \
  MODEL_SERVER="${MODEL_SERVER:-}" OLLAMA_PORT="${OLLAMA_PORT:-11435}" \
  OLLAMA_MODELS="${OLLAMA_MODELS:-$PWD/.tessella/models}" bash -s -- "$@" <<'INSIDE'
set -euo pipefail
# A new namespace starts with loopback down. `ip` may be missing, so set the flag directly.
"$PYTHON" - <<'PY'
import fcntl, socket, struct
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
req = struct.pack("16sH14s", b"lo", 0, b"")
flags = struct.unpack("16sH14s", fcntl.ioctl(s, 0x8913, req))[1]           # SIOCGIFFLAGS
fcntl.ioctl(s, 0x8914, struct.pack("16sH14s", b"lo", flags | 0x1, b""))    # SIOCSIFFLAGS, IFF_UP
PY
interfaces=$(awk -F: 'NR>2 {gsub(/ /, "", $1); print $1}' /proc/self/net/dev)
if [ "$interfaces" != "lo" ]; then echo "unexpected interfaces: $interfaces" >&2; exit 2; fi
echo "Network namespace ready: only loopback ($interfaces)."

server=""
if [ -n "$MODEL_SERVER" ]; then
  bash -c "$MODEL_SERVER" & server=$!
else
  ollama=$(command -v .tessella/bin/ollama .tessella/bin/bin/ollama ollama 2>/dev/null | head -n1 || true)
  if [ -n "$ollama" ]; then
    OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" OLLAMA_MODELS="$OLLAMA_MODELS" "$ollama" serve \
      >/tmp/zero-egress-ollama.log 2>&1 & server=$!
  fi
fi
trap '[ -n "$server" ] && kill "$server" 2>/dev/null || true' EXIT
if [ -n "$server" ]; then sleep "${SERVER_WAIT:-3}"; fi

TESSELLA_OFFLINE=1 "$TESSELLA" "$@"
INSIDE
