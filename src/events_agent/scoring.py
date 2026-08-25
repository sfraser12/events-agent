"""Stage 3 — Score. LLM scoring pass + duplicate_candidate adjudication.

Hard constraints (radius, price ceiling, blackout dates) are applied in
Python via constraints.py before anything reaches the model — see CLAUDE.md's
"never rely on the prompt to enforce a rule you can enforce in code." Only
new-or-changed events are ever scored, never the full table (Guiding
Principle 3), and one household's taste never affects another's score
(household_event_state, not a column on event).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from events_agent.constraints import estimate_drive_minutes, event_matches_household
from events_agent.db import list_households_as_dicts, upsert_score
from events_agent.models import DuplicateAdjudication, ScoreResult

SCORE_BATCH_SIZE = 40
DUPLICATE_BATCH_SIZE = 20
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096

SCORE_SYSTEM_PROMPT = """\
You score events for a household's weekly what's-on digest against their taste profile.

Score every event in the input array 0-100 using the household's own scoring
guidance in their taste profile if given, otherwise: 90-100 book immediately,
70-89 strong candidate, 50-69 worth a mention if the week is quiet, below 50
do not surface. When genuinely unsure, score low — a digest that's trusted
beats one that catches everything and gets skimmed.

Output ONLY a JSON array, one object per input event, each with exactly these
keys: event_id (int, must match an input event_id), score (int 0-100),
audience ("scott" | "both" | "partner"), reason (one short sentence), urgency
("none" | "on_sale_soon" | "selling_fast" | "last_chance"). No prose, no
markdown code fences, nothing outside the JSON array."""

DUPLICATE_SYSTEM_PROMPT = """\
For each pair of event listings below, decide whether they describe the same
real-world event — e.g. a "Venue Premium" or VIP-package listing of the same
show, or the same gig posted twice — or are genuinely different events that
happen to share a venue and date.

Output ONLY a JSON array, one object per input pair, each with exactly these
keys: pair_id (int, must match an input pair_id), same_event (bool), reason
(one short sentence). No prose, no markdown code fences, nothing outside the
JSON array."""


class LLMClient(Protocol):
    """The one method this module needs from an Anthropic client — lets
    tests substitute a fake without importing the real SDK."""

    def create_message(self, system: str, user_content: str) -> str: ...


class AnthropicLLMClient:
    """Wraps anthropic.Anthropic to the narrow LLMClient shape above."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def create_message(self, system: str, user_content: str) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


def run_scoring(conn: sqlite3.Connection, client: LLMClient) -> dict[str, Any]:
    """Score every household's new-or-changed events, then adjudicate
    duplicate_candidate once (a catalog fact, not a per-household one)."""
    summary: dict[str, Any] = {"households": {}, "duplicates_adjudicated": 0}
    households = list_households_as_dicts(conn)
    for household in households:
        summary["households"][household["label"]] = run_scoring_for_household(conn, client, household)
    summary["duplicates_adjudicated"] = adjudicate_duplicates(conn, client)
    return summary


def run_scoring_for_household(conn: sqlite3.Connection, client: LLMClient, household: dict[str, Any]) -> dict[str, int]:
    candidate_ids = select_scoring_candidates(conn, household["id"])
    taste_profile = Path(household["taste_profile_path"]).read_text()

    scored = 0
    excluded = 0
    failed = 0
    to_score: list[int] = []
    now = datetime.now(UTC).isoformat()

    for event_id in candidate_ids:
        event_row = _get_event_for_scoring(conn, event_id)
        if event_row is None:
            continue
        if event_matches_household(
            venue_latitude=event_row["venue_latitude"],
            venue_longitude=event_row["venue_longitude"],
            price_min=event_row["price_min"],
            price_max=event_row["price_max"],
            event_date=event_row["event_date"],
            home_latitude=household["home_latitude"],
            home_longitude=household["home_longitude"],
            radius_miles=household["radius_miles"],
            price_ceiling=household["price_ceiling"],
            blackout_dates_json=household["blackout_dates"],
        ):
            to_score.append(event_id)
        else:
            upsert_score(
                conn,
                household["id"],
                event_id,
                score=None,
                audience=None,
                score_reason="Excluded by a hard constraint (radius, price ceiling, or blackout dates).",
                urgency=None,
                scored_at=now,
            )
            excluded += 1

    for batch_start in range(0, len(to_score), SCORE_BATCH_SIZE):
        batch_ids = to_score[batch_start : batch_start + SCORE_BATCH_SIZE]
        payload = [_build_candidate_payload(conn, household, event_id) for event_id in batch_ids]
        results, error = _score_batch(client, taste_profile, payload)
        scored_at = datetime.now(UTC).isoformat()
        if results is not None:
            results_by_id = {r.event_id: r for r in results}
            for event_id in batch_ids:
                result = results_by_id.get(event_id)
                if result is None:
                    upsert_score(
                        conn, household["id"], event_id,
                        score=None, audience=None,
                        score_reason="Model response did not include this event.",
                        urgency=None, scored_at=scored_at,
                    )
                    failed += 1
                    continue
                upsert_score(
                    conn, household["id"], event_id,
                    score=result.score, audience=result.audience,
                    score_reason=result.reason, urgency=result.urgency,
                    scored_at=scored_at,
                )
                scored += 1
        else:
            for event_id in batch_ids:
                upsert_score(
                    conn, household["id"], event_id,
                    score=None, audience=None,
                    score_reason=f"Scoring failed, needs manual review: {error}",
                    urgency=None, scored_at=scored_at,
                )
                failed += 1

    return {"scored": scored, "excluded": excluded, "failed": failed}


