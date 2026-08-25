#!/bin/zsh

PROJECT_DIR="$HOME/projects/events-agent"
LOG_DIR="$PROJECT_DIR/logs"
BIN="$PROJECT_DIR/.venv/bin/events-agent"
LOG="$LOG_DIR/daily.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

echo "===== Daily run started: $(date) =====" >> "$LOG"

"$BIN" run >> "$LOG" 2>&1
EXIT_CODE=$?

echo "===== Daily run finished: $(date), exit code: $EXIT_CODE =====" >> "$LOG"
echo >> "$LOG"

exit $EXIT_CODE
