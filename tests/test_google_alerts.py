"""GoogleAlertsAdapter tests. All HTTP calls are faked from a saved fixture — no network."""

from pathlib import Path

from events_agent.sources.google_alerts import GoogleAlertsAdapter, _strip_html, _unwrap_google_redirect

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def get(self, url, timeout):
        self.calls.append({"url": url, "timeout": timeout})
        return FakeResponse(self.text)


def make_adapter(session, **overrides):
    defaults = dict(
        feed_url="https://www.google.com/alerts/feeds/fake",
        venue_name="Portavadie",
        venue_latitude=55.8747,
        venue_longitude=-5.3011,
        session=session,
    )
    defaults.update(overrides)
    return GoogleAlertsAdapter(**defaults)


def test_fetch_yields_one_event_per_item_skipping_blank_titles():
    xml_text = (FIXTURES / "google_alerts_portavadie.xml").read_text()
    adapter = make_adapter(FakeSession(xml_text))

    events = list(adapter.fetch())

    assert len(events) == 2  # the fixture's third item has an empty title


def test_fetch_maps_real_fields():
    xml_text = (FIXTURES / "google_alerts_portavadie.xml").read_text()
    adapter = make_adapter(FakeSession(xml_text))

    event = list(adapter.fetch())[0]

    assert event.title == "Portavadie launches winter spa offer with 20% off midweek breaks"
    assert event.source_name == "google_alerts_portavadie"
    assert event.source_event_id == "tag:google.com,2026:alert/0000000000000000001"
    assert event.category == "other"
    assert event.venue_name == "Portavadie"
    assert event.venue_latitude == 55.8747
    assert event.venue_longitude == -5.3011
    assert event.url == "https://www.portavadie.com/news/winter-spa-offer"
    assert event.blurb == "Portavadie has launched a new winter spa offer, giving guests 20% off midweek day-spa passes through March."
    assert event.event_date is None  # undated by design — see module docstring


def test_fetch_falls_back_to_url_hash_when_guid_missing():
    xml_text = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
<title>No guid here</title>
<link>https://www.google.com/url?rct=j&amp;sa=t&amp;url=https://example.com/no-guid&amp;ct=ga</link>
<description>test</description>
</item>
</channel></rss>"""
    adapter = make_adapter(FakeSession(xml_text))

    event = list(adapter.fetch())[0]

    assert event.source_event_id  # non-empty
    assert event.source_event_id != ""


def test_custom_name_overrides_the_default_slug():
    xml_text = (FIXTURES / "google_alerts_portavadie.xml").read_text()
    adapter = make_adapter(FakeSession(xml_text), name="google_alerts_custom")

    event = list(adapter.fetch())[0]

    assert event.source_name == "google_alerts_custom"


def test_unwrap_google_redirect_extracts_real_url():
    wrapped = "https://www.google.com/url?rct=j&sa=t&url=https://www.portavadie.com/offer&ct=ga&cd=abc"
    assert _unwrap_google_redirect(wrapped) == "https://www.portavadie.com/offer"


def test_unwrap_google_redirect_leaves_non_redirect_links_unchanged():
    plain = "https://www.portavadie.com/offer"
    assert _unwrap_google_redirect(plain) == plain


def test_strip_html_removes_bold_tags():
    assert _strip_html("A <b>spa</b> offer") == "A spa offer"
