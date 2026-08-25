#!/bin/zsh

PROJECT_DIR="$HOME/projects/events-agent"
LOG_DIR="$PROJECT_DIR/logs"
BIN="$PROJECT_DIR/.venv/bin/events-agent"
LOG="$LOG_DIR/daily.log"
MARKER="$LOG_DIR/.last_attempt_daily"
CATCHUP_HOURS=36

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

# RunAtLoad fires this on every launchd load/reboot/login, not just missed
# schedules -- skip if a run was already attempted recently (the normal
# case: RunAtLoad firing right after today's 06:40 scheduled run already
# happened, or right after `launchctl load`). Only actually run if it's
# been longer than CATCHUP_HOURS since the last attempt, meaning the
# 06:40 slot was genuinely missed (Mac off/asleep through it).
if [ -f "$MARKER" ]; then
  LAST_ATTEMPT=$(cat "$MARKER")
  NOW=$(date +%s)
  AGE_HOURS=$(( (NOW - LAST_ATTEMPT) / 3600 ))
  if [ "$AGE_HOURS" -lt "$CATCHUP_HOURS" ]; then
    exit 0
  fi
fi

echo "===== Daily run started: $(date) =====" >> "$LOG"

"$BIN" run >> "$LOG" 2>&1
EXIT_CODE=$?

echo "===== Daily run finished: $(date), exit code: $EXIT_CODE =====" >> "$LOG"
echo >> "$LOG"

# Marked on attempt, not on success -- a persistently failing step (e.g. a
# bad API key) shouldn't cause a retry on every single reboot; it gets
# another shot after the same 36h window instead.
date +%s > "$MARKER"

exit $EXIT_CODE
