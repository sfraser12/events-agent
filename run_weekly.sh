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
#
# Roundup only (2026-08-29): fortnight/Heads Up moved to its own script
# (run_fortnight.sh) on its own Wednesday schedule -- having both land in
# the same Sunday run meant two emails at once, right on top of each other,
# which felt like too much in one sitting. See CLAUDE.md "Scheduling".
"$BIN" digest >> "$LOG" 2>&1
DIGEST_EXIT=$?

# Admin stats/cost email moved to run_daily.sh (2026-08-31) -- daily for now
# while getting a feel for real spend/scale. Was here briefly; removed to
# avoid a duplicate send on Sundays once the daily job also started sending
# it. Move it back here (and drop it from run_daily.sh) to return to a
# weekly-only cadence.

echo "===== Weekly run finished: $(date), digest exit: $DIGEST_EXIT =====" >> "$LOG"
echo >> "$LOG"

date +%s > "$MARKER"

# Schedule a real hardware wake for next Sunday 18:30 -- 15min before the
# 18:45 StartCalendarInterval, mirroring the daily job's 06:25 wake / 06:40
# run gap. Added 2026-08-30 after the Mac slept through the 18:45 slot and
# the RunAtLoad catch-up guard (correctly) didn't treat a <150h-old marker
# as a genuine miss -- `pmset repeat` only allows one wake pair system-wide
# (confirmed via `man pmset`), so this uses a one-time `pmset schedule`
# entry instead, re-issued every run so there's always a wake queued for
# the *next* occurrence. Requires the NOPASSWD sudoers rule in
# /etc/sudoers.d/eventsagent-pmset -- if that's ever removed, this line
# fails silently under `sudo -n` and next Sunday reverts to the old
# sleep-through risk.
today_dow=$(date +%w)
days_ahead=$(( (0 - today_dow + 7) % 7 ))
if [ "$days_ahead" -eq 0 ]; then days_ahead=7; fi
wake_at=$(date -v+${days_ahead}d -v18H -v30M -v00S '+%m/%d/%y %H:%M:%S')
sudo -n /usr/bin/pmset schedule wakeorpoweron "$wake_at" >> "$LOG" 2>&1

if [ $DIGEST_EXIT -ne 0 ]; then
  exit 1
fi
exit 0
