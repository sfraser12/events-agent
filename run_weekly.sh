#!/bin/zsh

PROJECT_DIR="$HOME/projects/events-agent"
LOG_DIR="$PROJECT_DIR/logs"
BIN="$PROJECT_DIR/.venv/bin/events-agent"
LOG="$LOG_DIR/weekly.log"
MARKER="$LOG_DIR/.last_attempt_weekly"
CATCHUP_HOURS=150  # must stay below the 168h (7-day) schedule interval -- see run_daily.sh

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

# Same RunAtLoad catch-up logic as run_daily.sh -- see comments there.
# CATCHUP_HOURS must be LESS than the 168h weekly interval, for the same
# reason: 192h (the old value) is longer than a week, so every Sunday's
# real run looked like a recent duplicate and was skipped forever after
# the first one. 150h (~6.25 days) leaves slack for a same-week reboot
# without swallowing the next Sunday's real run.
if [ -f "$MARKER" ]; then
  LAST_ATTEMPT=$(cat "$MARKER")
  NOW=$(date +%s)
  AGE_HOURS=$(( (NOW - LAST_ATTEMPT) / 3600 ))
  if [ "$AGE_HOURS" -lt "$CATCHUP_HOURS" ]; then
    exit 0
  fi
fi

echo "===== Weekly run started: $(date) =====" >> "$LOG"

# Deliver-only, deliberately -- no harvest/score here. The daily job already
# refreshed the database that morning; re-harvesting and re-scoring in the
# evening too would just be a second LLM bill for the same data.
"$BIN" digest >> "$LOG" 2>&1
DIGEST_EXIT=$?

"$BIN" fortnight >> "$LOG" 2>&1
FORTNIGHT_EXIT=$?

echo "===== Weekly run finished: $(date), digest exit: $DIGEST_EXIT, fortnight exit: $FORTNIGHT_EXIT =====" >> "$LOG"
echo >> "$LOG"

date +%s > "$MARKER"

if [ $DIGEST_EXIT -ne 0 ] || [ $FORTNIGHT_EXIT -ne 0 ]; then
  exit 1
fi
exit 0
