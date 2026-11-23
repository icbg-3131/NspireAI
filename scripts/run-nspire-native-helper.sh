#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "${NSPIRE_NATIVE_PYTHON:-$ROOT/bridge/.venv/bin/python}" -m bridge.native_navnet "$@"
