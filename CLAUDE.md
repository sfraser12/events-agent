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
| 3 | Venue ICS/RSS feeds | Many venues publish an iCalendar or RSS feed off their listings page. Cheap to add, high signal, no rate limits. See "Venue shortlist" below — researched 2026-08-26, nothing buildable found (see Phase 6). |
| 4 | TMDB | Free API. Film release dates for the cinema horizon. Not showtimes — those come from venue feeds. |
| 5 | Annual anchors | Not a fetcher at all. A hand-maintained YAML file of recurring fixtures (see below). |
| 6 | Google Alerts (`sources/google_alerts.py`) | Added 2026-08-26 for content that doesn't fit a ticketed-event API — spa/wellness deals per taste-profile.md's Wellness section. No public API creates an alert; set one up manually at google.com/alerts (choose "RSS feed" under "Deliver to" — despite the label, the response is actually Atom, confirmed against a real feed; the adapter parses accordingly), configure it under `google_alerts:` in `households/<name>/config.yaml` with the venue it covers and that venue's real coordinates. Yields undated, priceless RawEvents by design — taste-profile.md tells the scoring LLM to score this category generously given the thin data. **Also covers alerts with no single physical venue** (a deal site, a promoter, a search term like "an evening with Glasgow tickets") — no adapter changes needed for this: give it a descriptive `venue_name` (e.g. "Various — Wowcher Spa Deals") and leave `latitude`/`longitude` blank. `constraints.event_matches_household()` already skips the radius check entirely when coordinates are absent (same "missing data never excludes" rule everything else follows), so a topic-based alert is never geographically rejected — verified 2026-08-27, not just assumed. |

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

Five outputs:

- **Weekly digest** — Sunday morning email. Grouped by horizon (this week / this month / on sale soon / announced for later). Only events with `score >= digest_threshold` and `verdict IS NULL`. Include a one-line reason per event and a direct booking link. Re-sends the full standing shortlist every run, not a delta — an event keeps reappearing until a verdict is set on it, re-bucketing into a nearer horizon as its date approaches.
- **Fortnight look-ahead** — a separate weekly email (own accent color, distinct from both the digest and the alert), covering events in the next 14 days scoring between `alert_threshold` and `digest_threshold`. Exists because the digest's score bar is permanent regardless of how soon the event is — without this, a moderate match could expire completely unseen just because its event date crept up while it stayed under the digest bar. Kept as its own email rather than a digest section specifically so it doesn't clutter the digest.
- **Urgent alert** — runs daily. Fires only for anything with an `on_sale_date` in the next 48 hours, or a status flip to `low_availability`. This is the highest-value output in the whole system; it should be short and hard to ignore.
- **ICS export** — shortlisted (`verdict` = `interested`/`booked`) events written as a plain `.ics` file via `events-agent calendar`, not a Google Calendar push — either household member imports it into whatever calendar app they use, with no OAuth flow to set up or maintain in a cron job. Provisional (`interested`) entries marked `STATUS:TENTATIVE`. One file per household once there's more than one (`curtainup-<label-slug>.ics`); the plain `curtainup.ics` default only applies with a single household, to avoid one household's file silently overwriting another's.
- **Admin stats email** (added 2026-08-31, via `events-agent status`) — not household-facing, never sent to `household.email_to`; goes to `Secrets.admin_email` (`ADMIN_EMAIL` in `.env`, falls back to `SMTP_USER`) so it can never accidentally multiply the moment a second household exists. Built to actually answer "how is this scaling, what does it cost, do we need to rearchitect" from real data rather than scrollback: `source_run` counts per adapter over the last 7 days (Ticketmaster/Skiddle/Google Alerts call volume — the practical concern there is the 5,000/day Ticketmaster ceiling, not $ cost, since both APIs are free), `llm_usage` totals per model with an estimated USD cost (Sonnet 5 pricing confirmed via the `claude-api` skill, not guessed — a model missing from the pricing table reports tokens with no cost estimate rather than a wrong number), a same-window breakdown by household/context, and an all-time catalog snapshot (event/venue counts, DB file size). Piggybacks on the Sunday weekly job (`run_weekly.sh`, right after `digest`) rather than getting its own plist/wake — low-urgency, and that slot already has a working wake.

