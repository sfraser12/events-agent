#!/bin/zsh

PROJECT_DIR="$HOME/projects/events-agent"
LOG_DIR="$PROJECT_DIR/logs"
BIN="$PROJECT_DIR/.venv/bin/events-agent"
LOG="$LOG_DIR/weekly.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

echo "===== Weekly run started: $(date) =====" >> "$LOG"

# Deliver-only, deliberately — no harvest/score here. The daily job already
# refreshed the database that morning; re-harvesting and re-scoring in the
# evening too would just be a second LLM bill for the same data.
"$BIN" digest >> "$LOG" 2>&1
DIGEST_EXIT=$?

"$BIN" fortnight >> "$LOG" 2>&1
FORTNIGHT_EXIT=$?

echo "===== Weekly run finished: $(date), digest exit: $DIGEST_EXIT, fortnight exit: $FORTNIGHT_EXIT =====" >> "$LOG"
echo >> "$LOG"

if [ $DIGEST_EXIT -ne 0 ] || [ $FORTNIGHT_EXIT -ne 0 ]; then
  exit 1
fi
exit 0
