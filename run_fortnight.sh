#!/bin/zsh

PROJECT_DIR="$HOME/projects/events-agent"
LOG_DIR="$PROJECT_DIR/logs"
BIN="$PROJECT_DIR/.venv/bin/events-agent"
LOG="$LOG_DIR/fortnight.log"
MARKER="$LOG_DIR/.last_attempt_fortnight"
CATCHUP_HOURS=150  # must stay below the 168h (7-day) schedule interval -- see run_daily.sh

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

# Same RunAtLoad catch-up logic as run_daily.sh/run_weekly.sh -- see comments
# there. Split out from run_weekly.sh on 2026-08-29: Heads Up used to fire
# right after Roundup in the same Sunday run, which meant two emails landing
# at once -- moved to its own Wednesday evening slot instead, maximally
# spaced from the previous and next Sunday's Roundup. See CLAUDE.md
# "Scheduling".
if [ -f "$MARKER" ]; then
  LAST_ATTEMPT=$(cat "$MARKER")
  NOW=$(date +%s)
  AGE_HOURS=$(( (NOW - LAST_ATTEMPT) / 3600 ))
  if [ "$AGE_HOURS" -lt "$CATCHUP_HOURS" ]; then
    exit 0
  fi
fi

echo "===== Fortnight run started: $(date) =====" >> "$LOG"

# Deliver-only, deliberately -- no harvest/score here. The daily job already
# refreshed the database that morning.
"$BIN" fortnight >> "$LOG" 2>&1
FORTNIGHT_EXIT=$?

echo "===== Fortnight run finished: $(date), fortnight exit: $FORTNIGHT_EXIT =====" >> "$LOG"
echo >> "$LOG"

date +%s > "$MARKER"

if [ $FORTNIGHT_EXIT -ne 0 ]; then
  exit 1
fi
exit 0
