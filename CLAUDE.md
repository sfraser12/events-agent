# Events Agent — Project Brief

A personal event-discovery agent for a household in Milngavie, East Dunbartonshire (Glasgow area, Scotland). It watches theatre, live music, cinema, comedy and general events across multiple time horizons and surfaces the ones worth planning for and booking.

Home coordinates: **55.9410, -4.3170** (Milngavie). Default search radius: **25 miles** (covers Glasgow, Paisley, Stirling; excludes Edinburgh unless explicitly widened).

---

## Guiding principles

Read these before writing any code. They shape every decision below.

1. **Fetching is deterministic; judging is AI.** All harvesting is plain Python hitting APIs and feeds. No LLM ever fetches, schedules, or remembers. The LLM only scores, merges ambiguous duplicates, and writes prose.
2. **The database is the memory.** State lives in SQLite, not in a prompt and not in the model's context.
3. **New-ness matters more than completeness.** The point is to notice what has *changed* since the last run — new announcements, new on-sale dates — not to re-list everything on every run.
4. **Never tell the user the same thing twice.** Suppression of already-seen and already-rejected events is a first-class feature, not a nice-to-have.
5. **Fail loudly, degrade gracefully.** If one source is down, the run continues with the others and the digest says which source failed. A silent partial run is worse than an error.

---

## Non-goals

Explicitly out of scope. Do not build these unless asked later.

- Buying tickets or automating checkout.
- Price tracking or resale-market monitoring.
- A web UI. Output is email, terminal, and calendar entries.
- Recommending events outside the configured radius.
- Anything requiring a login to a ticketing site.

---

## Architecture

Four stages, run in sequence by a single scheduled entry point.

```
  harvest  ->  normalise & dedupe  ->  score  ->  deliver
 (adapters)      (SQLite)          (LLM)     (email / ICS)
```

### Stage 1 — Harvest

One module per source, all implementing the same interface:

```python
class SourceAdapter(Protocol):
    name: str                     # short slug, e.g. "ticketmaster"
    def fetch(self, since: datetime | None) -> Iterable[RawEvent]: ...
```

`RawEvent` is a dataclass holding whatever the source gave us, plus `source_name` and `source_event_id`. Adapters must not write to the database — they yield, the pipeline writes. This keeps them independently testable against saved JSON fixtures.

Every adapter run is logged to a `source_run` table (see schema) with a status and a row count. If an adapter raises, the pipeline catches, records the failure, and carries on.

**Adapters, in build order:**

| Order | Source | Notes |
|---|---|---|
| 1 | Ticketmaster Discovery API | Free key from developer.ticketmaster.com. 5,000 calls/day, 5 req/sec, and deep paging is capped at 1,000 items (`size * page < 1000`) — so page carefully and slice by date window rather than trying to pull everything at once. Covers OVO Hydro, Armadillo, and most large theatre. |
| 2 | Skiddle | Free key from skiddle.com/api/join.php. UK-specific, better for smaller venues. Geo-search with `latitude` / `longitude` / `radius` (miles) — all three required together. Filter with `eventcode`: LIVE, FEST, THEATRE, COMEDY, CLUB. Note the promoter chooses the code, so it is unreliable — treat it as a hint, not a fact. |
| 3 | Venue ICS/RSS feeds | Many venues publish an iCalendar or RSS feed off their listings page. Cheap to add, high signal, no rate limits. See "Venue shortlist" below. |
| 4 | TMDB | Free API. Film release dates for the cinema horizon. Not showtimes — those come from venue feeds. |
| 5 | Annual anchors | Not a fetcher at all. A hand-maintained YAML file of recurring fixtures (see below). |

**Rate limiting:** a single shared token-bucket limiter, configured per adapter. Cache raw responses to disk for the duration of a run so re-running during development does not burn quota.

### Stage 2 — Normalise and dedupe

Every `RawEvent` becomes a normalised `Event` row.

**Fingerprinting.** The dedupe key is a SHA-256 of:
```
normalise(title) | normalise(venue_name) | event_date.date().isoformat()
```
where `normalise()` lowercases, strips punctuation, collapses whitespace, and removes a stoplist of noise tokens (`the`, `live`, `tour`, `presents`, `feat`, `+ support`, `an evening with`, `plus special guests`).

