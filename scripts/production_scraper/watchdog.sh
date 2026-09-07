#!/usr/bin/env bash
# watchdog.sh — relaunch the production scraper if its log goes quiet for STALL_MIN minutes.
# Why: 2026-09-06 21:15 the run sat 58 min on one HTTPS call to Propelio (socket ESTAB, 0 bytes
# received, DB idle) — the client's read timeout never fired. The runner resumes per address, so a
# kill + relaunch costs one address (~6 min). Runs until the pass completes (completed_at set).
set -u
cd "$(dirname "$0")/../.." || exit 1
PY=/home/kk/projects/clients/lot-ledger/.venv/bin/python
PROFILE="${1:-refresh_6m}"; STALL_MIN="${STALL_MIN:-12}"
LOG=scripts/production_scraper/logs/latest.log; STATE=scripts/production_scraper/state/state.json
WLOG=scripts/production_scraper/logs/watchdog.log
say(){ echo "[$(date '+%F %T')] $*" | tee -a "$WLOG"; }
say "watchdog start profile=$PROFILE stall=${STALL_MIN}m"
while true; do
  sleep 60
  if python3 -c "import json,sys; s=json.load(open('$STATE')); c=s.get('current_pass'); sys.exit(0 if (c is None or c.get('completed_at')) else 1)" 2>/dev/null; then
    say "pass complete — watchdog exits"; exit 0
  fi
  pid=$(pgrep -f "^$PY -u scripts/production_scraper/run.py" | head -1)  # anchored on the interpreter path so a shell merely MENTIONING run.py never matches
  age=$(( $(date +%s) - $(stat -L -c %Y "$LOG" 2>/dev/null || echo 0) ))  # -L: the log FILE, not the symlink (bug 9/06: symlink mtime = relaunch time → killed a healthy run every 12.5 min)
  if [ -z "$pid" ]; then
    say "no scraper process — relaunching"
    setsid nohup "$PY" -u scripts/production_scraper/run.py --profile "$PROFILE" >/dev/null 2>&1 </dev/null &
    sleep 30; continue
  fi
  if [ "$age" -gt $(( STALL_MIN * 60 )) ]; then
    say "log quiet ${age}s (pid $pid) — killing and relaunching"
    kill -INT "$pid"; sleep 20; kill -KILL "$pid" 2>/dev/null; sleep 5
    setsid nohup "$PY" -u scripts/production_scraper/run.py --profile "$PROFILE" >/dev/null 2>&1 </dev/null &
    sleep 30
  fi
done
