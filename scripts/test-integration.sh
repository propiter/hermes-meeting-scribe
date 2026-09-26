#!/usr/bin/env bash
# Integration tests against a real Hermes checkout (read-only). pytest is installed into a
# cache dir OUTSIDE the plugin tree (the Hermes security scan walks the plugin root) so nothing
# is added to the Hermes venv.
set -euo pipefail
cd "$(dirname "$0")/.."
HERMES_SRC="${HERMES_SRC:-$HOME/.hermes/hermes-agent}"
PY="${HERMES_PYTHON:-$HERMES_SRC/venv/bin/python}"
DEPS="${MEETING_SCRIBE_TEST_DEPS:-${XDG_CACHE_HOME:-$HOME/.cache}/meeting-scribe/hermes-test-deps}"
if [ ! -d "$DEPS/pytest" ]; then
  uv pip install -q --python "$PY" --target "$DEPS" "pytest>=8,<10"
fi
PYTHONPATH="$HERMES_SRC:$DEPS" "$PY" -m pytest -p no:cacheprovider -m integration tests/integration \
  -o addopts="--import-mode=importlib" "$@"