Exact fingerprint match = same event, merge. Near-miss (same venue, same date, title similarity above a threshold via `rapidfuzz`) = **candidate duplicate**: write both, flag the pair in a `duplicate_candidate` table, and let the LLM adjudicate in Stage 3. Do not silently merge on fuzzy matches — false merges lose events permanently and are very hard to notice.

**Venue resolution.** Maintain a `venue` table keyed by a normalised name, with lat/long and a `drive_minutes` field computed once from home. Do not compute travel time per event.

**Change detection.** On every upsert, compare against the stored row. If `on_sale_date`, `status`, or `price_range` changed, append a row to `event_change`. The digest is built from changes, not from the full event table.

### Stage 3 — Score

A single LLM pass per run over *new and changed* events only, never the full table.

Input: the taste profile (a markdown file, see `taste-profile.md`), plus a compact JSON array of candidate events (title, venue, date, category, price range, drive time, blurb).

Output: strict JSON, one object per event, no prose, no markdown fences:

```json
[
  {
    "event_id": 1234,
    "score": 0..100,
    "audience": "scott" | "both" | "partner",
    "reason": "one short sentence",
    "urgency": "none" | "on_sale_soon" | "selling_fast" | "last_chance"
  }
]
```

Batch in groups of roughly 40 events per call to keep responses parseable. Validate against a Pydantic model; on a parse failure, retry once with the error appended, then fall back to `score: null` and flag for manual review rather than crashing the run.

The LLM also adjudicates the `duplicate_candidate` pairs in a separate, smaller call — a plain "same event or not" with a reason.

**Scoring is advisory.** Hard constraints (radius, budget ceiling, blackout dates) are applied in Python *before* the events ever reach the model. Never rely on the prompt to enforce a rule you can enforce in code.

### Stage 4 — Deliver

Four outputs:

- **Weekly digest** — Sunday morning email. Grouped by horizon (this week / this month / on sale soon / announced for later). Only events with `score >= digest_threshold` and `verdict IS NULL`. Include a one-line reason per event and a direct booking link. Re-sends the full standing shortlist every run, not a delta — an event keeps reappearing until a verdict is set on it, re-bucketing into a nearer horizon as its date approaches.
- **Fortnight look-ahead** — a separate weekly email (own accent color, distinct from both the digest and the alert), covering events in the next 14 days scoring between `alert_threshold` and `digest_threshold`. Exists because the digest's score bar is permanent regardless of how soon the event is — without this, a moderate match could expire completely unseen just because its event date crept up while it stayed under the digest bar. Kept as its own email rather than a digest section specifically so it doesn't clutter the digest.
- **Urgent alert** — runs daily. Fires only for anything with an `on_sale_date` in the next 48 hours, or a status flip to `low_availability`. This is the highest-value output in the whole system; it should be short and hard to ignore.
- **ICS export** — shortlisted (`verdict` = `interested`/`booked`) events written as a plain `.ics` file via `events-agent calendar`, not a Google Calendar push — either household member imports it into whatever calendar app they use, with no OAuth flow to set up or maintain in a cron job. Provisional (`interested`) entries marked `STATUS:TENTATIVE`.

---

## Data model

SQLite. Use `sqlite3` from the standard library or SQLModel — do not reach for a heavyweight ORM.

**Multi-household split (added in Phase 4, ahead of schedule on purpose).** The
original design put `score`/`verdict`/`snoozed_until`/etc. directly on `event`.
That works for exactly one household — it breaks the moment a second
household's taste profile needs to score the *same* physical event
differently, or record its own independent verdict. So `event` (and
`venue`/`event_source`/`event_change`/`duplicate_candidate`) hold only
genuinely shared facts about an event, true regardless of who's looking at
it. Everything that's a judgment call — score, audience, verdict, snooze —
lives in `household_event_state`, keyed by `(household_id, event_id)`. There
is one `household` row today (seeded from `config.yaml` + `taste-profile.md`
on every `events-agent init`); the plan is to widen the harvest radius to
cover all of central Scotland and add more households filtering the same
shared catalog daily — `config.yaml`/`taste-profile.md` stay single-file for
now (cheap to turn into a `households/` directory later, unlike a schema
migration), and CLI commands (`score`, `digest`) already loop over every row
in `household` rather than assuming there's only one.