**Marketing names** (2026-08-29, everywhere a household actually sees the email — subject line, colored mark chip, plain-text header): Urgent alert → **Last Call**, Weekly digest → **Shortlist**, Fortnight look-ahead → **Understudy** (briefly named "Second Chance", changed same day — "Understudy" says what it actually is, the near-miss backup pick, rather than a vague "another go"). The names above (Weekly digest/Fortnight look-ahead/Urgent alert) stay as the internal/architectural terms in this doc and in code comments — only the user-facing strings (`cli.py` subject lines, `mark_suffix` in `alert.py`/`digest.py`/`lookahead.py`, plain-text headers) changed. Brand/type separator is always an en dash (`–`/`&ndash;`, not the wider em dash `—` — swapped 2026-08-30, the em dash read too wide at this size), applied consistently in every user-facing string: `Curtain Up – <Type>` (chip), `Curtain Up – <Type> – <description>` (subject line, plain-text header) — previously the subject lines omitted the first dash while the chip had it, an inconsistency the user caught by eye in their inbox.

**Straplines** (2026-08-29): each email type also carries a fixed marketing strapline, distinct from the dynamic functional subtitle line underneath it (`shell()`'s `strapline` vs `subtitle` params in `email_design.py`) — the strapline explains what this email type is *for* (same every send), the subtitle says what's actually *in* this particular send. Last Call: "On sale soon, selling fast — act today". Shortlist: "Handpicked for your taste". Understudy: "Not quite Shortlist material, but worth a peek".

### The "worth a special trip" tier (added 2026-08-26)

Added after the user said he'd travel to Argyll & Bute, Skye, or "elsewhere in Scotland" for a genuinely good event — `radius_miles` as a flat hard cutoff can't express that; anything past it was discarded before scoring ever saw it. Two new **optional** household fields, both `NULL` by default (feature off unless configured):

- `far_radius_miles` (config: `search.far_radius_miles`) — an outer boundary. An event beyond `radius_miles` but within this is no longer a hard reject in `constraints.event_matches_household()` — it still reaches scoring.
- `far_threshold` (config: `scoring.far_threshold`) — the score bar a far-flung event must clear to actually surface, applied in Python at digest-selection time, deliberately higher than `digest_threshold` (Scott: 60 → 85). Routine-good three hours away shouldn't clutter the digest the way routine-good twenty minutes away does.

**A plain circle is the wrong shape for "the rest of Scotland."** Confirmed against real harvested data: a 200mi circle around Milngavie is ~70% North West England/Northern Ireland (Blackpool, Manchester, Scarborough, even Belfast — which isn't reachable by road at all, haversine doesn't know about the Irish Sea), because Scotland's north-south extent puts Skye at roughly the same straight-line distance as Blackpool. A third optional field, `far_min_latitude` (config: `search.far_min_latitude`, Scott: `55.0`), is a rough "north of about here" floor applied only within the far-flung branch — an approximation of the England border, not a real region lookup, chosen because it cut the real would-reach-the-LLM candidate count from **4,076 to 318** on first run against live data (verified, not just reasoned about — see [[project_cost_sensitivity]] pattern in memory).

**Harvest**: `cmd_harvest` adds a second Ticketmaster-only pass at `far_radius_miles` when configured (`sources/ticketmaster.py` reused as-is, no new adapter — instance's `.name` overridden to `"ticketmaster_wide"` for clean log/status output). Ticketmaster only, not Skiddle: Ticketmaster skews toward bigger, more "worth a special trip" caliber acts; Skiddle skews toward small club nights/local promoters that would just be noise at this radius. Same real event caught by both passes upserts once (fingerprint dedupe) — harmless.

**Digest**: far-flung events that clear the bar get a distinct purple "worth the trip · ~Nh drive" badge (`FARFLUNG`/`FARFLUNG_BG` in `email_design.py`) inside their normal horizon section — not a separate email, not a separate horizon grouping, just a visual tag so it doesn't read as an ordinary local pick.

**Known one-time cost**: the first wide-radius harvest pulled in ~5,600 new event rows; the constraint filter (with `far_min_latitude`) keeps the actual LLM-scoring hit to a bounded ~318 for that first run. Subsequent runs settle back near baseline — the `last_seen`-only-advances-on-real-change fix (see [[project_cost_sensitivity]]) means already-scored/already-excluded events don't get rescored for free.

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

-- One row per household. households/<name>/config.yaml stays the
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
    far_radius_miles    REAL,             -- "worth a special trip" tier, NULL = off — see Architecture
    far_threshold       INTEGER,          -- score bar for the far tier, above digest_threshold
    far_min_latitude    REAL,             -- rough Scotland-vs-England floor for the far tier
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

-- One row per real LLM API call (added 2026-08-31, for cost/scale
-- monitoring — see "Admin stats email" below). context is the household
-- label for a scoring call, or the literal 'duplicate_adjudication' for
-- the catalog-level adjudication pass.
CREATE TABLE llm_usage (
    id                          INTEGER PRIMARY KEY,
    context                     TEXT NOT NULL,
    model                       TEXT NOT NULL,
    input_tokens                INTEGER NOT NULL,
    output_tokens               INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
    created_at                  TEXT NOT NULL
);
```

Hard constraints (radius, price ceiling, blackout dates) are applied in
Python against `household` — see `constraints.py` — before an event ever
reaches the model, never left to the prompt.

---

## Configuration

`households/<name>/config.yaml` — one per household (`households/scott/`,
`households/brother/`, ...), gitignored (personal, not checked in — see
"Multi-household split" in Data model below). `config.example.yaml` at the
repo root is the checked-in template to copy from:

```yaml
home:
  latitude: 55.9410
  longitude: -4.3170
  label: "Milngavie"
search:
  radius_miles: 25
  # far_radius_miles: 200      # optional "worth a special trip" tier, off by default — see Architecture
  # far_min_latitude: 55.0     # optional companion floor — a plain radius circle isn't shaped like Scotland
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
  # far_threshold: 85          # score bar for the far tier, deliberately above digest_threshold
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
ADMIN_EMAIL=
```

`ADMIN_EMAIL` is optional — falls back to `SMTP_USER` if blank. Only used by `events-agent status` (see "Admin stats email" under Stage 4 — Deliver); deliberately not part of any household's config.

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
│       └── google_alerts.py   # not venue_ics.py/tmdb.py — see Phase 6 (no venue feed was buildable) and note below (TMDB never built)
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
ICS export for shortlisted events.
**Status: done.** `events-agent calendar` writes shortlisted
(`verdict` = `interested`/`booked`) events to a plain `.ics` file, provisional
entries marked `STATUS:TENTATIVE`. No Google Calendar OAuth push: a static
file imports into any calendar app without an auth flow to maintain in a
cron job, which satisfies the actual goal ("both people can plan around
them") more simply. The free-text `events-agent ask` command originally
scoped for this phase was **dropped (2026-08-29)** — decided not needed.

### Phase 6 — Venue feeds
**Status: researched (2026-08-26), no adapters built — a deliberate outcome, not an abandoned phase.**

Checked all 15 shortlisted venues for an ICS/RSS/JSON feed (per the "prefer
an official feed over a scraper" principle) plus a second round on See
Tickets, Eventim, Glasgow Life, and local cinemas (Omniplex Clydebank,
Everyman, Vue St Enoch's, GSC IMAX) after the user asked about them
directly. Findings:

- **Only one real feed exists: Òran Mór** (RSS 2.0, `oran-mor.co.uk/events/feed/`,
  WordPress Modern Events Calendar plugin — real title/date/price/blurb/
  category fields, confirmed working). But it's **already covered** by the
  existing Ticketmaster/Skiddle harvest (176 events already in the DB) — an
  adapter here would mostly duplicate data already flowing in.
- **Theatre Royal Glasgow and King's Theatre Glasgow are on ATG's platform,
  which blanket-blocks Claude-class bots in `robots.txt`** — off-limits
  regardless of ambition. (Theatre Royal is separately already covered by
  Ticketmaster/Skiddle; King's Theatre Glasgow is a genuine gap with no
  legal way to close it via a feed.)
- Everything else on the shortlist (Hydro, Armadillo, Barrowland, SWG3, King
  Tut's, St Luke's, Pavilion, Tron [feed exists but the Events post type
  isn't wired into WP's output — zero `<item>`s], Glasgow Film Theatre
  [GraphQL exists but robots.txt-disallowed], East Dunbartonshire Council,
  Lillie Art Gallery) has **no usable feed** — most are already covered by
  Ticketmaster/Skiddle anyway; the genuine gaps are Armadillo, King's
  Theatre Glasgow, Tron, Pavilion, GFT, and the hyperlocal council/gallery
  listings, none of which have a clean source to build against.
- **See Tickets and Eventim** both gate their APIs behind a partner/
  affiliate application — no open self-serve tier like Ticketmaster's
  Discovery API. **Glasgow Life** and Glasgow's open-data portal have no
  events feed. **Cinema showtimes** (the TMDB gap — release dates only, no
  showtimes) have no free public API for Omniplex/Everyman/Vue; only paid
  commercial aggregators (MovieGlu etc.) or account-scoped platforms (Ticket
  Tailor for GSC's IMAX, wrong ownership) exist.

**Net result:** nothing to build right now without either violating
robots.txt/ToS, paying for a commercial API, or duplicating existing
coverage. Revisit only if a venue changes platforms or a partner
application becomes worth pursuing.

### Phase 7 — Beyond the build (backlog, not started)
Not a build phase with a concrete "done when" like 0–6 — a standing checklist
for how the tool keeps evolving once the core pipeline is stable. Deliberately
not started; revisit as a group once Phase 6 is done and running cleanly,
rather than picking any of these off ad hoc mid-build.

- **Source gaps surfaced while writing `taste-profile.md`.** Currently the
  top priority — see `google_alerts_todo.csv` (gitignored, local only) for
  the live, working queue of these. None of Skiddle/Ticketmaster/venue feeds
  reliably cover: spa/sauna/wellness deals and openings — **built
  2026-08-26**, `sources/google_alerts.py`, see the Stage 1 adapter table
  above, now **active with 13 live feeds** as of 2026-08-29 (Portavadie,
  Taymouth Marina, Cameron House, Fonab Castle Hotel, Crieff Hydro,
  Gleneagles, Peebles Hydro, Dunblane Hydro, Stobo Castle, Trump Turnberry,
  Cromlix House Hotel, and two Wowcher deal-site searches) — verified
  fetching/parsing cleanly, real content not yet confirmed (see
  [[project_source_gaps]] in memory). Eventbrite
  (tech/space/environment talks); Facebook Events (one-off local
  screenings — **researched 2026-08-26: essentially a dead end**, Facebook
  locked down public RSS/API access to Groups and Events years ago, no
  ToS-compliant way to pull listings without being a logged-in member);
  major one-off speaker/celebrity-talk platforms, Fane Productions-style
  (wants on-sale dates treated with the same urgency as a gig on-sale). Also
  now confirmed gaps (see Phase 6 above): See Tickets, Eventim, Glasgow
  Life, and cinema showtimes (Omniplex/Everyman/Vue) — all partner-gated or
  feedless, not self-serve buildable. Equestrian, Scottish fire/Viking
  festivals, and classic Mini events (including the Thistle Run) added
  2026-08-28 — same CSV, same workflow: fill in `feed_url` after creating
  the alert at google.com/alerts and hand it back. **Waverley (the last
  seagoing paddle steamer, Clyde/UK-wide sailings) added 2026-08-30** —
  checked `waverleyexcursions.co.uk` first since real sailing dates/times/
  prices are a much better fit for a proper feed than a topic-based alert;
  confirmed no RSS/iCal/API, timetables are PDF-only. Same CSV, same
  workflow.
- **Second household.** Brother (Edinburgh) becomes household #2.
  **`households/` directory migration: done.** `cli.py`/`config.py` moved off
  single-file config — `households/<name>/config.yaml` +
  `households/<name>/taste-profile.md`, one subdirectory per household,
  stable ids from an explicit `HOUSEHOLD_IDS` map in `cli.py` (`scott` = 1,
  `brother` = 2) so a new household can never reassign an existing one's id.
  `cmd_init` skips any household whose files aren't both present yet —
  everything keeps running for whichever households *are* configured.
  **Harvest is per-household now (fixed 2026-08-31, was a known gap):**
  `cmd_harvest` used to be a single shared fetch anchored on `households/
  scott/config.yaml` alone — fine by luck (Scott's 90mi Milngavie radius
  already reaches Edinburgh) but meant a household's own `far_radius_miles`
  tier or Google Alerts feeds were silently never fetched at all. Now loops
  every household with both config.yaml and taste-profile.md present (same
  presence check as `cmd_init`) and builds each one's own Skiddle/
  Ticketmaster/Ticketmaster-wide/Google-Alerts adapters from that
  household's own config — merged into the one shared catalog as before
  (dedupe is by fingerprint regardless of which household's pass found an
  event first). Adapter names only pick up a `_<household>` suffix once
  there's more than one household configured (e.g. `ticketmaster_brother`)
  — for Scott alone today, names and `source_run` history are unchanged
  from before this fix; verified live against production, zero behavior
  change with only one household configured. **Still not done:** the actual
  `households/brother/config.yaml` + `taste-profile.md` — waiting on the
  brother's own taste-profile Q&A session (he writes his own, not drafted
  secondhand) — and deciding his radius/price ceiling/digest
  thresholds/email once that happens.
- **A real domain + "from" address.** Emails are Curtain Up-branded now
  (display text; the domain itself stays the compact "curtainup", no
  space);
  `curtainup.io` and `curtainup.co` were both unregistered as of 2026-08-26
  if a proper domain/mailbox is ever wanted instead of a personal Gmail
  "from" address.
- **"Worth a special trip" tier: done (2026-08-26).** See "The 'worth a
  special trip' tier" under Architecture above for the full design — the
  short version: `far_radius_miles`/`far_threshold`/`far_min_latitude` on
  `household`, a second Ticketmaster-only wide-radius harvest pass, and a
  stricter score bar applied at digest time so a genuinely exceptional
  event in Skye or Argyll & Bute can surface without flooding the digest
  with routine events hours away. Live in Scott's config now
  (`far_radius_miles: 200`, `far_threshold: 85`, `far_min_latitude: 55.0`).
- **Council/regional listings for Argyll & Bute, Highland (incl. Skye), and
  similar — researched 2026-08-27, no adapter built.** Same conclusion as
  Phase 6: real content, no feed anywhere.
  - **Argyll & Bute Council**: only a general news RSS (`/news`), nothing
    events-specific. Their culture/arts page just links out to third
    parties (Wild About Argyll, Live Argyll) — both real, both list actual
    dated events, neither has a feed (Live Argyll's `robots.txt` is fully
    open, so not blocked, just nothing machine-readable).
  - **Highland Council**: an `/events` page exists (Drupal-style, has a
    "submit event" form) but currently has no content and no feed.
  - **An Lanntair** (Stornoway): real site, real dated events, no feed.
    Confirmed via the database this isn't already covered by
    Skiddle/Ticketmaster — a genuine gap, just not a closeable one.
  - **VisitScotland / The List**: The List is still operating in 2026
    (festival guides), no public events feed found.
  - **Data Thistle (datathistle.com) — found, deliberately not pursued.** A
    real UK-wide events aggregator (10,000+ venues) with a genuine free
    self-serve tier (RSS/iCal feeds, API, no sales call). Would plausibly
    cover small Highland/Argyll & Bute venues. **Its `robots.txt`
    specifically blanket-blocks ClaudeBot, GPTBot, and CCBot by name** —
    not a generic bot rule, a deliberate one. Declined on the same
    principle as everything else here: respect what the operator's
    `robots.txt` is actually saying, even when the data would be useful and
    even though the block is arguably more about the site itself than an
    API endpoint. Don't revisit this without the operator's explicit
    permission through some other channel.
  Google Alerts (already built, see Architecture above) remains the only
  feed-respecting way to get signal on any of these regions — several
  candidate search terms are in `google_alerts_todo.csv` (gitignored,
  local only).
- **Delisting detection: done (2026-08-29).** Triggered by a user report of a
  dead Ticketmaster booking link (`.../armand-van-helden-.../event/...`
  returned a bot-detection 401 for curl — confirmed universal across every
  Ticketmaster URL tested, including known-good ones, so not the actual root
  cause). The real gap: nothing ever re-checked whether a previously-seen
  event was still actually listed by its source — an event sold out and
  pulled from Ticketmaster/Skiddle just sat in the DB as `on_sale` forever,
  since neither source reliably flags removal via a status code. Fixed with
  a new `event.last_confirmed_at` column (unlike `last_seen`, advances on
  every successful upsert regardless of content change) and
  `mark_delisted_events()` in `db.py`, run at the end of `cmd_harvest`
  alongside `mark_past_events` — an active, dated event not reconfirmed by
  any source for `DELIST_AFTER_DAYS` (4) gets flipped to `cancelled` (reused
  rather than a new status value, since digest/lookahead/alert already
  exclude it). Guarded to only run when at least one full-catalog source
  (ticketmaster/skiddle) actually succeeded that run, so an API outage can
  never masquerade as mass delisting. Verified live against production data
  the same day: 173 events correctly flagged, all with `last_seen` several
  days stale, several dated that same day for small club nights Skiddle had
  clearly pulled — no false positives. Paired with a UX-side fix in
  `email_design.py`'s `cta_cell()`: every Book button now also gets a small
  "link not working?" fallback (a Google search for title + venue), so a
  dead link — from this cause or any other — always has an escape hatch
  rather than a dead end.
- **`annual-anchors.yaml` checking — done (2026-08-31).** Discovered
  2026-08-28 as documented-but-never-built (the "Configuration" section
  above and `annual-anchors.example.yaml` described the feature, but the
  real file didn't exist and no code read it). Now implemented:
  `annual_anchors.py` (`load_annual_anchors` + `due_reminders` — an anchor
  is due through the single calendar month before its `programme_announced`
  month, wrapping year boundaries correctly), wired into `cmd_digest`,
  rendering a reminder banner above the horizon sections in Shortlist
  (HTML and plain text) when something's due. `annual-anchors.yaml` itself
  is gitignored, same as `config.yaml` — seeded from
  `annual-anchors.example.yaml`'s four entries (Celtic Connections, Glasgow
  Film Festival, Edinburgh Festival Fringe, Panto season). Scottish
  fire/Viking festivals (Up Helly Aa, Beltane, Stonehaven Fireballs,
  Flambeaux — see `google_alerts_todo.csv`) are still worth reconsidering
  as anchors here instead of Google Alerts, since this mechanism now
  actually exists — not done yet, a fixed calendar date isn't quite the
  same shape as a "programme announced" reminder and would need a second
  anchor type to fit properly.

**Why:** avoids scope-creep mid-build — Phase 6 is the last phase with a
fixed deliverable; everything here is open-ended tool evolution, not a
build step.

---

## Venue shortlist (for Phase 6)

**Already checked — see Phase 6 above for the per-venue findings** (2026-08-26). Kept here as the historical record of what was checked, not a to-do list.

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
`~/Library/LaunchAgents`, logging to `logs/`). Three jobs:

- **`com.scott.eventsagent.daily`** — 06:40 every day. Runs `run_daily.sh`,
  which calls `events-agent run` (harvest → score → alert). Piggybacks on
  the Mac's existing global `pmset repeat wakeorpoweron` wake at 06:25 —
  no separate wake needed for this one.
- **`com.scott.eventsagent.weekly`** — Sunday **18:45** (not 19:00 — a doc/
  plist mismatch caught 2026-08-30). Runs `run_weekly.sh`, which calls
  `events-agent digest` only — deliver-only, deliberately no harvest/score
  here (that already happened in the same day's 06:40 run; re-running it in
  the evening would just be a second LLM bill for the same data).
- **`com.scott.eventsagent.fortnight`** — Wednesday 18:45. Runs
  `run_fortnight.sh`, which calls `events-agent fortnight`. Split out from
  the weekly job on 2026-08-29: both used to fire in the same Sunday run,
  landing two emails (Shortlist and Understudy) at once — moved to its own
  Wednesday slot instead, maximally spaced from the Sundays either side.
  Its plist deliberately has **no `RunAtLoad`**: with no prior
  `.last_attempt_fortnight` marker the first time this loads, the catch-up
  guard below would have nothing to compare against and would fire an
  unwanted immediate send. (`run_fortnight.sh` still contains the same
  catch-up-guard code as the other two scripts, but it's currently inert
  since nothing triggers `RunAtLoad` for this job — add the key back once a
  real marker exists from a genuine Wednesday run.)

**Real hardware wake, not just an idle-sleep assumption (fixed 2026-08-30).**
The original design for the weekly/fortnight jobs relied on `pmset -g`
showing `sleep 0` (idle system sleep disabled) to guarantee the Mac stayed
awake from the 06:25 daily wake through the 18:45 evening slot — no
dedicated wake mechanism of its own. That assumption broke in practice: on
2026-08-30 the Mac went into a real system sleep mid-afternoon (confirmed via
`pmset -g log` — `powerd` restarted at 19:18, hours after the 18:45 slot had
already silently fired and been dropped by `launchd`) and the weekly job was
missed. A second `pmset repeat` wake entry was tried as the fix and rejected
again — confirmed via `man pmset` this time, not just trial and error: *"you
may only have one pair of repeating events scheduled"*, system-wide, full
stop.

The actual fix: `pmset schedule` (singular one-time wake events, a different
subsystem from `repeat` with no such one-pair limit) can hold as many
future entries as needed alongside the existing daily `repeat` entry. Each
of `run_weekly.sh` and `run_fortnight.sh` now ends by computing its own
*next* occurrence (next Sunday / next Wednesday, 18:30 — 15 min before its
18:45 `StartCalendarInterval`, mirroring the daily job's 06:25→06:40 gap)
and calling `sudo -n /usr/bin/pmset schedule wakeorpoweron "<that time>"` —
self-perpetuating, since every real run re-arms the next one. This needs
root, which normally means an interactive password `launchd` can't supply;
solved with a narrowly-scoped passwordless sudo rule in
`/etc/sudoers.d/eventsagent-pmset`:
```
scottfraser ALL=(root) NOPASSWD: /usr/bin/pmset schedule wakeorpoweron *
```
This grants nothing beyond that one exact subcommand — `pmset schedule
cancel ...` and everything else still prompts for a password normally. If
that sudoers file is ever removed, the `pmset schedule` calls in the
wrapper scripts fail silently under `sudo -n` (the log line still gets
written, just as a permission error) and the weekly/fortnight jobs quietly
revert to depending on the Mac happening to be awake — check
`logs/weekly.log`/`logs/fortnight.log` if either job goes quiet again.
`pmset -g sched` shows both the daily repeat entry and the live one-time
entries under "Scheduled power events".

The known failure mode is still the Mac sleeping through a scheduled
time that nothing wakes it for — now only really a risk if the sudoers
rule above is ever lost.

**`RunAtLoad` catch-up.** The daily and weekly plists set `RunAtLoad`, so
each wrapper script also runs on every `launchctl load`/reboot/login, not
just its `StartCalendarInterval` (the fortnight plist deliberately doesn't,
see above). Each script guards this with a marker file
(`logs/.last_attempt_daily` / `.last_attempt_weekly` / `.last_attempt_fortnight`,
a plain epoch-seconds timestamp) written after every attempt regardless of
exit code — if the marker is younger than `CATCHUP_HOURS`, the script exits
immediately; only a genuinely missed scheduled slot (Mac off/asleep through
it) is older than that and triggers a real run. Marked on attempt rather
than success so a persistently failing step gets retried once per window
rather than on every reboot. `CATCHUP_HOURS` is **20** for daily (interval
24h) and **150** for weekly/fortnight (interval 168h) — deliberately just
under the real schedule interval in each case; a value equal to or above it
(36h and 192h were both tried and both wrong) makes every legitimate
scheduled firing look like a recent duplicate of itself and silently
suppresses all future real runs after the first one, which is exactly what
happened before these values were corrected.

Build order change: Skiddle is the Phase 1 adapter. Ticketmaster moves to Phase 2 pending API key access.

Schema change: Phase 4 split `event`'s scoring/verdict columns into a new `household` + `household_event_state` pair (see "Data model" above) — done during Phase 4 itself, ahead of any second household actually existing, specifically to avoid migrating live data later.