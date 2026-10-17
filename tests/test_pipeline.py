#!/usr/bin/env python3
"""Tests for the upsy.si pipeline.

No network anywhere: the feed parsers, the ranking vocabulary, the source list and the whole site
build are exercised against fixtures, so the suite runs in CI and on a laptop with the wifi off.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_site  # noqa: E402
import fetch as fetcher  # noqa: E402
import moments as editor  # noqa: E402

RSS_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:media="http://search.yahoo.com/mrss/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Example AI</title>
    <link>https://example.com/</link>
    <item>
      <title>A frontier model did something odd</title>
      <link>https://example.com/odd?utm_source=rss</link>
      <guid>https://example.com/odd</guid>
      <pubDate>Sat, 11 Oct 2026 09:12:00 +0000</pubDate>
      <dc:creator>Ada L</dc:creator>
      <description>Short dek about the odd thing.</description>
      <media:content url="https://example.com/odd.jpg" type="image/jpeg"/>
      <content:encoded><![CDATA[<p>Full text of the article about the odd thing, long enough to matter.</p>]]></content:encoded>
    </item>
  </channel>
</rss>
"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <updated>2026-10-11T08:00:00Z</updated>
  <entry>
    <title>An agent team wasted tokens</title>
    <link rel="alternate" href="https://atom.example.com/post"/>
    <id>tag:atom.example.com,2026:1</id>
    <updated>2026-10-11T07:30:00Z</updated>
    <summary>They spent a fortune for nothing measurable.</summary>
  </entry>
</feed>
"""

RDF_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://rdf.example.com/feed">
    <title>Example RDF</title>
    <link>https://rdf.example.com/</link>
    <items><rdf:Seq><rdf:li resource="https://rdf.example.com/paper"/></rdf:Seq></items>
  </channel>
  <item rdf:about="https://rdf.example.com/paper">
    <title>A paper that parses</title>
    <link>https://rdf.example.com/paper</link>
    <dc:date>2026-10-10T12:00:00Z</dc:date>
    <description>Abstract of the paper.</description>
  </item>