```sql
CREATE TABLE venue (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    name_normalised TEXT NOT NULL UNIQUE,
    city            TEXT,
    postcode        TEXT,
    latitude        REAL,
    longitude       REAL,
    type            TEXT              -- source-provided venue category, e.g. "bar", "theatre"
);

CREATE TABLE event (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    title           TEXT NOT NULL,
    title_normalised TEXT NOT NULL,
    category        TEXT,             -- theatre | music | cinema | comedy | other
    venue_id        INTEGER REFERENCES venue(id),

    event_date      TEXT,             -- ISO8601; NULL for announced-but-undated
    event_date_end  TEXT,             -- for runs and festivals
    announced_date  TEXT,
    on_sale_date    TEXT,             -- THE important one

    status          TEXT,             -- announced | on_sale | low_availability | sold_out | cancelled | past
    price_min       REAL,
    price_max       REAL,
    currency        TEXT DEFAULT 'GBP',
    url             TEXT,
    blurb           TEXT,

    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE INDEX idx_event_fingerprint ON event(fingerprint);
CREATE INDEX idx_event_date        ON event(event_date);
CREATE INDEX idx_event_onsale      ON event(on_sale_date);

-- One row per household (today: just the one). config.yaml stays the
-- human-edited source of truth; this row is a queryable materialization of
-- it, re-synced on every `events-agent init`.
CREATE TABLE household (
    id                  INTEGER PRIMARY KEY,
    label               TEXT NOT NULL,
    home_latitude       REAL NOT NULL,
    home_longitude      REAL NOT NULL,
    radius_miles        REAL NOT NULL,
    near_days           INTEGER,          -- digest horizon: "this week" cutoff
    month_days          INTEGER,          -- digest horizon: "this month" cutoff
    max_drive_minutes   INTEGER,
    price_ceiling       REAL,
    blackout_dates      TEXT,             -- JSON array of [start_iso, end_iso] pairs
    taste_profile_path  TEXT NOT NULL,
    digest_threshold    INTEGER,
    alert_threshold     INTEGER,
    email_to            TEXT,
    created_at          TEXT NOT NULL
);

-- Per-household view of an event: score, verdict, snooze. Two households
-- can (and will) score the same gig differently — this is the field people
-- leave out and then abandon the tool three weeks later. Build it in from
-- Phase 1 even before there is a way to set it.
CREATE TABLE household_event_state (
    household_id    INTEGER NOT NULL REFERENCES household(id),
    event_id        INTEGER NOT NULL REFERENCES event(id),

    -- scoring
    score           INTEGER,
    audience        TEXT,
    score_reason    TEXT,
    urgency         TEXT,
    scored_at       TEXT,

    -- user state
    verdict         TEXT,             -- NULL | interested | booked | no
    verdict_at      TEXT,
    snoozed_until   TEXT,
    surfaced_at     TEXT,             -- last time this appeared in a digest

    PRIMARY KEY (household_id, event_id)
);

CREATE INDEX idx_hes_verdict ON household_event_state(verdict);
CREATE INDEX idx_hes_score   ON household_event_state(score);

CREATE TABLE event_source (
    event_id        INTEGER REFERENCES event(id),
    source_name     TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    source_url      TEXT,
    raw_json        TEXT,
    PRIMARY KEY (source_name, source_event_id)
);

CREATE TABLE event_change (
    id              INTEGER PRIMARY KEY,
    event_id        INTEGER REFERENCES event(id),
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    detected_at     TEXT NOT NULL,
    notified_at     TEXT              -- set once the daily alert has surfaced this change
);

CREATE TABLE duplicate_candidate (
    id              INTEGER PRIMARY KEY,
    event_id_a      INTEGER REFERENCES event(id),
    event_id_b      INTEGER REFERENCES event(id),
    similarity      REAL,
    resolution      TEXT,             -- NULL | same | different
    resolved_at     TEXT
);

CREATE TABLE source_run (
    id              INTEGER PRIMARY KEY,
    source_name     TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT,             -- ok | failed | partial
    rows_fetched    INTEGER,
    error           TEXT
);
```

Hard constraints (radius, price ceiling, blackout dates) are applied in
Python against `household` — see `constraints.py` — before an event ever
reaches the model, never left to the prompt.

---

## Configuration

`config.yaml` — checked into git, no secrets:

