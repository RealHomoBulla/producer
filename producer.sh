#!/bin/sh
# One entry point for the everyday commands (Linux / macOS / Git Bash).  ./producer.sh help
cd "$(dirname "$0")" || exit 1
PY=""
for candidate in python3 python py; do
  # A candidate counts only if it really runs Python 3.11+ (Windows ships a python3 stub that does not).
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ is required but was not found (or is too old). Install it from https://www.python.org/downloads/ and run this again." >&2
  exit 1
fi
cmd="${1:-help}"
[ "$#" -gt 0 ] && shift
case "$cmd" in
  setup)  exec "$PY" tools/setup.py "$@" ;;
  doctor) exec "$PY" tools/producer.py doctor ;;
  status) exec "$PY" tools/producer.py status ;;
  start)  exec "$PY" tools/guardian.py start "$@" ;;
  stop)   exec "$PY" tools/guardian.py stop ;;
  serve)  exec "$PY" tools/serve.py "$@" ;;
  update) exec "$PY" tools/update.py "$@" ;;
  docs)   exec "$PY" tools/doc_check.py ;;
  test)
    if ! "$PY" -c 'import pytest' 2>/dev/null; then
      echo "pytest is missing: $PY -m pip install -r requirements-dev.txt" >&2
      exit 1
    fi
    exec "$PY" -m pytest -q tools/tests -p no:cacheprovider "$@" ;;
  *)
    echo "usage: ./producer.sh setup | doctor | status | start | stop | serve | update | docs | test"
    echo "  update  safe fast-forward from your own origin (refuses over uncommitted work)"
    echo "  setup   first-run wizard (python tools/setup.py)      doctor  is this machine ready?"
    echo "  status  one screen of Producer state                  start   start the Guardian (opens the Producer tab)"
    echo "  stop    stop the Guardian                             serve   optional local dev server for a web product"
    echo "  docs    check that the docs point at real things      test    run the tool tests"
    [ "$cmd" = "help" ] && exit 0 || exit 2 ;;
esac
