#!/usr/bin/env bash
# One-command start (macOS / Linux): creates .venv, installs deps, runs the paper bot.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
python run.py "$@"