def select_scoring_candidates(conn: sqlite3.Connection, household_id: int) -> list[int]:
    """New-or-changed events for this household — never the full table.
    Excludes events that have already happened or been called off; there's
    nothing useful to score there."""
    rows = conn.execute(
        """
        SELECT e.id FROM event e
        LEFT JOIN household_event_state hes ON hes.event_id = e.id AND hes.household_id = ?
        WHERE e.status NOT IN ('past', 'cancelled')
          AND (hes.event_id IS NULL OR e.last_seen > hes.scored_at)
        """,
        (household_id,),
    ).fetchall()
    return [row[0] for row in rows]


def adjudicate_duplicates(conn: sqlite3.Connection, client: LLMClient) -> int:
    pairs = conn.execute(
        """
        SELECT dc.id, ea.title, va.name, ea.event_date, ea.blurb, eb.title, vb.name, eb.event_date, eb.blurb
        FROM duplicate_candidate dc
        JOIN event ea ON ea.id = dc.event_id_a
        LEFT JOIN venue va ON va.id = ea.venue_id
        JOIN event eb ON eb.id = dc.event_id_b
        LEFT JOIN venue vb ON vb.id = eb.venue_id
        WHERE dc.resolution IS NULL
        """
    ).fetchall()

    adjudicated = 0
    for batch_start in range(0, len(pairs), DUPLICATE_BATCH_SIZE):
        batch = pairs[batch_start : batch_start + DUPLICATE_BATCH_SIZE]
        payload = [
            {
                "pair_id": pair_id,
                "event_a": {"title": title_a, "venue": venue_a, "date": date_a, "blurb": blurb_a},
                "event_b": {"title": title_b, "venue": venue_b, "date": date_b, "blurb": blurb_b},
            }
            for pair_id, title_a, venue_a, date_a, blurb_a, title_b, venue_b, date_b, blurb_b in batch
        ]
        results, error = _adjudicate_batch(client, payload)
        now = datetime.now(UTC).isoformat()
        if results is not None:
            for result in results:
                resolution = "same" if result.same_event else "different"
                conn.execute(
                    "UPDATE duplicate_candidate SET resolution = ?, resolved_at = ? WHERE id = ?",
                    (resolution, now, result.pair_id),
                )
                adjudicated += 1
        # On failure, leave these pairs with resolution IS NULL — they'll be
        # retried on the next `score` run rather than guessed at.

    return adjudicated


def _build_candidate_payload(conn: sqlite3.Connection, household: dict[str, Any], event_id: int) -> dict[str, Any]:
    row = _get_event_for_scoring(conn, event_id)
    drive_minutes = None
    if row["venue_latitude"] is not None and row["venue_longitude"] is not None:
        drive_minutes = estimate_drive_minutes(
            household["home_latitude"], household["home_longitude"], row["venue_latitude"], row["venue_longitude"]
        )
    return {
        "event_id": event_id,
        "title": row["title"],
        "venue": row["venue_name"],
        "date": row["event_date"],
        "category": row["category"],
        "price_min": row["price_min"],
        "price_max": row["price_max"],
        "currency": row["currency"],
        "drive_minutes": drive_minutes,
        "blurb": row["blurb"],
    }


def _get_event_for_scoring(conn: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT e.title, v.name, e.event_date, e.category, e.price_min, e.price_max, e.currency, e.blurb,
               v.latitude, v.longitude
        FROM event e LEFT JOIN venue v ON v.id = e.venue_id
        WHERE e.id = ?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    title, venue_name, event_date, category, price_min, price_max, currency, blurb, venue_latitude, venue_longitude = row
    return {
        "title": title,
        "venue_name": venue_name,
        "event_date": event_date,
        "category": category,
        "price_min": price_min,
        "price_max": price_max,
        "currency": currency,
        "blurb": blurb,
        "venue_latitude": venue_latitude,
        "venue_longitude": venue_longitude,
    }


def _score_batch(
    client: LLMClient, taste_profile: str, payload: list[dict[str, Any]]
) -> tuple[list[ScoreResult] | None, str | None]:
    user_content = f"Taste profile:\n\n{taste_profile}\n\nEvents to score:\n\n{json.dumps(payload)}"
    return _call_with_retry(client, SCORE_SYSTEM_PROMPT, user_content, ScoreResult)


def _adjudicate_batch(
    client: LLMClient, payload: list[dict[str, Any]]
) -> tuple[list[DuplicateAdjudication] | None, str | None]:
    user_content = json.dumps(payload)
    return _call_with_retry(client, DUPLICATE_SYSTEM_PROMPT, user_content, DuplicateAdjudication)


def _call_with_retry(client: LLMClient, system: str, user_content: str, model_cls: type) -> tuple[list | None, str | None]:
    raw_text = client.create_message(system, user_content)
    items, error = _parse_and_validate(raw_text, model_cls)
    if error is None:
        return items, None

    retry_content = (
        f"{user_content}\n\nYour previous response was invalid: {error}\n"
        "Return ONLY a valid JSON array matching the schema — no prose, no markdown fences."
    )
    raw_text = client.create_message(system, retry_content)
    return _parse_and_validate(raw_text, model_cls)


def _parse_and_validate(raw_text: str, model_cls: type) -> tuple[list | None, str | None]:
    try:
        data = json.loads(raw_text)
        if not isinstance(data, list):
            raise ValueError("expected a JSON array")
        return [model_cls.model_validate(item) for item in data], None
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return None, str(exc)
