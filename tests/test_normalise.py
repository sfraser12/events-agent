from datetime import datetime

from events_agent.normalise import fingerprint, normalise_text


def test_strips_stopwords_and_punctuation():
    assert normalise_text("The Beatles Live") == "beatles"


def test_strips_documented_stopwords_from_ugly_titles():
    # "and"/"&" aren't in the CLAUDE.md stoplist, so these two don't converge yet —
    # that gap is explicitly flagged as Phase 2 work ("second source, two naming
    # conventions"). This locks in today's actual behaviour rather than the wished-for one.
    assert normalise_text("Bruce Springsteen & The E Street Band") == "bruce springsteen e street band"
    assert (
        normalise_text("Bruce Springsteen and the E Street Band - 2027 Tour")
        == "bruce springsteen and e street band 2027"
    )


def test_strips_phrase_stopwords():
    assert normalise_text("Some Band + Support") == "some band"
    assert normalise_text("An Evening With Some Band") == "some band"
    assert normalise_text("Some Band Plus Special Guests") == "some band"


def test_preserves_meaningful_words_that_look_like_stopwords():
    # "live" as a genre/venue signal elsewhere shouldn't nuke the whole title
    assert normalise_text("Trad Friday Sessions @ Blackfriars") == "trad friday sessions blackfriars"


def test_collapses_whitespace_and_case():
    assert normalise_text("  KING TUT'S   Wah Wah Hut  ") == "king tut s wah wah hut"


def test_fingerprint_stable_for_same_event():
    date = datetime(2026, 9, 4, 19, 30)
    fp_a = fingerprint("Old Skool Tribute Night", "ARTA", date)
    fp_b = fingerprint("Old Skool Tribute Night", "ARTA", date)
    assert fp_a == fp_b


def test_fingerprint_ignores_noise_tokens():
    date = datetime(2026, 9, 4)
    fp_a = fingerprint("The Old Skool Tribute Night Tour", "ARTA", date)
    fp_b = fingerprint("Old Skool Tribute Night", "ARTA", date)
    assert fp_a == fp_b


def test_fingerprint_differs_on_date():
    fp_a = fingerprint("Old Skool Tribute Night", "ARTA", datetime(2026, 9, 4))
    fp_b = fingerprint("Old Skool Tribute Night", "ARTA", datetime(2026, 9, 5))
    assert fp_a != fp_b


def test_fingerprint_differs_on_venue():
    date = datetime(2026, 9, 4)
    fp_a = fingerprint("Old Skool Tribute Night", "ARTA", date)
    fp_b = fingerprint("Old Skool Tribute Night", "Blackfriars", date)
    assert fp_a != fp_b


def test_fingerprint_handles_undated_event():
    fp = fingerprint("Some Announcement", "Some Venue", None)
    assert isinstance(fp, str) and len(fp) == 64
