import json
from datetime import UTC, datetime
from typing import Any

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
    AnthropicLLMClient,
    _score_batch,
    adjudicate_duplicates,
    run_scoring_for_household,
    select_scoring_candidates,
)


class FakeLLMClient:
    """Returns canned responses in order, one per call to create_message."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str | list[dict[str, Any]]]] = []

    def create_message(self, system: str, user_content: str | list[dict[str, Any]], context: str = "") -> str:
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


def test_select_scoring_candidates_excludes_events_re_harvested_unchanged_after_scoring(conn, household):
    # Regression: harvest re-touches nearly every known event on every run
    # (both sources just re-return the same listings). If upsert_raw_event
    # advanced last_seen unconditionally, this exact sequence — score once,
    # then harvest again with nothing actually different — would make
    # last_seen > scored_at true for basically every event and trigger a
    # near-full rescore every single day. Confirmed in production: a second
    # real `run` rescored 2,739 events for what should've been a handful of
    # genuine changes.
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    upsert_score(
        conn, household["id"], event_id, score=80, audience="both",
        score_reason="great", urgency="none", scored_at=datetime.now(UTC).isoformat(),
    )
    conn.commit()

    upsert_raw_event(conn, make_raw_event())  # identical re-harvest, nothing differs
    conn.commit()

    assert select_scoring_candidates(conn, household["id"]) == []


def test_select_scoring_candidates_includes_events_re_harvested_with_a_real_change(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event(status="on_sale"))
    upsert_score(
        conn, household["id"], event_id, score=80, audience="both",
        score_reason="great", urgency="none", scored_at=datetime.now(UTC).isoformat(),
    )
    conn.commit()

    upsert_raw_event(conn, make_raw_event(status="low_availability"))  # genuine change
    conn.commit()

    assert select_scoring_candidates(conn, household["id"]) == [event_id]


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


def test_score_batch_marks_the_taste_profile_block_cacheable(conn, household):
    # The taste profile is stable across every batch in a run and across
    # weeks until edited — it must be its own block with a cache breakpoint,
    # and the (genuinely volatile, different every batch) events list must
    # come after that breakpoint, uncached.
    response = json.dumps(
        [{"event_id": 1, "score": 50, "audience": "both", "reason": "x", "urgency": "none"}]
    )
    client = FakeLLMClient([response])
    payload = [{"event_id": 1, "title": "Test Event"}]

    _score_batch(client, "Likes folk music.", payload)

    assert len(client.calls) == 1
    _, user_content = client.calls[0]
    assert isinstance(user_content, list)
    assert len(user_content) == 2
    taste_block, events_block = user_content
    assert "Likes folk music." in taste_block["text"]
    assert taste_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "Test Event" in events_block["text"]
    assert "cache_control" not in events_block


def test_score_batch_retry_preserves_the_cached_block(conn, household):
    # A retry must append to the cached content, not rebuild it — rebuilding
    # would change the prefix bytes and silently defeat the cache hit.
    valid = json.dumps([{"event_id": 1, "score": 50, "audience": "both", "reason": "x", "urgency": "none"}])
    client = FakeLLMClient(["not valid json", valid])
    payload = [{"event_id": 1, "title": "Test Event"}]

    _score_batch(client, "Likes folk music.", payload)

    assert len(client.calls) == 2
    _, retry_content = client.calls[1]
    assert isinstance(retry_content, list)
    assert retry_content[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "Likes folk music." in retry_content[0]["text"]
    assert any("previous response was invalid" in block["text"] for block in retry_content)


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


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text, usage):
        self.content = [_FakeTextBlock(text)]
        self.usage = usage


class _FakeAnthropicMessages:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeAnthropicSDKClient:
    def __init__(self, response):
        self.messages = _FakeAnthropicMessages(response)


def test_anthropic_client_calls_on_usage_with_token_counts_and_context():
    response = _FakeAnthropicResponse("[]", _FakeUsage(100, 20, 5, 3))
    client = AnthropicLLMClient(api_key="fake-key")
    client._client = _FakeAnthropicSDKClient(response)

    calls = []
    client.on_usage = lambda *args: calls.append(args)

    client.create_message("system prompt", "user content", context="Milngavie")

    assert len(calls) == 1
    context, model, input_tokens, output_tokens, cache_creation, cache_read = calls[0]
    assert context == "Milngavie"
    assert model == client.model
    assert (input_tokens, output_tokens, cache_creation, cache_read) == (100, 20, 5, 3)


def test_anthropic_client_without_on_usage_does_not_error():
    response = _FakeAnthropicResponse("[]", _FakeUsage(100, 20, 0, 0))
    client = AnthropicLLMClient(api_key="fake-key")
    client._client = _FakeAnthropicSDKClient(response)

    result = client.create_message("system prompt", "user content")

    assert result == "[]"