```yaml
home:
  latitude: 55.9410
  longitude: -4.3170
  label: "Milngavie"
search:
  radius_miles: 25
  horizons:
    near_days: 7
    month_days: 31
    long_days: 270
constraints:
  max_drive_minutes: 60
  price_ceiling: 120           # per ticket, GBP
  blackout_dates: []
scoring:
  digest_threshold: 60
  alert_threshold: 45          # lower bar for on-sale alerts
delivery:
  email_to: ""
```

Cadence (when `alert`/`digest` actually run) lives in the launchd plist, not
here — see "Scheduling" below. Keeping it in one place only avoids the two
drifting out of sync.

`.env` — gitignored, never committed:

```
TICKETMASTER_API_KEY=
SKIDDLE_API_KEY=
TMDB_API_KEY=
ANTHROPIC_API_KEY=
SMTP_HOST=
SMTP_USER=
SMTP_PASSWORD=
```

`annual-anchors.yaml` — hand-maintained, the cheapest high-value file in the project:

```yaml
- name: Celtic Connections
  typical_month: january
  programme_announced: october
  watch_url: ""
- name: Glasgow Film Festival
  typical_month: february
  programme_announced: january
- name: Edinburgh Festival Fringe
  typical_month: august
  programme_announced: june
- name: Panto season
  typical_month: december
  programme_announced: august
```

The agent checks this file each run and, in the month before a `programme_announced` value, adds a line to the digest reminding you to look.

---

## Repository layout

```
events-agent/
├── CLAUDE.md                  # this brief, or a pointer to it
├── README.md
├── config.yaml
├── annual-anchors.yaml
├── taste-profile.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── src/events_agent/
│   ├── __init__.py
│   ├── cli.py                 # entry point: init | harvest | alert | score | digest | verdict | run
│   ├── config.py
│   ├── db.py                  # schema, migrations, upsert logic
│   ├── models.py              # RawEvent, Event, ScoreResult (Pydantic)
│   ├── normalise.py           # title/venue normalisation, fingerprinting
│   ├── dedupe.py
│   ├── constraints.py         # hard radius/price/blackout filter, per household
│   ├── scoring.py             # the LLM pass
│   ├── delivery/
│   │   ├── digest.py
│   │   ├── email.py           # smtplib sender shared by digest (and alert, if ever wanted)
│   │   ├── alert.py
│   │   └── ics.py
│   └── sources/
│       ├── base.py            # SourceAdapter protocol
│       ├── ticketmaster.py
│       ├── skiddle.py
│       ├── venue_ics.py
│       └── tmdb.py
└── tests/
    ├── fixtures/              # saved JSON responses, one per source
    ├── test_normalise.py
    ├── test_dedupe.py
    └── test_sources.py
```

---

## Build phases

Each phase ends in something that works and is committed. Do not start the next phase until the current one runs cleanly twice in a row.

### Phase 0 — Skeleton
Repo, `pyproject.toml`, config loading, `.env` handling, SQLite schema created by `events-agent init`, and a `taste-profile.md` written by hand.
**Done when:** `events-agent init` creates the database and `events-agent --help` lists the commands.

### Phase 1 — One source, no AI
Ticketmaster adapter. Fetch a 90-day window within 25 miles of home, normalise, upsert, print a table to the terminal.
**Done when:** running twice inserts nothing the second time (idempotent upsert), and the venue list contains the ones you expect — Hydro, Armadillo, King's, Theatre Royal, Barrowland.

### Phase 2 — Second source and dedupe
Skiddle adapter. Fingerprinting, `duplicate_candidate` detection, venue resolution across the two naming conventions.
**Done when:** an event listed on both sources appears once, and the near-miss cases are visible in `duplicate_candidate` rather than silently merged. Expect to find your normalisation stoplist is wrong here — that is the point of this phase.

### Phase 3 — The on-sale watcher
Change detection, `event_change` population, and the daily urgent alert. Backfill `on_sale_date` where the sources provide it and flag where they do not.
**Done when:** a genuine on-sale date change produces exactly one alert, and re-running produces none.

### Phase 4 — Scoring and digest
LLM scoring pass with strict JSON output and validation. Weekly digest email grouped by horizon. Suppression by `verdict` and `snoozed_until`.
**Done when:** a week's digest reads like something you would actually open, and marking something `no` keeps it out of next week's.

