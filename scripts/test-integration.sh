#!/usr/bin/env bash
# Integration tests against a real Hermes checkout (read-only). Test-only packages (pytest, pyyaml,
# jsonschema) go into a cache dir OUTSIDE the plugin tree (the Hermes security scan walks the plugin
# root) so nothing is added to the Hermes environment.
#
# Environment:
#   HERMES_SRC     Hermes checkout (default: ~/.hermes/hermes-agent)
#   HERMES_PYTHON  interpreter with Hermes' dependencies (default: $HERMES_SRC/.venv/bin/python, then
#                  $HERMES_SRC/venv/bin/python)
#   MEETING_SCRIBE_TEST_DEPS  where the test-only packages are installed
set -euo pipefail
cd "$(dirname "$0")/.."
HERMES_SRC="${HERMES_SRC:-$HOME/.hermes/hermes-agent}"
if [ -z "${HERMES_PYTHON:-}" ]; then
  for candidate in "$HERMES_SRC/.venv/bin/python" "$HERMES_SRC/venv/bin/python"; do
    if [ -x "$candidate" ]; then HERMES_PYTHON="$candidate"; break; fi
  done
fi
PY="${HERMES_PYTHON:?set HERMES_PYTHON (no venv found under $HERMES_SRC)}"
DEPS="${MEETING_SCRIBE_TEST_DEPS:-${XDG_CACHE_HOME:-$HOME/.cache}/meeting-scribe/hermes-test-deps}"
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
DEPS="$DEPS/py$PYVER"
if [ ! -f "$DEPS/.installed-v2" ]; then
  uv pip install -q --python "$PY" --target "$DEPS" "pytest>=8,<10" "pyyaml>=6,<7" "jsonschema>=4,<5"
  touch "$DEPS/.installed-v2"
fi
PYTHONPATH="$HERMES_SRC:$DEPS" "$PY" -m pytest -p no:cacheprovider -m integration tests/integration \
  -o addopts="--import-mode=importlib" "$@"
