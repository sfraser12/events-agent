import json
from datetime import UTC, datetime

import pytest

from events_agent.db import (
    get_connection,
    get_household_as_dict,
    init_db,
    upsert_household,
    upsert_raw_event,
    upsert_score,
)
from events_agent.models import RawEvent
from events_agent.scoring import (
    adjudicate_duplicates,
    run_scoring_for_household,
    select_scoring_candidates,
)


class FakeLLMClient:
    """Returns canned responses in order, one per call to create_message."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def create_message(self, system: str, user_content: str) -> str:
        self.calls.append((system, user_content))
        return self.responses.pop(0)


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def taste_profile_path(tmp_path):
    path = tmp_path / "taste-profile.md"
    path.write_text("Likes folk music. Dislikes arena shows.")
    return path


@pytest.fixture
def household(conn, taste_profile_path):
    upsert_household(
        conn,
        household_id=1,
        label="Test household",
        home_latitude=55.9410,
        home_longitude=-4.3170,
        radius_miles=25,
        near_days=7,
        month_days=31,
        max_drive_minutes=90,
        price_ceiling=200,
        blackout_dates=[],
        taste_profile_path=str(taste_profile_path),
        digest_threshold=60,
        alert_threshold=45,
        email_to="test@example.com",
    )
    conn.commit()
    return get_household_as_dict(conn, 1)


def make_raw_event(**overrides) -> RawEvent:
    defaults = dict(
        source_name="skiddle",
        source_event_id="1",
        title="Karine Polwart",
        category="music",
        venue_name="Oran Mor",
        venue_city="Glasgow",
        venue_latitude=55.8712,
        venue_longitude=-4.2893,
        event_date=datetime(2026, 9, 15, 19, 30, tzinfo=UTC),
        status="on_sale",
        price_min=25.0,
        price_max=25.0,
        raw={},
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


def test_select_scoring_candidates_returns_new_events(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())

    candidates = select_scoring_candidates(conn, household["id"])

    assert candidates == [event_id]


def test_select_scoring_candidates_excludes_past_and_cancelled(conn, household):
    upsert_raw_event(conn, make_raw_event(source_event_id="1", status="past"))
    upsert_raw_event(conn, make_raw_event(source_event_id="2", status="cancelled"))

    assert select_scoring_candidates(conn, household["id"]) == []


def test_select_scoring_candidates_excludes_already_scored_unchanged_events(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    upsert_score(
        conn, household["id"], event_id, score=80, audience="both",
        score_reason="great", urgency="none", scored_at=datetime.now(UTC).isoformat(),
    )
    conn.commit()

    assert select_scoring_candidates(conn, household["id"]) == []


def test_run_scoring_for_household_scores_via_the_llm_and_upserts(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    response = json.dumps(
        [{"event_id": event_id, "score": 92, "audience": "both", "reason": "A folk act we love.", "urgency": "none"}]
    )
    client = FakeLLMClient([response])

    stats = run_scoring_for_household(conn, client, household)
    conn.commit()

    assert stats == {"scored": 1, "excluded": 0, "failed": 0}
    row = conn.execute(
        "SELECT score, audience, score_reason, urgency FROM household_event_state WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row == (92, "both", "A folk act we love.", "none")


def test_run_scoring_handles_response_wrapped_in_markdown_fences(conn, household):
    # Regression: measured against the real API, the model wraps its JSON in
    # ```json ... ``` despite the system prompt saying not to — consistently,
    # not as a fluke, so this must be tolerated rather than relied on not to
    # happen.
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    fenced = (
        "```json\n"
        + json.dumps([{"event_id": event_id, "score": 77, "audience": "scott", "reason": "Good fit.", "urgency": "none"}])
        + "\n```"
    )
    client = FakeLLMClient([fenced])

    stats = run_scoring_for_household(conn, client, household)
    conn.commit()

    assert stats == {"scored": 1, "excluded": 0, "failed": 0}
    assert len(client.calls) == 1  # succeeded on the first call, no retry needed


def test_run_scoring_for_household_excludes_events_failing_constraints_without_calling_the_model(conn, household):
    # Real Barrowland coordinates but a narrow household radius — should be
    # filtered in Python before ever reaching the (fake) model.
    event_id, _ = upsert_raw_event(
        conn, make_raw_event(venue_latitude=55.8550553, venue_longitude=-4.2369184)
    )
    narrow_household = dict(household, radius_miles=1)
    client = FakeLLMClient([])  # would raise IndexError if called — proves no call happens

    stats = run_scoring_for_household(conn, client, narrow_household)
    conn.commit()

    assert stats == {"scored": 0, "excluded": 1, "failed": 0}
    score = conn.execute(
        "SELECT score FROM household_event_state WHERE event_id = ?", (event_id,)
    ).fetchone()[0]
    assert score is None


def test_run_scoring_retries_once_on_invalid_json_then_succeeds(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    valid = json.dumps(
        [{"event_id": event_id, "score": 70, "audience": "scott", "reason": "Good fit.", "urgency": "none"}]
    )
    client = FakeLLMClient(["not valid json at all", valid])

    stats = run_scoring_for_household(conn, client, household)
    conn.commit()

    assert stats == {"scored": 1, "excluded": 0, "failed": 0}
    assert len(client.calls) == 2  # confirms the retry actually happened


def test_run_scoring_falls_back_to_null_score_after_retry_also_fails(conn, household):
    # The Pydantic model must reject malformed output rather than pass it
    # through (per CLAUDE.md's testing notes) — here score is out of range.
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    invalid = json.dumps(
        [{"event_id": event_id, "score": 500, "audience": "scott", "reason": "x", "urgency": "none"}]
    )
    client = FakeLLMClient([invalid, invalid])

    stats = run_scoring_for_household(conn, client, household)
    conn.commit()

    assert stats == {"scored": 0, "excluded": 0, "failed": 1}
    row = conn.execute(
        "SELECT score, score_reason FROM household_event_state WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row[0] is None
    assert "manual review" in row[1]


def test_run_scoring_does_not_crash_on_a_non_json_response_after_retry(conn, household):
    upsert_raw_event(conn, make_raw_event())
    client = FakeLLMClient(["nope", "still nope"])

    stats = run_scoring_for_household(conn, client, household)
    conn.commit()

    assert stats["failed"] == 1


def test_adjudicate_duplicates_writes_back_resolution(conn, household):
    id_a, _ = upsert_raw_event(conn, make_raw_event(source_event_id="1", title="AEW: Dynamite & Collision"))
    id_b, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="2", title="Venue Premium - AEW: Dynamite & Collision")
    )
    pair_id = conn.execute(
        "SELECT id FROM duplicate_candidate WHERE event_id_a = ? AND event_id_b = ?",
        (min(id_a, id_b), max(id_a, id_b)),
    ).fetchone()[0]

    response = json.dumps([{"pair_id": pair_id, "same_event": True, "reason": "Same show, VIP tier."}])
    client = FakeLLMClient([response])

    adjudicated = adjudicate_duplicates(conn, client)
    conn.commit()

    assert adjudicated == 1
    resolution = conn.execute(
        "SELECT resolution FROM duplicate_candidate WHERE id = ?", (pair_id,)
    ).fetchone()[0]
    assert resolution == "same"


def test_adjudicate_duplicates_leaves_unresolved_on_failure(conn, household):
    id_a, _ = upsert_raw_event(conn, make_raw_event(source_event_id="1", title="AEW: Dynamite & Collision"))
    upsert_raw_event(
        conn, make_raw_event(source_event_id="2", title="Venue Premium - AEW: Dynamite & Collision")
    )
    client = FakeLLMClient(["garbage", "still garbage"])

    adjudicated = adjudicate_duplicates(conn, client)
    conn.commit()

    assert adjudicated == 0
    resolution = conn.execute(
        "SELECT resolution FROM duplicate_candidate LIMIT 1"
    ).fetchone()[0]
    assert resolution is None