### Phase 5 — Calendar and ad-hoc
ICS export or Google Calendar push for shortlisted events. A `events-agent ask "anything on the last weekend in October?"` command that queries the database and answers in prose.
**Status: the `.ics` half is done** — `events-agent calendar` writes shortlisted
(`verdict` = `interested`/`booked`) events to a plain `.ics` file, provisional
entries marked `STATUS:TENTATIVE`. No Google Calendar OAuth push: a static
file imports into any calendar app without an auth flow to maintain in a
cron job, which satisfies the actual goal ("both people can plan around
them") more simply. **`events-agent ask` is deliberately left for the
backlog** — a real feature (free-text parsing + a new LLM prompt), not a
quick add-on, and not requested yet.

### Phase 6 — Venue feeds
Add ICS/RSS adapters for the venue shortlist. Deliberately last: it is the most fiddly and the least reusable work, and by this point the pipeline around it is stable.

---

## Venue shortlist (for Phase 6)

Check each for an ICS, RSS, or JSON listings feed before writing any scraper. Respect `robots.txt` and terms of service; prefer an official feed even where it is less complete.

Glasgow: OVO Hydro, SEC Armadillo, Theatre Royal, King's Theatre, Pavilion, Tron, Òran Mór, Barrowland Ballroom, King Tut's Wah Wah Hut, SWG3, St Luke's, Glasgow Film Theatre, Òran Mór (A Play, a Pie and a Pint).
Local: East Dunbartonshire Council events, Lillie Art Gallery, Milngavie Town Hall.
Wider: Usher Hall and Festival Theatre (Edinburgh) if the radius is widened.

---

## Testing notes

- Every adapter gets a saved JSON fixture in `tests/fixtures/`. Adapter tests must never hit the network.
- `test_normalise.py` is the highest-value test file in the project. Seed it with real ugly titles: `"Bruce Springsteen & The E Street Band"` vs `"Bruce Springsteen and the E Street Band - 2027 Tour"`.
- The scoring pass needs a test with a canned LLM response asserting the Pydantic model rejects malformed output rather than passing it through.

## Scheduling

Runs via `launchd`, mirroring the existing `com.scott.stockpicker` pattern on
this Mac (a `run_*.sh` wrapper in the project root, invoked by a plist in
`~/Library/LaunchAgents`, logging to `logs/`). Two jobs:

- **`com.scott.eventsagent.daily`** — 06:40 every day. Runs `run_daily.sh`,
  which calls `events-agent run` (harvest → score → alert). Piggybacks on
  the Mac's existing global `pmset repeat wakeorpoweron` wake at 06:25 —
  no separate wake needed for this one.
- **`com.scott.eventsagent.weekly`** — Sunday 19:00. Runs `run_weekly.sh`,
  which calls `events-agent digest` then `events-agent fortnight` —
  deliver-only, deliberately no harvest/score here (that already happened
  in the same day's 06:40 run; re-running it in the evening would just be a
  second LLM bill for the same data). No dedicated wake for this one — tried
  adding a second `pmset repeat` entry (a distinct `wake SU`/`wake U` event
  alongside the existing `wakeorpoweron`) and confirmed twice on this
  machine that `pmset repeat` won't hold two wake-family entries at once;
  the later one silently drops the earlier. Settled on relying on `pmset
  -g`'s observed `sleep 0` (idle system sleep disabled) instead — if that
  holds, the Mac never goes back to sleep after the 06:25 wake, so no
  second wake is needed. Verify by checking `logs/weekly.log` after the
  first few Sundays; if entries are missing, that assumption was wrong and
  this needs revisiting (a `poweron`-type entry was the untried fallback).

The known failure mode is the Mac sleeping through the scheduled time —
`launchd` does not fire missed jobs by default. `pmset -g sched` shows
what's actually active — currently just the single every-day 06:25 entry.
Not yet built: a `RunAtLoad` catch-up check (compare against the last
`source_run`/send timestamp and run anyway if overdue) for the case where
the Mac was off or asleep through an unexpected outage.

Build order change: Skiddle is the Phase 1 adapter. Ticketmaster moves to Phase 2 pending API key access.

Schema change: Phase 4 split `event`'s scoring/verdict columns into a new `household` + `household_event_state` pair (see "Data model" above) — done during Phase 4 itself, ahead of any second household actually existing, specifically to avoid migrating live data later.