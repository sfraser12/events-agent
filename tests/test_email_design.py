from events_agent.delivery.email_design import cta_cell


def test_cta_cell_returns_empty_when_no_url():
    assert cta_cell(None) == ""


def test_cta_cell_includes_book_link():
    html_out = cta_cell("https://example.com/event", "Karine Polwart", "Oran Mor")
    assert 'href="https://example.com/event"' in html_out
    assert "Book" in html_out


def test_cta_cell_omits_fallback_link_when_no_title_given():
    # Regression: alert/digest/lookahead all pass a title now, but a bare
    # cta_cell(url) call (no title) should still degrade cleanly rather than
    # raising or emitting a broken "None tickets" search link.
    html_out = cta_cell("https://example.com/event")
    assert "link not working" not in html_out


def test_cta_cell_includes_fallback_search_link_when_title_given():
    # Regression 2026-08-29: a user-reported Ticketmaster link that led to a
    # page with no event on it. The direct booking link can go dead for
    # reasons outside our control (see mark_delisted_events in db.py for the
    # harvest-side half of this fix) — the email should always offer a way
    # out that doesn't depend on the specific source's link still working.
    html_out = cta_cell("https://example.com/event", "Karine Polwart", "Oran Mor")
    assert "link not working" in html_out
    assert "google.com/search?q=Karine+Polwart+Oran+Mor+tickets" in html_out


def test_cta_cell_fallback_search_link_works_without_venue():
    html_out = cta_cell("https://example.com/event", "Karine Polwart", None)
    assert "google.com/search?q=Karine+Polwart+tickets" in html_out
