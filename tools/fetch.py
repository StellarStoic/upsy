#!/usr/bin/env python3
"""upsy.si fetcher: read the sources, enrich what can be enriched, rank, and write data/news.json.

Standard library only, on purpose - the site is built by a GitHub Action every few minutes and a
toolchain with no dependencies is a toolchain that cannot rot.

Two things worth knowing before reading:

* Feeds are matched on local tag names, not full namespaced ones, so RSS 2.0, Atom and RSS 1.0/RDF all
  parse through the same code. Namespaced matching is what made a perfectly good arXiv feed look empty.
* Article pages are fetched only when they add something: an image the feed did not carry, or a
  description where the feed carried none. Their content is cached in data/state.json, so a run every
  ten minutes costs each article exactly one request, ever.

Fetching is direct by default and ignores any *_PROXY variables in the environment: a SOCKS proxy that
curl understands but urllib does not is a confusing failure ("Tunnel connection failed: 501 Tor is not
an HTTP Proxy") that has nothing to do with the source being down. Set UPSY_USE_PROXY=1 to honour them.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE_PATH = DATA / "state.json"
NEWS_PATH = DATA / "news.json"
META_PATH = DATA / "meta.json"

UA = "upsy.si/1.0 (+https://upsy.si/; a small AI news aggregator; contact one@upsy.si)"
TIMEOUT = 25
MAX_ITEMS = 400
MAX_ARTICLES_PER_RUN = 60
KEEP_DAYS = 21

# Ranking. Recency dominates, tier separates a lab announcing something from a blog writing about it.
TIER_WEIGHT = {1: 42.0, 2: 22.0, 3: 8.0}
# No single feed may own more than this much of the wire. The three-of-the-top-forty rule keeps the
# opening view mixed; without a wire-wide cap a feed that publishes a hundred items a day still fills
# everything below it, and a fifty-source catalogue becomes one blog with guests.
WIRE_PER_SOURCE = 8
HALF_LIFE_HOURS = 26.0
# How much of a feed's own body a per-source topical filter may read. Enough to see what the piece
# is about, short enough that a passing mention deep in a long essay does not qualify it.
FILTER_CHARS = 2000
# Matched on word boundaries, never as substrings: "agi" lives inside "again" and "magic", and one
# term matching by accident is enough to give half the internet a superintelligence score. A term
# ending in "*" is a stem, so "hallucinat*" still catches "hallucinating".
SI_GENERIC = {"ai", "a.i.", "artificial intelligence", "machine learning", "ml"}

PROMO_TERMS = (
    "register", "join us", "get your ticket", "tickets", "hours to", "save your seat", "early bird",
    "webinar", "sponsored", "advertorial", "podcast episode", "watch now", "listen now", "giveaway",
    "coupon", "discount", "buy now", "on sale", "newsletter", "subscribe to",
)

# An event page is a promotion with a date on it, and it can hide from the list above: a meetup whose
# feed summary is a bland sentence never says "register" in the two lines the card shows, so it keeps
# its freshness and ranks like news. These phrases are how a page asks a reader to turn up.
# Only the body's opening is read. A publisher's footer ("subscribe to our newsletter") sits in
# almost every article, and matching the whole body would sink most of the board for boilerplate.
PROMO_BODY_PHRASES = (
    "register now", "registration is", "registration for", "join us for", "join us at",
    "save your seat", "early bird", "reserve your spot", "rsvp", "seats are limited",
    "space is limited", "get your ticket", "buy tickets", "sign up for the event",
    "the event will be held", "attendees will", "meet us at", "doors open",
)
BODY_PROMO_CHARS = 700
# ...and the address, which a registration page cannot hide. Whole path segments only, so a post
# called "events-are-fun" is not caught by a substring.
PROMO_PATH_SEGMENTS = frozenset((
    "event", "events", "meetup", "meetups", "webinar", "webinars", "workshop", "workshops",
    "conference", "conferences", "summit", "summits", "tickets", "rsvp", "registration",
))


def _pattern(term: str) -> re.Pattern:
    stem = term.endswith("*")
    body = re.escape(term[:-1] if stem else term)
    return re.compile(rf"(?<![a-z0-9]){body}" + ("" if stem else r"(?![a-z0-9])"))


def matches(terms, text: str) -> list[str]:
    haystack = (text or "").lower()
    return [term for term in terms if _pattern(term).search(haystack)]


def passes_source_filter(source: dict, raw: dict) -> bool:
    """Whether one item belongs on the wire at all. A source with no `require` keeps everything."""
    return not (source.get("require") or []) or bool(source_topic_hits(source, raw))


def source_topic_hits(source: dict, raw: dict) -> list[str]:
    """Which of a source's own required terms one parsed item matches.

    A source may carry `require`: terms matched on word boundaries against the headline and the opening
    of what the feed itself gave us. An empty list means the item is off-topic; a source without the
    field keeps everything, so adding the field is the only change a narrow feed needs. The opening is
    bounded by FILTER_CHARS because a passing mention deep inside a long essay should not qualify it.
    """
    require = source.get("require") or []
    if not require:
        return []
    haystack = f"{raw.get('title') or ''} {raw.get('summary') or ''} {(raw.get('content') or '')[:FILTER_CHARS]}"
    return matches(require, haystack)


def promo_signals(item: dict, haystack: str) -> list[str]:
    """Why an item counts as a promotion, as a list of reasons.

    Three places, narrowest first: the words the card itself shows, the opening of the body the feed
    already gave us, and the address. A promotion's body asks the reader to turn up or sign up; an
    article's opening talks about its subject. Only the opening is read because a publisher's footer
    ("subscribe to our newsletter") sits in nearly every article, and matching a whole body would
    penalise most of the board for boilerplate. Returning the reasons is what makes a surprise
    readable from the data instead of guesswork.
    """
    hits = matches(PROMO_TERMS, haystack)
    content = (item.get("content") or "")[:BODY_PROMO_CHARS]
    if content:
        hits = hits + [term for term in matches(PROMO_BODY_PHRASES, content) if term not in hits]
    segment = promo_path_segment(item.get("url") or "")
    if segment and not hits:
        hits = [f"path:{segment}"]
    return hits


def promo_path_segment(url: str) -> str:
    """The path segment that makes a URL a registration page, or an empty string."""
    try:
        path = urllib.parse.urlsplit(url or "").path.lower()
    except ValueError:  # a malformed URL is not a promotion, it is just malformed
        return ""
    for segment in path.split("/"):
        if segment in PROMO_PATH_SEGMENTS:
            return segment
    return ""


SI_TERMS = (
    "superintelligence", "super intelligence", "agi", "artificial general intelligence",
    "frontier model", "frontier ai", "scaling law", "emergent", "alignment", "safety",
    "recursive self-improvement", "agentic", "autonomous agent", "reasoning model",
    "benchmark", "state of the art", "sota", "breakthrough", "training run", "compute cluster",
)
BREAKING_HOURS = 6.0
# What turns an item into an upsy moment: a machine doing something absurd, uncanny, or wild enough
# that people share it as "well, that happened". Rules pick the candidates; the editor writes them up.
UPSY_TERMS = (
    "hallucinat", "went rogue", "rogue", "accidentally", "by accident", "unexpectedly", "surprisingly",
    "prompt injection", "jailbreak", "sideways", "gone wrong", "embarrassing", "blunder", "mishap",
    "cheated", "cheating", "hacked the", "escaped", "refused to", "lying", "deceived", "blackmail",
    "sycophant", "glitch", "meltdown", "spiral", "chaos", "viral", "meme", "weird", "bizarre", "absurd",
    "spooky", "uncanny", "sentient", "conscious", "self-aware", "what could go wrong",
)
UPSY_SOURCES = {"singularity", "hn-frontier", "google-news-agi"}
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "at", "by", "with", "from", "is",
    "are", "was", "were", "be", "been", "it", "its", "this", "that", "as", "how", "why", "what",
    "new", "ai", "artificial", "intelligence", "model", "models", "says", "said", "will", "can",
    "could", "should", "has", "have", "not", "but", "more", "most", "than", "after", "over",
}


def direct_network() -> None:
    if os.environ.get("UPSY_USE_PROXY") == "1":
        return
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(key, None)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- fetching

def fetch(url: str, etag: str | None = None, modified: str | None = None) -> tuple[int, bytes, dict]:
    headers = {"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html;q=0.8, */*;q=0.5"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(4_000_000), dict(response.headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", dict(exc.headers or {})
        raise


def fetch_with_retries(url: str, attempts: int = 3, **kwargs) -> tuple[int, bytes, dict]:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetch(url, **kwargs)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(2 + 3 * attempt)
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - network errors are many and all mean "try again"
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2 + 3 * attempt)
                continue
            raise
    raise last if last else RuntimeError("unreachable")


# --------------------------------------------------------------------------- parsing

def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def descendants(node: ET.Element) -> list[ET.Element]:
    return list(node.iter())


def first_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in descendants(node):
        if local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def first_text_in_order(node: ET.Element, names: tuple[str, ...]) -> str:
    """The first named child's text, trying the names in the order given — not in document order.

    `first_text` walks the item and matches on names alone, so whichever of `<link>`, `<guid>` and
    `<id>` the publisher happened to write first wins. Apple's feed puts `<guid>` first, so the
    identifier — a slug, not a URL — was chosen as the link and every item from that source was
    dropped for having none. The order the caller writes is its order of trust; this keeps it.
    """
    for name in names:
        for child in descendants(node):
            if local(child.tag) == name and (child.text or "").strip():
                return child.text.strip()
    return ""


def attr_anywhere(node: ET.Element, names: tuple[str, ...], attribute: str) -> str:
    for child in descendants(node):
        if local(child.tag) in names:
            value = (child.get(attribute) or "").strip()
            if value:
                return value
    return ""


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style|figure|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_url(url: str) -> str:
    """Drop the tracking parameters that make the same article look like several."""
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url.strip())
    if not parts.scheme:
        return url.strip()
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "fbclid",
            "gclid", "mc_cid", "mc_eid", "ref", "source", "at_medium", "at_campaign", "guccounter"}
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=False) if k.lower() not in drop]
    cleaned = urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/") or "/",
                                       urllib.parse.urlencode(query), ""))
    return cleaned


def parse_date(node: ET.Element) -> datetime | None:
    for child in descendants(node):
        if local(child.tag) in ("pubdate", "published", "updated", "date", "created", "issued"):
            raw = (child.text or "").strip()
            if not raw:
                continue
            try:
                stamp = parsedate_to_datetime(raw)
            except Exception:
                try:
                    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
                except Exception:
                    continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(timezone.utc)
    return None


def _revive(entries: list[dict]) -> list[dict]:
    """The cached form of a parsed item, back to the shape the collector expects."""
    revived = [dict(entry) for entry in entries]
    for entry in revived:
        entry["published_dt"] = _parse(entry.get("published"))
    return revived


# Summaries that are not summaries. Reddit hands us "submitted by /u/name [link] [comments]" and
# Hacker News hands us "Article URL: ... Comments URL: ... Points: ..." - both were appearing verbatim
# on the page, including in the hero.
BOILERPLATE = (
    re.compile(r"submitted by\s+/u/\S+.*", re.I),
    re.compile(r"^\s*article url:\s*\S+.*$", re.I | re.M),
    re.compile(r"^\s*comments url:\s*\S+.*$", re.I | re.M),
    re.compile(r"^\s*points:\s*\d+.*$", re.I | re.M),
    re.compile(r"^\s*#\s*comments:\s*\d+.*$", re.I | re.M),
    re.compile(r"^\s*https?://\S+\s*$"),
    re.compile(r"^\s*\[link\]\s*\[comments\]\s*$", re.I),
    # Publisher boilerplate that a page's meta description often carries instead of the article's.
    re.compile(r"founded in \d{4}, we offer our audience.*", re.I),
    re.compile(r"^\s*(?:please )?subscribe.*(?:newsletter|to continue reading).*$", re.I | re.M),
    re.compile(r"^\s*(?:read more|continue reading)\s*[:\u2026.].*$", re.I | re.M),
)

# A subreddit argument is not a news event.
QUESTION_STARTS = (
    "serious question", "why do", "why does", "why is", "does anyone", "can someone", "eli5",
    "explain like", "discussion", "what if", "am i the only", "is it just me", "unpopular opinion",
    "how do you", "thoughts on", "question:",
)


# A page's meta description is usually a sentence about the publisher rather than about the story.
# Those are dropped whole: a card with no second line beats a card whose second line is an advert.
SELF_DESCRIPTION = (
    re.compile(r"\bis about technology and how it makes us feel\b", re.I),
    re.compile(r"\bwe offer our audience\b", re.I),
    re.compile(r"\bfounded in \d{4}\b", re.I),
    re.compile(r"\b(?:the )?latest (?:news|developments|updates) (?:in|on|from)\b", re.I),
    re.compile(r"\bleading (?:source|publication|provider|platform)\b", re.I),
    re.compile(r"\bour mission is\b", re.I),
    re.compile(r"\b(?:sign up|subscribe) (?:for|to|now)\b", re.I),
    re.compile(r"\bdelivered (?:to your inbox|straight to your)\b", re.I),
    re.compile(r"\ball things (?:ai|artificial intelligence)\b", re.I),
)


def clean_summary(summary: str) -> str:
    """Strip feed furniture, and return "" when what is left was never a summary at all."""
    text = summary or ""
    if any(pattern.search(text) for pattern in SELF_DESCRIPTION):
        return ""
    for pattern in BOILERPLATE:
        text = pattern.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -\u2013\u2014\t")
    return text if len(text.split()) >= 5 else ""


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except Exception:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def image_from_node(node: ET.Element) -> str:
    for child in descendants(node):
        tag = local(child.tag)
        if tag in ("content", "thumbnail") and child.get("url") and (
            (child.get("medium") or "").lower() == "image" or (child.get("type") or "").startswith("image")
            or tag == "thumbnail"
        ):
            return (child.get("url") or "").strip()
        if tag == "enclosure" and (child.get("type") or "").startswith("image") and child.get("url"):
            return (child.get("url") or "").strip()
    for child in descendants(node):
        if local(child.tag) in ("encoded", "content", "summary", "description", "group"):
            match = re.search(r"""(?is)<img[^>]+src=["']([^"']+)["']""", ET.tostring(child, encoding="unicode"))
            if match:
                return html.unescape(match.group(1)).strip()
    return ""


def resolve_link(node: ET.Element, link: str, base: str = "") -> str:
    """The URL we are willing to publish as a story link.

    Atom puts the real address in link/@href and frequently a "tag:host,2026:1" identifier in <id>,
    which is not a link and must never be published as one. Anything that is not http(s) is refused,
    and an item with no link at all is dropped by the collector rather than linked to nowhere.
    """
    if absolute(link, base) and is_article_url(absolute(link, base)):
        return absolute(link, base)
    href = absolute(attr_anywhere(node, ("link",), "href"), base)
    if href and is_article_url(href):
        return href
    return ""


def absolute(url: str, base: str = "") -> str:
    """A link as a whole URL. A feed may address its own posts relatively (`<link href="/blog/x">`,
    which Jekyll's Atom emits), and that is still a story as long as we know the feed's own address."""
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if base and url.startswith(("/", "./", "../")):
        return urllib.parse.urljoin(base, url)
    return ""


def atom_href(node: ET.Element, base: str = "") -> str:
    """Atom's real address: <link rel="alternate" href=...>, otherwise any <link href=...>."""
    fallback = ""
    for child in node:
        if local(child.tag) != "link":
            continue
        href = absolute((child.get("href") or "").strip(), base)
        if not href:
            continue
        if (child.get("rel") or "alternate").lower() == "alternate":
            return href
        fallback = fallback or href
    return fallback


def is_article_url(url: str) -> bool:
    """A link to a page, not to the front door of a site.

    The Verge's Atom entries carry <id>https://www.theverge.com/?p=1009406</id> - an http URL that
    lands on their homepage - while the article sits in <link rel="alternate" href=...>. A path is
    what makes a URL a story, so a bare host is refused and the next candidate is used instead.
    """
    parts = urllib.parse.urlparse(url)
    return bool(parts.netloc) and parts.path.strip("/") != ""


def reusable(items: list[dict]) -> list[dict]:
    """Filter links out of the kept-items cache on the way back in.

    A source that answers 304, rate-limits us or fails keeps the items it gave us last time, which is
    how the stream avoids emptying out. But those items were accepted by whatever rules existed when
    they were parsed, so they are re-checked here: The Verge's old "?p=NNNN" ids, which resolve to
    their homepage, must not survive a fix to the link rules.
    """
    return [item for item in items if is_article_url(item.get("link") or "")]


def parse_feed(body: bytes, base: str = "") -> list[dict]:
    text = body.decode("utf-8", "replace").lstrip("\ufeff \r\n\t")
    root = ET.fromstring(text)
    nodes: list[ET.Element] = []
    for candidate in root.iter():
        if local(candidate.tag) in ("item", "entry"):
            nodes.append(candidate)
    items: list[dict] = []
    for node in nodes:
        title = strip_html(first_text(node, ("title",)))
        # Ordered by trust: Atom's href, then the RSS text link, then guid/id - and the first one
        # that is both http and an actual page wins. An id that only points at the site root (The
        # Verge publishes ?p=NNNN ids) is skipped instead of being published as the story's address.
        candidates = (atom_href(node, base),
                      first_text_in_order(node, ("link", "origlink", "guid", "id")),
                      attr_anywhere(node, ("link",), "href"))
        link = ""
        for candidate in candidates:
            candidate = absolute(candidate, base)
            if not candidate:
                continue
            cleaned = clean_url(candidate)
            if is_article_url(cleaned):
                link = cleaned
                break
        if not title or not link:
            continue
        raw_summary = first_text(node, ("encoded", "summary", "description", "content"))
        full_text = first_text(node, ("encoded",))
        items.append({
            "title": title,
            "link": resolve_link(node, link, base),
            "published": parse_date(node),
            "author": strip_html(first_text(node, ("author", "creator", "name"))),
            "summary": strip_html(raw_summary)[:1200],
            "content": strip_html(full_text) if full_text else "",
            "has_full_text": bool(full_text),
            "image": image_from_node(node),
        })
    return items


ARTICLE_CACHE: dict[str, dict] = {}


def enrich_article(url: str, cache: dict[str, dict], budget: list[int]) -> dict:
    """An article page asked for one thing only: what the feed did not already give us."""
    if url in cache:
        return cache[url]
    if budget[0] <= 0:
        return {}
    if url in ARTICLE_CACHE:
        return ARTICLE_CACHE[url]
    found: dict = {}
    try:
        budget[0] -= 1
        status, body, _ = fetch_with_retries(url, attempts=2)
        if status < 400:
            page = body.decode("utf-8", "replace")
            head = page[:400_000]
            match = re.search(r"""(?is)<meta[^>]+(?:property|name)=["']og:image(?::url)?["'][^>]+content=["']([^"']+)["']""", head)
            if not match:
                match = re.search(r"""(?is)<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["']og:image(?::url)?["']""", head)
            if match:
                found["image"] = html.unescape(match.group(1) or "").strip()
            if not found.get("image"):
                match = re.search(r"""(?is)<meta[^>]+(?:property|name)=["']twitter:image["'][^>]+content=["']([^"']+)["']""", head)
                if match:
                    found["image"] = html.unescape(match.group(1) or "").strip()
            match = re.search(r"""(?is)<meta[^>]+(?:property|name)=["'](?:og:description|description)["'][^>]+content=["']([^"']{40,})["']""", head)
            if match:
                found["description"] = html.unescape(match.group(1)).strip()[:1200]
            match = re.search(r"""(?is)<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']""", head)
            if match:
                found["canonical"] = clean_url(html.unescape(match.group(1)))
            img_alt = re.search(r"""(?is)<meta[^>]+property=["']og:image:alt["'][^>]+content=["']([^"']+)["']""", head)
            if img_alt:
                found["image_alt"] = html.unescape(img_alt.group(1)).strip()
    except Exception as exc:  # noqa: BLE001 - enrichment is best effort, never fatal
        found["error"] = f"{type(exc).__name__}"
    ARTICLE_CACHE[url] = found
    if found:
        cache[url] = found
    return found


# --------------------------------------------------------------------------- ranking

def _echoes(summary: str, title: str) -> bool:
    """True when the summary is the headline again (aggregator feeds do this constantly)."""
    a, b = tokens(summary), tokens(title)
    if not a or not b:
        return True
    return len(a & b) / max(1, len(b)) >= 0.7


ARCHIVE_PREAMBLE_KEYS = ("author", "title", "subtitle", "date", "notes", "source", "translator",
                         "transcribed", "published", "written", "edited")


def strip_archive_preamble(text: str) -> str:
    """Drop the 'Author : … Title : … Date : … Source : …' run that archive feeds put before the prose.

    The Anarchist Library prefixes every item with it, and clean_summary() then throws the whole
    description away as publisher furniture - the item arrived with a headline and no second line at
    all. Only a leading run of these keys is touched, so an ordinary article is unchanged.
    """
    value = (text or "").strip()
    keys = "|".join(ARCHIVE_PREAMBLE_KEYS)
    lead = re.compile(rf"^(?:{keys})\s*:\s*", re.I)
    following = re.compile(rf"\s(?:{keys})\s*:\s*", re.I)
    for _ in range(12):
        opening = lead.match(value)
        if not opening:
            break
        rest = value[opening.end():]
        nxt = following.search(rest)
        value = (rest[nxt.start():] if nxt else rest).strip()
        if not nxt:
            break
    # "Retrieved on 2026-09-28 from example.org/wiki/x" is where the text came from, not what it says.
    value = re.sub(r"^Retrieved\s+(?:on|from)\b.*?\s(?=[A-Z][a-z])", "", value, flags=re.S).strip()
    return value


def summary_text(summary: str, title: str) -> str:
    """An item's second line: catalogue preamble off, publisher furniture dropped, never an echo of the
    headline. The echo check runs on the stripped text, because a library's preamble repeats the title
    inside its own metadata ("Title : …") and used to blank every summary the source had."""
    cleaned = strip_archive_preamble(summary)
    if _echoes(cleaned, title):
        return ""
    return clean_summary(cleaned)


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def select_items(listed: list[dict], sources: list[dict], limit: int) -> list[dict]:
    """The wire: the best `limit` items, with room reserved for the curated narrow sources.

    A source marked `keep` publishes rarely and is narrowed by its own `require` filter, so its pieces
    are already curated - but they are not news, and a three-week-old essay scores about 8 against a
    fresh press story's 100+. Without a reservation such a source is listed on the page and never
    actually posts anything, which is exactly what happened to the first AI text the library sent.
    """
    keep_ids = {source["id"] for source in sources if source.get("keep")}
    keepers = [item for item in listed if item["source"] in keep_ids]
    others = [item for item in listed if item["source"] not in keep_ids]
    chosen = keepers + others[:max(0, limit - len(keepers))]
    return sorted(chosen, key=lambda i: (-(i["score"] or 0), i.get("published") or ""))


def cap_per_source(items: list[dict], limit: int) -> list[dict]:
    """Keep at most `limit` items from any one source, in the order they arrived (which is score order).

    Deferring the overflow of the opening view is not enough: the deferred items come back in at their
    score, so a feed with a hundred fresh entries takes a hundred of the four hundred slots. A wire is a
    mixture, and the mixture is the point.
    """
    kept: list[dict] = []
    per: dict[str, int] = {}
    for item in items:
        count = per.get(item["source"], 0)
        if count >= limit:
            continue
        per[item["source"]] = count + 1
        kept.append(item)
    return kept


def freshness(published: datetime | None, now: datetime) -> float:
    if not published:
        return 0.0
    hours = max(0.0, (now - published).total_seconds() / 3600.0)
    return 100.0 * (0.5 ** (hours / HALF_LIFE_HOURS))


def load_json(path: pathlib.Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def run(args: argparse.Namespace) -> int:
    direct_network()
    DATA.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    defaults = config.get("defaults", {})
    state = load_json(STATE_PATH, {})
    seen: dict = state.get("seen", {})
    feed_cache: dict = state.get("feed_cache", {})
    item_cache: dict = state.get("item_cache", {})
    article_cache: dict = state.get("article_cache", {})
    previous = load_json(NEWS_PATH, {"items": [], "generated": None})
    previous_ids = {item["id"] for item in previous.get("items", [])}

    now = datetime.now(timezone.utc)
    sources = [s for s in config["sources"] if s.get("enabled", defaults.get("enabled", True))]
    if args.only:
        wanted = set(args.only.split(","))
        sources = [s for s in sources if s["id"] in wanted]

    log(f"upsy fetch: {len(sources)} source(s), {now.isoformat(timespec='seconds')}")

    def read_source(source: dict) -> tuple[dict, list[dict], str]:
        cached = feed_cache.get(source["url"], {})
        try:
            status, body, headers = fetch_with_retries(source["url"],
                                                       etag=None if args.ignore_cache else cached.get("etag"),
                                                       modified=None if args.ignore_cache else cached.get("modified"))
        except Exception as exc:  # noqa: BLE001
            # A source that fails now still has the items it gave us last time, and losing them on a
            # transient 429 or a Cloudflare hiccup is how a stream quietly empties out.
            kept = reusable(_revive(item_cache.get(source["url"], [])))
            if kept:
                return (source, kept, f"{type(exc).__name__}: {exc}"[:120])
            return (source, [], f"{type(exc).__name__}: {exc}"[:160])
        if status == 304:
            # A feed that has not changed still has items, and dropping them is how a site loses
            # half its content on the second run of the day. Reuse what this source last gave us.
            revived = reusable(_revive(item_cache.get(source["url"], [])))
            return (source, revived, f"not modified ({len(revived)} kept)")
        try:
            items = parse_feed(body, source["url"])
        except ET.ParseError as exc:
            kept = reusable(_revive(item_cache.get(source["url"], [])))
            if kept:
                return (source, kept, f"not feed XML, kept {len(kept)}")
            return (source, [], f"not feed XML ({exc})")
        feed_cache[source["url"]] = {
            "etag": headers.get("ETag") or headers.get("etag"),
            "modified": headers.get("Last-Modified") or headers.get("last-modified"),
            "fetched": now.isoformat(timespec="seconds"),
        }
        for entry in items:
            entry["published_dt"] = entry.get("published")
        # Stored as ISO strings: this dict goes straight into state.json, and a datetime in there is
        # a TypeError on the one write that matters. _parse() turns it back on the revived path.
        item_cache[source["url"]] = [
            {
                **{k: v for k, v in entry.items() if k != "published_dt"},
                "published": entry["published"].isoformat(timespec="seconds")
                if isinstance(entry.get("published"), datetime) else entry.get("published"),
            }
            for entry in items
        ][:120]
        return (source, items, f"ok {status}")

    results: list[tuple[dict, list[dict], str]] = []
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(read_source, sources):
            results.append(result)

    collected: list[dict] = []
    health: list[dict] = []
    for source, items, status in results:
        fresh = 0
        filtered = 0
        newest = None
        # A per-source topical filter ("require" in sources.json): The Anarchist Library publishes
        # every text it archives, so the whole feed is only useful once it is narrowed to the pieces
        # that touch AI. Terms match on word boundaries (see matches()), so "ai" cannot score "said".
        require = source.get("require") or []
        for raw in items:
            if not raw["link"] or not raw["title"]:
                continue
            ai_terms = source_topic_hits(source, raw)
            if require and not ai_terms:
                filtered += 1
                continue
            identifier = hashlib.sha1(clean_url(raw["link"]).encode("utf-8")).hexdigest()[:16]
            published = raw.get("published_dt") or raw.get("published")
            if published and (now - published).days > KEEP_DAYS:
                continue
            if identifier not in seen:
                seen[identifier] = now.isoformat(timespec="seconds")
                fresh += 1
            if published and (newest is None or published > newest):
                newest = published
            collected.append({
                "id": identifier,
                "title": raw["title"][:400],
                "url": clean_url(raw["link"]),
                "source": source["id"],
                "sourceUrl": source["url"],
                "sourceSite": source.get("site", ""),
                "sourceName": source["name"],
                "sourceCategory": source.get("category", "news"),
                "tier": source.get("tier", defaults.get("tier", 2)),
                "published": published.isoformat(timespec="seconds") if published else None,
                "author": raw["author"][:120],
                "summary": summary_text(raw["summary"], raw["title"]),
                "content": raw["content"][:8000],
                "contentFrom": "feed" if raw["has_full_text"] else "",
                "image": raw["image"],
                "aiTerms": ai_terms[:6],
                "fetchArticle": source.get("fetchArticle", defaults.get("fetchArticle", True)),
            })
        health.append({
            "id": source["id"], "name": source["name"], "status": status, "items": len(items),
            "new": fresh, "filtered": filtered,
            "newest": newest.isoformat(timespec="seconds") if newest else None,
        })

    # One item per URL, and one per near-identical title, keeping the highest tier.
    deduped: dict[str, dict] = {}
    titles: list[tuple[set[str], dict]] = []
    for item in sorted(collected, key=lambda i: (i["tier"], i["published"] or "")):
        if item["id"] in deduped:
            continue
        item_tokens = tokens(item["title"])
        duplicate = False
        for other_tokens, other in titles:
            if not item_tokens or not other_tokens:
                continue
            overlap = len(item_tokens & other_tokens) / max(1, min(len(item_tokens), len(other_tokens)))
            if overlap >= 0.75:
                other.setdefault("alsoCoveredBy", []).append({"source": item["sourceName"], "url": item["url"]})
                duplicate = True
                break
        if duplicate:
            continue
        deduped[item["id"]] = item
        titles.append((item_tokens, item))

    items = list(deduped.values())

    # Enrich with the article page only where it changes the card.
    budget = [0 if args.no_articles else MAX_ARTICLES_PER_RUN]
    if budget[0]:
        wanted = [i for i in items if i["fetchArticle"] and (not i["image"] or len(i["summary"]) < 220)]
        wanted.sort(key=lambda i: (i["published"] or ""), reverse=True)
        for item in wanted:
            extra = enrich_article(item["url"], article_cache, budget)
            if extra.get("image") and not item["image"]:
                item["image"] = extra["image"]
                item["imageFrom"] = "article"
                if extra.get("image_alt"):
                    item["imageAlt"] = extra["image_alt"]
            if extra.get("description") and len(item["summary"]) < 220:
                item["summary"] = clean_summary(extra["description"])
            if extra.get("canonical") and extra["canonical"] != item["url"]:
                item["url"] = extra["canonical"]
                item["id"] = hashlib.sha1(item["url"].encode("utf-8")).hexdigest()[:16]
        log(f"enriched {len(wanted)} article(s), {MAX_ARTICLES_PER_RUN - budget[0]} request(s) spent")

    # Whatever the article cache already knows still counts, even on a run that fetches nothing new:
    # otherwise --no-articles quietly strips the images and summaries earlier runs paid for.
    for item in items:
        cached_extra = article_cache.get(item["url"])
        if not cached_extra:
            continue
        if cached_extra.get("image") and not item["image"]:
            item["image"] = cached_extra["image"]
            item["imageFrom"] = "article"
            if cached_extra.get("image_alt"):
                item["imageAlt"] = cached_extra["image_alt"]
        if cached_extra.get("description") and len(item["summary"]) < 220:
            # Through the same filter as a feed summary: a page's meta description is often the
            # publisher describing itself, which is how "Founded in 2011, we offer our audience..."
            # ended up as the second line of the top story.
            item["summary"] = clean_summary(cached_extra["description"])

    # Score.
    for item in items:
        published = None
        if item["published"]:
            try:
                published = datetime.fromisoformat(item["published"])
            except Exception:
                published = None
        haystack = f"{item['title']} {item['summary']}".lower()
        score = freshness(published, now) + TIER_WEIGHT.get(item["tier"], 8.0)
        hits = len(matches([term for term in SI_TERMS if term not in SI_GENERIC], haystack))
        # Cap the keyword boost and halve it for aggregators: the Google News feed wears
        # "superintelligence" in every headline, which is how it took over the front page.
        boost = min(hits, 3) * 5.0
        score += boost * (0.5 if item["sourceCategory"] == "aggregator" else 1.0)
        if item["image"]:
            score += 6.0
        if item["sourceCategory"] == "aggregator":
            score -= 12.0
        # "48 hours to Disrupt" is a promotion wearing an AI headline, and it ranked top of the page.
        question = any(item["title"].strip().lower().startswith(start) for start in QUESTION_STARTS)
        if question:
            score -= 18.0
        promo_hits = promo_signals(item, haystack)
        if promo_hits:
            score -= 30.0
        if item.get("alsoCoveredBy"):
            score += min(len(item["alsoCoveredBy"]), 4) * 7.0
        item["score"] = round(score, 2)
        item["siTerms"] = sorted(matches(SI_TERMS, haystack))[:6]
        upsy_hits = matches(UPSY_TERMS, haystack)
        # One term plus a community source, or two terms anywhere: an upsy moment has to be a story
        # in its own right, not simply the word "unexpectedly" in a press release.
        item["upsyScore"] = len(upsy_hits) + (1 if item["source"] in UPSY_SOURCES else 0)
        item["upsyTerms"] = upsy_hits[:6]
        item["promo"] = bool(promo_hits)
        # Why a story was called a promotion, so the next surprise is readable from the data rather
        # than guessed at: the term, or the path segment that condemned it.
        if promo_hits:
            item["promoTerms"] = promo_hits[:6]
        item["question"] = question
        item["breaking"] = bool(
            published
            and (now - published) <= timedelta(hours=BREAKING_HOURS)
            and score >= 92
            and not promo_hits
            and not question
            and item["sourceCategory"] != "aggregator"
        )
        item["upsy"] = item["upsyScore"] >= 2
        item.pop("fetchArticle", None)

    items.sort(key=lambda i: (-(i["score"] or 0), i.get("published") or ""))
    # A ticker with twenty-two entries is not a ticker. Highest score wins, one per source.
    breaking_kept, breaking_sources = 0, set()
    for item in items:
        if not item["breaking"]:
            continue
        if breaking_kept >= 6 or item["source"] in breaking_sources:
            item["breaking"] = False
            continue
        breaking_kept += 1
        breaking_sources.add(item["source"])
    # Editorial rule: the first page has to be a mix. Without this a single prolific feed fills every
    # slot it can rank into, which is exactly what the aggregator did before it was demoted.
    per_source: dict[str, int] = {}
    ordered: list[dict] = []
    deferred: list[dict] = []
    for item in items:
        seen_count = per_source.get(item["source"], 0)
        if len(ordered) < 40 and seen_count >= 3:
            deferred.append(item)
            continue
        per_source[item["source"]] = seen_count + 1
        ordered.append(item)
    items = select_items(cap_per_source(ordered + deferred, WIRE_PER_SOURCE), sources, MAX_ITEMS)

    counts = {
        "items": len(items),
        "new": len([i for i in items if i["id"] not in previous_ids]),
        "breaking": len([i for i in items if i["breaking"]]),
        "promo": len([i for i in items if i.get("promo")]),
        "upsy": len([i for i in items if i["upsy"]]),
        "withImage": len([i for i in items if i["image"]]),
        "withFullText": len([i for i in items if i["contentFrom"]]),
        "sources": len(sources),
    }

    # Keep state bounded.
    table = []
    for item in items:
        table.append({
            "id": item["id"], "title": item["title"], "url": item["url"], "source": item["source"],
            "sourceName": item["sourceName"], "published": item["published"], "summary": item["summary"],
            "image": item["image"], "tier": item["tier"], "score": item["score"],
            "breaking": item["breaking"], "upsy": item["upsy"], "upsyTerms": item["upsyTerms"],
            "siTerms": item["siTerms"], "author": item["author"], "contentFrom": item["contentFrom"],
            "content": item["content"], "alsoCoveredBy": item.get("alsoCoveredBy", []),
            "imageFrom": item.get("imageFrom", "feed" if item["image"] else ""),
            "imageAlt": item.get("imageAlt", ""),
            # Fields the site builder, the editor and the sources page read. They live on the working
            # item, and forgetting to publish one is exactly how the whole build died on sourceUrl.
            "sourceUrl": item["sourceUrl"], "sourceSite": item.get("sourceSite", ""),
            "sourceCategory": item["sourceCategory"],
            "aiTerms": item.get("aiTerms", []),
            "promo": item.get("promo", False), "question": item.get("question", False),
            "upsyWhy": item.get("upsyWhy", ""),
        })
    DATA.joinpath("news.json").write_text(json.dumps({
        "generated": now.isoformat(timespec="seconds"),
        "counts": counts,
        "items": table,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    DATA.joinpath("meta.json").write_text(json.dumps({
        "generated": now.isoformat(timespec="seconds"),
        "counts": counts,
        "health": sorted(health, key=lambda h: h["id"]),
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    cutoff = (now - timedelta(days=KEEP_DAYS + 7)).isoformat(timespec="seconds")
    state = {
        "seen": {k: v for k, v in seen.items() if v >= cutoff},
        "feed_cache": feed_cache,
        "item_cache": item_cache,
        "article_cache": {k: v for k, v in list(article_cache.items())[-3000:]},
        "updated": now.isoformat(timespec="seconds"),
    }
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    log("")
    log(f"{'source':<22} {'status':<18} {'items':>5} {'new':>4}  newest")
    for row in sorted(health, key=lambda h: (not h["status"].startswith("ok") and h["status"] != "not modified", h["id"])):
        log(f"{row['id']:<22} {row['status']:<18} {row['items']:>5} {row['new']:>4}  {row['newest'] or '-'}")
    log("")
    log(f"wrote {len(table)} item(s): {counts}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch the upsy.si sources into data/news.json")
    parser.add_argument("--only", help="comma-separated source ids to fetch")
    parser.add_argument("--no-articles", action="store_true", help="skip article enrichment entirely")
    parser.add_argument("--ignore-cache", action="store_true", help="do not send If-None-Match / If-Modified-Since")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