</rdf:RDF>
"""


class FeedParsing(unittest.TestCase):
    def test_rss_with_image_and_full_text(self):
        items = fetcher.parse_feed(RSS_FIXTURE.encode())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["title"], "A frontier model did something odd")
        self.assertEqual(item["image"], "https://example.com/odd.jpg")
        self.assertIn("Full text of the article", item["content"])
        self.assertEqual(item["author"], "Ada L")
        self.assertIsNotNone(item["published"])

    def test_atom(self):
        items = fetcher.parse_feed(ATOM_FIXTURE.encode())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://atom.example.com/post")
        self.assertEqual(items[0]["summary"], "They spent a fortune for nothing measurable.")

    def test_rdf_does_not_parse_empty(self):
        # arXiv-style feeds are RDF, and an RSS-only parser silently returns nothing for them.
        items = fetcher.parse_feed(RDF_FIXTURE.encode())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "A paper that parses")

    def test_tracking_parameters_are_stripped_from_the_link(self):
        items = fetcher.parse_feed(RSS_FIXTURE.encode())
        self.assertEqual(fetcher.clean_url(items[0]["link"]), "https://example.com/odd")


class SummaryHygiene(unittest.TestCase):
    def test_reddit_boilerplate_is_not_a_summary(self):
        self.assertEqual(fetcher.clean_summary("submitted by /u/Dry_Radio_761 [link] [comments]"), "")

    def test_hacker_news_boilerplate_is_not_a_summary(self):
        noisy = "Article URL: https://www.project-syndicate.org/x Comments URL: https://news.ycombinator.com/item?id=1 Points: 44 # Comments: 12"
        self.assertEqual(fetcher.clean_summary(noisy), "")

    def test_a_bare_url_is_not_a_summary(self):
        self.assertEqual(fetcher.clean_summary("https://example.com/some/article"), "")

    def test_a_real_summary_survives(self):
        kept = fetcher.clean_summary("On Equity, we debate the impact of data center moratoriums and whether the AI industry has a communication problem.")
        self.assertIn("data center moratoriums", kept)

    def test_a_publishers_description_of_itself_is_dropped(self):
        self.assertEqual(fetcher.clean_summary(
            "The Verge is about technology and how it makes us feel. Founded in 2011, we offer our audience everything."), "")
        self.assertEqual(fetcher.clean_summary("The latest news in AI, delivered to your inbox."), "")

    def test_a_summary_that_repeats_the_headline_is_dropped(self):
        self.assertTrue(fetcher._echoes("Superintelligence race moves to desk work", "Superintelligence race moves to desk work"))


class RankingVocabulary(unittest.TestCase):
    def test_agi_does_not_match_inside_other_words(self):
        # This is the bug that gave half the internet a superintelligence score: "agi" in "again".
        self.assertEqual(fetcher.matches(["agi"], "we are talking about it again and again"), [])
        self.assertEqual(fetcher.matches(["agi"], "magic"), [])

    def test_agi_matches_as_a_word(self):
        self.assertEqual(fetcher.matches(["agi"], "their AGI claim landed"), ["agi"])

    def test_stems_still_match_inflections(self):
        self.assertEqual(fetcher.matches(["hallucinat*"], "the model was hallucinating again"), ["hallucinat*"])

    def test_generic_terms_are_not_scored(self):
        self.assertIn("ai", fetcher.SI_GENERIC)
        self.assertNotIn("superintelligence", fetcher.SI_GENERIC)

    def test_promos_are_recognised(self):
        self.assertTrue(fetcher.matches(fetcher.PROMO_TERMS, "48 hours to Disrupt 2026 - register now"))
        self.assertFalse(fetcher.matches(fetcher.PROMO_TERMS, "A frontier lab published a safety report"))


class SourceList(unittest.TestCase):
    def test_sources_are_well_formed_and_unique(self):
        sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
        self.assertGreaterEqual(len(sources), 10)
        ids, known = set(), {"lab", "research", "press", "commentary", "community", "paper", "aggregator"}
        for source in sources:
            self.assertNotIn(source["id"], ids, f"duplicate source id {source['id']}")
            ids.add(source["id"])
            self.assertTrue(source["url"].startswith("https://"), source["id"])
            self.assertIn(source["category"], known, source["id"])
            self.assertIn(source["tier"], (1, 2, 3), source["id"])
            self.assertTrue(source["name"].strip(), source["id"])

    def test_disabled_sources_say_why(self):
        for source in json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]:
            if not source.get("enabled", True):
                self.assertTrue(source.get("note"), f"{source['id']} is disabled without a reason")


class SourceTopicFilter(unittest.TestCase):
    """A source that publishes everything needs narrowing before it reaches an AI wire.

    The Anarchist Library archives every text it holds; without `require` the page fills with
    nineteenth-century pamphlets and an essay on the Paris Commune outranks a piece about models.
    """

    def item(self, title, summary="", content=""):
        return {"title": title, "summary": summary, "content": content}

    def test_a_source_without_a_filter_keeps_everything(self):
        self.assertTrue(fetcher.passes_source_filter({}, self.item("On the Paris Commune")))

    def test_a_topical_source_drops_what_it_did_not_ask_for(self):
        source = {"require": ["artificial intelligence", "machine learning"]}
        self.assertFalse(fetcher.passes_source_filter(source, self.item("Will 1793 Take Place?")))
        self.assertTrue(fetcher.passes_source_filter(
            source, self.item("Notes on machine learning and the commune")))

    def test_the_filter_reports_what_matched_so_the_card_can_say_why(self):
        source = {"require": ["generative ai", "automation"]}
        hits = fetcher.source_topic_hits(source, self.item("Against generative AI in the studio"))
        self.assertEqual(hits, ["generative ai"])

    def test_the_filter_reads_the_opening_not_the_whole_body(self):
        source = {"require": ["automation"]}
        deep = "x" * (fetcher.FILTER_CHARS + 50) + " automation"
        self.assertFalse(fetcher.passes_source_filter(source, self.item("A long essay", content=deep)))

    def test_the_filter_matches_words_not_fragments(self):
        source = {"require": ["ai"]}
        self.assertTrue(fetcher.passes_source_filter(source, self.item("AI and the class struggle")))
        self.assertFalse(fetcher.passes_source_filter(source, self.item("Again the same argument")))
        self.assertFalse(fetcher.passes_source_filter(source, self.item("Said nothing at all")))


class ArchivePreamble(unittest.TestCase):
    """The library prefixes each text with its own catalogue fields, which used to eat the whole dek."""

    def test_the_catalogue_fields_are_stripped(self):
        text = ("Author : ziq Title : Beware of Open Source Absolutists Date : September 7. 2026 "
                "Source : Retrieved on 2026-09-28 from raddle.me/wiki/x Open Source Is Not Liberation")
        cleaned = fetcher.strip_archive_preamble(text)
        self.assertTrue(cleaned.startswith("Open Source Is Not Liberation"), cleaned)
        self.assertNotIn("Author :", cleaned)

    def test_prose_that_happens_to_start_with_a_word_like_source_is_untouched(self):
        text = "Source code is not a constitution, and never was."
        self.assertEqual(fetcher.strip_archive_preamble(text), text)


class WireSelection(unittest.TestCase):
    """A curated, slow source must reach the page even though it is not news."""

    def item(self, source, score, published="2026-10-01T00:00:00+00:00"):
        return {"source": source, "score": score, "published": published}

    def test_a_kept_source_survives_the_cut(self):
        listed = [self.item("press", 100 - n) for n in range(10)] + [self.item("library", 8.0)]
        chosen = fetcher.select_items(listed, [{"id": "library", "keep": True}], limit=5)
        self.assertIn("library", [i["source"] for i in chosen])
        self.assertEqual(len(chosen), 5)

    def test_without_the_flag_the_cut_applies_normally(self):
        listed = [self.item("press", 100 - n) for n in range(10)] + [self.item("library", 8.0)]
        chosen = fetcher.select_items(listed, [{"id": "library"}], limit=5)
        self.assertNotIn("library", [i["source"] for i in chosen])

    def test_the_selection_stays_in_score_order(self):
        listed = [self.item("library", 8.0), self.item("press", 90.0)]
        chosen = fetcher.select_items(listed, [{"id": "library", "keep": True}], limit=2)
        self.assertEqual([i["score"] for i in chosen], [90.0, 8.0])


class StoryPages(unittest.TestCase):
    """A card that links to a page the build never wrote is a 404 the sitemap also promises."""

    def test_every_published_item_gets_a_story_page(self):
        source = (ROOT / "tools" / "build_site.py").read_text(encoding="utf-8")
        self.assertNotIn("items[:300]",
                         source,
                         "a capped loop leaves late items linking to a 404 the sitemap still promises")
        self.assertIn("for item in items:", source)


class PublishedSchema(unittest.TestCase):
    """Every field the site reads must exist on a published item.

    This is the test that was missing when the whole build died on a KeyError for sourceUrl: the
    fixture was richer than the real output, so the suite stayed green while the site would not build.
    """

    REQUIRED = ("id", "title", "url", "source", "sourceName", "sourceUrl", "sourceSite",
                "sourceCategory", "tier", "published", "summary", "content", "contentFrom",
                "image", "imageAlt", "imageFrom", "author", "score", "breaking", "upsy", "aiTerms",
                "siTerms", "upsyTerms", "upsyWhy", "promo", "question")

    def test_real_output_has_every_field_the_site_reads(self):
        news = ROOT / "data" / "news.json"
        if not news.exists():
            self.skipTest("no fetched data in this checkout; the build test covers the shape")
        item = json.loads(news.read_text(encoding="utf-8"))["items"][0]
        missing = [key for key in self.REQUIRED if key not in item]
        self.assertEqual(missing, [], f"published items are missing {missing}")

    def test_the_builder_reads_no_field_outside_that_list(self):
        source = (ROOT / "tools" / "build_site.py").read_text(encoding="utf-8")
        read = set(re.findall(r"item\[['\"]([a-zA-Z_]+)['\"]\]", source))
        # Fields that are added by the builder itself (a locally cached picture, a rendered age),
        # as opposed to fields a published item is required to carry.
        optional = {"age", "why", "brief", "moments", "id", "imageLocal"}
        self.assertFalse(read - set(self.REQUIRED) - optional,
                         f"build_site.py reads fields nobody publishes: {read - set(self.REQUIRED) - optional}")


class EditorFallback(unittest.TestCase):
    def test_json_is_found_inside_prose(self):
        parsed = editor.parse_reply('Here you go:\n```json\n{"brief": "quiet day", "moments": []}\n```')
        self.assertEqual(parsed["brief"], "quiet day")

    def test_a_reply_without_json_is_rejected_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            editor.parse_reply("I would rather not.")

    def test_a_truncated_reply_still_yields_its_moments(self):
        # What a small model returns when it runs out of room mid-object.
        cut = '{"brief": "Quiet day.", "moments": [{"id": "a1", "why": "It escaped."}, {"id": "b2", "wh'
        parsed = editor.parse_reply(cut, strict=False)
        self.assertEqual(parsed["brief"], "Quiet day.")
        self.assertEqual([m["id"] for m in parsed["moments"]], ["a1"])

    def test_a_reply_inside_a_sentence_is_still_read(self):
        parsed = editor.parse_reply('Sure! {"brief": "B.", "moments": []}')
        self.assertEqual(parsed["brief"], "B.")

    def test_rules_engine_never_invents_a_story(self):
        items = [{"id": "a", "title": "T", "url": "https://x.test/1", "sourceName": "X", "image": "",
                  "summary": "s", "published": "", "upsy": True, "promo": False, "question": False,
                  "sourceCategory": "community", "breaking": False}]
        result = editor.rules_engine(items, fetcher.datetime.now(fetcher.timezone.utc))
        self.assertEqual([m["id"] for m in result["moments"]], ["a"])
        self.assertEqual(result["moments"][0]["url"], "https://x.test/1")


class SiteBuild(unittest.TestCase):
    """Build the site from a fixture and read what came out."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        (root / "data").mkdir()
        items = [{
            "id": "abc123", "title": "A frontier lab shipped something strange",
            "url": "https://example.com/story", "source": "example", "sourceName": "Example AI",
            "sourceUrl": "https://example.com/feed", "sourceCategory": "media", "tier": 2,
            "published": "2026-10-11T09:00:00+00:00", "summary": "It did the strange thing on a Tuesday.",
            "content": "<p>Body paragraph one.</p><p>Body paragraph two.</p>", "contentFrom": "feed",
            "image": "https://example.com/story.jpg", "author": "A", "score": 100.0, "breaking": True,
            "upsy": True, "upsyWhy": "Because it did.", "siTerms": ["superintelligence"],
            "promo": False, "question": False, "tags": ["t"],
        }]
        for n in range(5):
            items.append({**items[0], "id": f"other{n}", "title": f"Another story {n}",
                          "url": f"https://example.com/{n}", "breaking": False, "upsy": False,
                          "image": "", "content": "", "contentFrom": ""})
        news = {"generated": "2026-10-11T10:00:00+00:00", "counts": {
            "items": len(items), "sources": 1, "breaking": 1, "upsy": 1, "withImage": 1,
            "withFullText": 1, "new": 1, "promo": 0}, "items": items}
        meta = {"generated": news["generated"], "counts": news["counts"],
                "health": [{"id": "example", "name": "Example AI", "status": "ok 200", "items": len(items),
                            "new": 1, "newest": items[0]["published"]}]}
        (root / "data" / "news.json").write_text(json.dumps(news), encoding="utf-8")
        (root / "data" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (root / "data" / "moments.json").write_text(json.dumps({
            "engine": "test", "brief": "A brief.", "generated": news["generated"],
            "topIds": [items[0]["id"]],
            "moments": [{"id": "abc123", "title": items[0]["title"], "url": items[0]["url"],
                         "sourceName": "Example AI", "image": "", "published": items[0]["published"],
                         "why": "Because it did."}]}), encoding="utf-8")
        (root / "sources.json").write_text((ROOT / "sources.json").read_text(encoding="utf-8"), encoding="utf-8")

        cls._site, cls._data = build_site.SITE, build_site.DATA
        build_site.SITE = root / "_site"
        build_site.DATA = root / "data"
        build_site.ROOT = root
        build_site.CACHE_IMAGES = False   # the suite must never reach the network
        build_site.main()
        cls.site = root / "_site"

    @classmethod
    def tearDownClass(cls):
        build_site.SITE, build_site.DATA = cls._site, cls._data
        cls.tmp.cleanup()

    def test_index_lists_stories(self):
        page = (self.site / "index.html").read_text(encoding="utf-8")
        self.assertIn("A frontier lab shipped something strange", page)
        self.assertIn("Another story 3", page)
        self.assertIn('class="row', page)

    def test_every_story_has_its_own_page_that_links_out(self):
        page = (self.site / "s" / "abc123" / "index.html").read_text(encoding="utf-8")
        self.assertIn("read the original at Example AI", page)
        self.assertIn("https://example.com/story", page)
        self.assertIn("Body paragraph two", page)  # full text came from the feed

    def test_story_page_carries_metadata_for_sharing(self):
        page = (self.site / "s" / "abc123" / "index.html").read_text(encoding="utf-8")
        self.assertIn('property="og:title"', page)
        self.assertIn('rel="canonical" href="https://upsy.si/s/abc123/"', page)
        self.assertIn("application/ld+json", page)
        self.assertIn('"@type": "NewsArticle"', page)

    def test_a_story_without_full_text_says_so(self):
        page = (self.site / "s" / "other0" / "index.html").read_text(encoding="utf-8")
        self.assertIn("publishes only a summary", page)

    def test_feed_and_sitemap_are_valid_xml(self):
        for name in ("feed.xml", "sitemap.xml"):
            ET.fromstring((self.site / name).read_text(encoding="utf-8"))
        sitemap = (self.site / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("https://upsy.si/s/abc123/", sitemap)

    def test_archive_data_matches_the_page(self):
        payload = json.loads((self.site / "data" / "news.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["items"]), 6)
        self.assertTrue(all("age" in item for item in payload["items"]))

    def test_claim_files_are_present(self):
        self.assertEqual((self.site / "CNAME").read_text(encoding="utf-8").strip(), "upsy.si")
        self.assertTrue((self.site / "robots.txt").read_text(encoding="utf-8").startswith("User-agent: *"))
        self.assertTrue((self.site / ".nojekyll").exists())

    def test_filtered_rows_are_actually_hidden(self):
        """A filter that sets `hidden` on `display:grid` rows renders nothing.

        The rule has to outrank `.row`, and the script has to do more than flip the property - the
        first version of this looked correct and changed nothing on screen.
        """
        page = (self.site / "index.html").read_text(encoding="utf-8")
        self.assertIn(".row[hidden], .row.is-hidden { display:none; }", page)
        self.assertIn("classList.toggle('is-hidden'", page)

    def test_a_story_with_no_picture_takes_the_screenshot_of_its_page(self):
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            site, shots = pathlib.Path(td) / "site", pathlib.Path(td) / "shots"
            site.mkdir(); shots.mkdir()
            want = "https://example.test/a-story-with-no-picture"
            shot = shots / (hashlib.sha1(want.encode()).hexdigest()[:20] + ".png")
            shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 400)      # magic bytes are what the builder checks
            html = shots / (hashlib.sha1(b"https://example.test/challenged").hexdigest()[:20] + ".png")
            html.write_bytes(b"<!DOCTYPE html><html>verify you are human</html>")

            items = [{"url": want, "image": "", "imageLocal": ""},
                     {"url": "https://example.test/challenged", "image": "", "imageLocal": ""},
                     {"url": "https://example.test/has-a-picture", "image": "https://cdn/x.jpg",
                      "imageLocal": ""}]
            # a story whose feed carried a picture keeps it, a challenge page is not a picture
            self.assertEqual(build_site.attach_shots(items, 10, site, shots), 1)
            self.assertEqual(items[0]["imageLocal"], f"/img/{shot.name}")
            self.assertEqual(items[1]["imageLocal"], "")
            self.assertEqual(items[2]["imageLocal"], "")
            self.assertTrue((site / "img" / shot.name).exists())

    def test_an_atom_id_that_is_only_a_front_door_is_refused(self):
        """The Verge publishes <id>?p=NNNN</id> alongside the real link; the href must win."""
        feed = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <title>AI | The Verge</title>
          <entry>
            <title>A story with a real address</title>
            <id>https://www.theverge.com/?p=1009406</id>
            <link rel="alternate" type="text/html" href="https://www.theverge.com/ai/1009406/a-story"/>
            <updated>2026-10-11T10:00:00Z</updated>
          </entry>
        </feed>"""
        items = fetcher.parse_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://www.theverge.com/ai/1009406/a-story")

    def test_a_story_whose_only_link_is_the_homepage_is_dropped(self):
        feed = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <title>x</title>
          <entry><title>Nothing to click</title>
            <id>https://www.theverge.com/?p=1009406</id>
            <updated>2026-10-11T10:00:00Z</updated></entry>
        </feed>"""
        self.assertEqual(fetcher.parse_feed(feed), [])

    def test_a_plain_rss_link_still_wins(self):
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
          <item><title>RSS style</title><link>https://example.test/2026/10/a-story</link>
          <pubDate>Sat, 11 Oct 2026 10:00:00 +0000</pubDate></item>
        </channel></rss>"""
        items = fetcher.parse_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://example.test/2026/10/a-story")

    def test_every_source_is_a_filter_button_and_every_row_carries_its_source(self):
        page = (self.site / "index.html").read_text(encoding="utf-8")
        listed = build_site.re.findall(r'class="net-item" data-src="([^"]+)"', page)
        self.assertTrue(listed, "the sidebar must list the sources it has, not a top slice")
        rows = build_site.re.findall(r'class="row[^"]*"[^>]*data-src="([^"]+)"', page)
        self.assertTrue(rows, "rows must carry the source they came from")
        for key in set(rows):
            self.assertIn(key, listed, f"row source {key!r} has no sidebar button")
        self.assertIn("state.sources.size && !state.sources.has(row.dataset.src)", page)
        self.assertIn("article.dataset.src = item.src", page)

    def test_a_sticky_button_gets_the_reader_back_to_the_top(self):
        page = (self.site / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="to-top"', page)
        # hidden by display, so an author rule cannot lose to the browser's own [hidden]
        self.assertIn("#to-top { display:none;", page)
        self.assertIn("#to-top.is-on { display:flex; }", page)
        self.assertIn("window.scrollY > 700", page)
        self.assertIn('id="top-mark"', page)
        self.assertIn("new IntersectionObserver(function (entries)", page)
        self.assertIn("setInterval(watchScroll, 250)", page)
        self.assertIn("window.scrollTo(0, 0);", page)

    def test_the_source_network_is_a_drawer_behind_a_header_icon(self):
        page = (self.site / "index.html").read_text(encoding="utf-8")
        header = page[page.index("<header"):page.index("</header>")]
        self.assertIn('id="net-toggle"', header, "the network icon belongs in the header, top right")
        self.assertIn('aria-controls="net-drawer"', header)
        # The list has to live in the drawer, and the right column has to be free of it: two copies
        # of one id is how a drawer opens onto nothing.
        drawer = page[page.index('<aside class="net-drawer"'):page.index("</aside>", page.index('<aside class="net-drawer"'))]
        self.assertIn('id="net"', drawer)
        self.assertIn('class="net-item"', drawer)
        column = page[page.index("<aside>"):page.index("</aside>", page.index("<aside>"))]
        self.assertNotIn('id="net"', column, "the source list is no longer the right column's")
        self.assertIn("upsy moments", column)
        self.assertIn("what this is", column)

    def test_the_moved_list_still_filters_the_stream(self):
        page = (self.site / "index.html").read_text(encoding="utf-8")
        # The filter finds the list by id. Move the list and forget the id, and every source tap
        # becomes a button that changes nothing.
        self.assertIn("querySelectorAll('#net .net-item')", page)
        self.assertIn("state.sources.has(key)", page)
        self.assertIn('data-src="', page)
        # And with the list out of sight, the icon has to show that a source is selected.
        self.assertIn("netToggle.classList.toggle('is-filtered'", page)
        self.assertIn('id="net-count"', page)
        self.assertIn("state.sources.clear()", page)

    def test_the_drawer_closes_by_keyboard_and_backdrop(self):
        page = (self.site / "index.html").read_text(encoding="utf-8")
        self.assertIn("event.key === 'Escape'", page)
        self.assertIn('id="net-backdrop"', page)
        self.assertIn("netBackdrop.addEventListener('click'", page)
        self.assertIn("body.net-open", page, "an open drawer must hold the page still behind it")

    def test_the_network_icon_is_open_source_and_credited(self):
        page = (self.site / "index.html").read_text(encoding="utf-8")
        self.assertIn("lucide (ISC licence)", page)
        icon = page[page.index('id="net-toggle"'):]
        self.assertIn("<svg", icon[:1200])

    def test_the_inline_scripts_are_valid_javascript(self):
        """A syntax error in one script block silently kills it, filters and all.

        Catches an unbalanced brace or a broken string. It does *not* catch a collision with a browser
        global (a top-level `const top` against window.top) because node has no such global - only the
        in-browser walk-through sees that one.
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed here; the in-browser check covers this")
        page = (self.site / "index.html").read_text(encoding="utf-8")
        blocks = build_site.re.findall(r"<script>(.*?)</script>", page, build_site.re.S)
        self.assertTrue(blocks, "the page should carry its own scripts")
        for n, block in enumerate(blocks):
            source = pathlib.Path(self.tmp.name) / f"inline{n}.js"
            source.write_text(block, encoding="utf-8")
            result = subprocess.run([node, "--check", str(source)], capture_output=True)
            self.assertEqual(result.returncode, 0,
                             f"script block {n} does not parse: {result.stderr.decode()[:300]}")

    def test_nothing_renders_as_python_none(self):
        for path in [p for p in self.site.rglob("*.html")]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(">None<", text, str(path))
            self.assertNotIn("undefined", text, str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class EventPagesArePromotionsTest(unittest.TestCase):
    """A meetup page is a promotion with a date on it, and it used to rank like news.

    The board is sorted by score, so an event page that keeps its full freshness sits at the top:
    one did, at 153 energy, because its feed summary was a bland sentence and never said "register"
    in the two lines the card shows.
    """

    def signals(self, **item):
        base = {"title": "A talk about alignment", "summary": "Doors at seven.",
                "url": "https://example.com/posts/one", "content": ""}
        base.update(item)
        return fetcher.promo_signals(base, f"{base['title']} {base['summary']}".lower())

    def test_an_event_address_is_a_promotion_on_its_own(self):
        # The reader's own link, kept as the fixture.
        url = "https://www.lesswrong.com/events/4iNmmyrLALGr6d8tw/krasnoyarsk-acx-meetups-everywhere-fall-2026"
        self.assertEqual(self.signals(url=url), ["path:events"])

    def test_an_event_body_is_a_promotion_when_the_summary_says_nothing(self):
        signals = self.signals(content="Registration is open for our fall meetup. Space is limited, so reserve your spot early.")
        self.assertEqual(sorted(signals), ["registration is", "reserve your spot", "space is limited"])

    def test_a_story_mentioning_a_newsletter_is_not_a_promotion(self):
        # The failure mode this narrowing exists to prevent: nearly every article carries this line.
        signals = self.signals(
            title="OpenAI's new model tops the benchmarks",
            summary="The company says the training run was its largest.",
            content="The company published results on Tuesday. Subscribe to our newsletter for more coverage of the field.",
        )
        self.assertEqual(signals, [])

    def test_a_word_merely_containing_a_segment_is_not_a_promotion(self):
        self.assertEqual(self.signals(url="https://example.com/posts/events-are-fun"), [])
        self.assertEqual(self.signals(url="https://example.com/posts/eventual-consistency"), [])

    def test_the_card_still_reads_first_and_wins(self):
        signals = self.signals(title="Register now for our webinar", url="https://example.com/posts/one")
        self.assertIn("register", signals)
        self.assertNotIn("path:events", signals)
