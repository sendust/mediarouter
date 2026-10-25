#!/usr/bin/env bash
# start the mediarouter backend (creates the venv on first run)
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m app.main "$@"
