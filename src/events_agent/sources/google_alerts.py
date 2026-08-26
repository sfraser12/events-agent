"""Google Alerts RSS adapter.

Covers a real gap flagged in taste-profile.md's Wellness section: spa/sauna/
hot-tub deals and new openings don't fit the ticketed-event API model (no
booking system to query), but a Google Alert on a search term ("Portavadie
spa offer") turns into a feed of matching articles/pages, which does fit
this project's "prefer a feed over a scraper" principle.

Format note, confirmed against a real feed (2026-08-27) rather than assumed:
despite "RSS feed" being the label in the Google Alerts UI, what Google
actually serves is Atom (`<feed>`/`<entry>`, `xmlns="http://www.w3.org/2005/
Atom"`, plus a `urn:atom-extension:indexing` namespace that marks it as the
legacy Google Reader-style Atom variant), not RSS 2.0 (`<rss>`/`<item>`). The
first version of this adapter assumed RSS 2.0 and would have silently
parsed zero items from every real feed -- caught by testing against the
user's actual feed URL, not by reasoning about it.

Setup isn't automatable — there's no public API to create a Google Alert.
For each search term you want covered: go to google.com/alerts, create the
alert, and under "Deliver to" choose "RSS feed" instead of email. Google
gives you a feed URL for that alert; put it in config.yaml (see
Config.google_alerts in config.py) alongside the venue that search term
covers and that venue's real coordinates -- see the note on venue_latitude/
venue_longitude below for why supplying them matters. Note a freshly
created alert's feed may show zero entries for a while -- it surfaces new
matches going forward, not necessarily a backfill.

Shape mismatch with every other adapter, and why: this yields RawEvents
with no event_date (undated -- these are article mentions of a deal or
opening, not a ticketed date the way a gig is) and no price (would need to
actually parse the linked article, out of scope). taste-profile.md already
tells the scoring LLM to treat this category generously given the thin
data. This is a deliberate, documented gap, not an oversight.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

import requests

from events_agent.models import RawEvent

REQUEST_TIMEOUT_SECONDS = 15

ATOM_NS = "http://www.w3.org/2005/Atom"


def _atom(tag: str) -> str:
    return f"{{{ATOM_NS}}}{tag}"


class GoogleAlertsAdapter:
    def __init__(
        self,
        feed_url: str,
        venue_name: str,
        # Google Alerts gives no venue coordinates at all -- but
        # db.upsert_venue() does an unconditional (non-COALESCE) overwrite
        # of latitude/longitude on an exact venue-name match, unlike its
        # COALESCE-protected `type` column. Passing None here for a venue
        # that already has real coordinates from Ticketmaster/Skiddle would
        # silently null them out and break that venue's radius constraint
        # for every source, not just this one. Always pass the venue's real
        # coordinates -- there are only ever a handful of these configured,
        # so looking them up once is cheap insurance against that trap.
        venue_latitude: float | None = None,
        venue_longitude: float | None = None,
        name: str | None = None,
        session: requests.Session | None = None,
    ):
        self.feed_url = feed_url
        self.venue_name = venue_name
        self.venue_latitude = venue_latitude
        self.venue_longitude = venue_longitude
        self.name = name or f"google_alerts_{_slug(venue_name)}"
        self.session = session or requests.Session()

    def fetch(self, since: datetime | None = None) -> Iterator[RawEvent]:
        response = self.session.get(self.feed_url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        yield from self._parse_feed(response.text)

    def _parse_feed(self, xml_text: str) -> Iterator[RawEvent]:
        root = ElementTree.fromstring(xml_text)
        for entry in root.findall(_atom("entry")):
            title = _text_content(entry.find(_atom("title")))
            if not title:
                continue

            raw_link = _first_link_href(entry)
            url = _unwrap_google_redirect(raw_link)
            # Atom allows either <content> (full body) or <summary>
            # (excerpt) -- Google Alerts is expected to use <content>, but
            # this feed is currently empty (fresh alert, no matches yet) so
            # that couldn't be confirmed against a real populated entry.
            # Falling back to <summary> costs nothing if <content> is
            # always what's actually there.
            body = entry.find(_atom("content"))
            if body is None:
                body = entry.find(_atom("summary"))
            description = _strip_html(_text_content(body))

            entry_id = (entry.findtext(_atom("id")) or "").strip()
            # Prefer the real id Google assigns (stable per article) --
            # fall back to hashing the resolved URL only when an entry is
            # missing one, so re-fetching the same feed doesn't create
            # duplicate rows.
            source_event_id = entry_id or hashlib.sha256(url.encode("utf-8")).hexdigest()

            yield RawEvent(
                source_name=self.name,
                source_event_id=source_event_id,
                title=title,
                category="other",
                venue_name=self.venue_name,
                venue_latitude=self.venue_latitude,
                venue_longitude=self.venue_longitude,
                url=url or None,
                blurb=description or None,
                raw={"title": title, "link": raw_link, "description": description},
            )


def _first_link_href(entry: ElementTree.Element) -> str:
    link = entry.find(_atom("link"))
    if link is None:
        return ""
    return (link.get("href") or "").strip()


def _unwrap_google_redirect(link: str) -> str:
    """Google Alerts wraps result links in a google.com/url?...&url=<real>
    redirect -- extract the real article URL. Returns the link unchanged if
    it isn't a Google redirect (e.g. if Google ever stops wrapping links)."""
    parsed = urlparse(link)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        real_url = parse_qs(parsed.query).get("url", [link])[0]
        return real_url
    return link


def _text_content(element: ElementTree.Element | None) -> str:
    """Google Alerts embeds unescaped <b> tags directly inside <title> (and
    possibly <content>/<summary>) to bold the matched search term --
    element.text alone only returns text before the first such tag.
    itertext() walks every text node regardless of nesting, so the bolded
    portion isn't silently dropped."""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _strip_html(text: str) -> str:
    """Covers the other real-world case: HTML-escaped tags (&lt;b&gt;) that
    decode to literal tag-shaped text rather than real XML nesting. A no-op
    on already-clean text either way."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
