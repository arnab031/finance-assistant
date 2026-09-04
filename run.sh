#!/usr/bin/env bash
# Start (or check) everything the assistant needs, in one command.
#
#   ./run.sh          start all services, tail the logs
#   ./run.sh status   what is up, what is not
#   ./run.sh stop     stop the API and web dev server
#   ./run.sh test     run the full regression suite
#
# Postgres and Ollama run as brew services, so they survive reboots and are
# only started if not already listening.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PG_BIN="/opt/homebrew/opt/postgresql@18/bin"
API_LOG="/tmp/tbx_api.log"
WEB_LOG="/tmp/tbx_web.log"

c()  { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
ok() { c "32" "  ✓ $1"; }
no() { c "31" "  ✗ $1"; }
hdr() { c "36" "$1"; }

listening() { nc -z -w1 localhost "$1" >/dev/null 2>&1; }

wait_for() {  # port, name, seconds
  for _ in $(seq 1 "${3:-40}"); do
    listening "$1" && { ok "$2 ready on :$1"; return 0; }
    sleep 1
  done
  no "$2 did not come up on :$1"; return 1
}

start_services() {
  hdr "Infrastructure"
  if listening 5432; then ok "postgres already on :5432"
  else brew services start postgresql@18 >/dev/null 2>&1; wait_for 5432 postgres; fi

  if listening 11434; then ok "ollama already on :11434"
  else brew services start ollama >/dev/null 2>&1; wait_for 11434 ollama; fi

  echo
  hdr "Application"
  if listening 8000; then ok "api already on :8000"
  else
    ./.venv/bin/uvicorn api.main:app --port 8000 --log-level info >"$API_LOG" 2>&1 &
    wait_for 8000 api 60 || { no "see $API_LOG"; tail -20 "$API_LOG"; exit 1; }
  fi

  if listening 3000; then ok "web already on :3000"
  else
    (cd web && npm run dev >"$WEB_LOG" 2>&1 &)
    wait_for 3000 web 60 || { no "see $WEB_LOG"; tail -20 "$WEB_LOG"; exit 1; }
  fi
}

status() {
  hdr "Services"
  for pair in "5432 postgres" "11434 ollama" "8000 api" "3000 web"; do
    set -- $pair
    listening "$1" && ok "$2 :$1" || no "$2 :$1 not running"
  done
  echo
  if listening 8000; then
    hdr "Health"
    curl -s localhost:8000/api/health \
      | ./.venv/bin/python -c '
import json,sys
d=json.load(sys.stdin)
for k,v in d["checks"].items():
    mark = "\033[32m✓\033[0m" if v.get("ok") else "\033[31m✗\033[0m"
    extra = v.get("version") or v.get("transactions") or v.get("error") or ""
    print(f"  {mark} {k:<10} {extra}")' 2>/dev/null || no "health check failed"
  fi
}

case "${1:-start}" in
  start)
    start_services
    echo
    hdr "Ready"
    echo "  web    http://localhost:3000     <- open this"
    echo "  api    http://localhost:8000/api/health"
    echo "  docs   http://localhost:8000/docs"
    echo
    echo "  logs:  tail -f $API_LOG"
    echo "  stop:  ./run.sh stop"
    ;;
  status) status ;;
  stop)
    pkill -f "uvicorn api.main:app" 2>/dev/null && ok "api stopped" || no "api was not running"
    pkill -f "next dev" 2>/dev/null && ok "web stopped" || no "web was not running"
    echo "  (postgres and ollama left running - brew services stop postgresql@18 ollama)"
    ;;
  test)
    listening 8000 || { no "api not running - ./run.sh start"; exit 1; }
    hdr "Compiler (phases 2-3)";  ./.venv/bin/python -m eval.test_compiler | tail -3
    hdr "Narration (phase 6)";    ./.venv/bin/python -m eval.test_narrate  | tail -2
    hdr "Extraction (phases 4-5)"; ./.venv/bin/python -m eval.test_extract | tail -2
    ;;
  *) echo "usage: ./run.sh [start|status|stop|test]"; exit 1 ;;
esac
