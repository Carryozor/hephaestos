#!/usr/bin/env bash
# Gate qualité backend : à lancer avant tout build/déploiement (et par le hook
# PostToolUse des sessions Claude). Échoue au premier outil en erreur.
set -euo pipefail
cd "$(dirname "$0")"

MYPY=.venv-tools/bin/mypy
[ -x "$MYPY" ] || { echo "mypy absent : python3 -m venv .venv-tools && .venv-tools/bin/pip install mypy" >&2; exit 1; }

echo "== ruff =="
ruff check app tests

echo "== mypy =="
"$MYPY"

echo "OK: ruff + mypy verts"
