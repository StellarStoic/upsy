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


class PublishedSchema(unittest.TestCase):
    """Every field the site reads must exist on a published item.

    This is the test that was missing when the whole build died on a KeyError for sourceUrl: the
    fixture was richer than the real output, so the suite stayed green while the site would not build.
    """

    REQUIRED = ("id", "title", "url", "source", "sourceName", "sourceUrl", "sourceSite",
                "sourceCategory", "tier", "published", "summary", "content", "contentFrom",
                "image", "imageAlt", "imageFrom", "author", "score", "breaking", "upsy",
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

    def test_nothing_renders_as_python_none(self):
        for path in [p for p in self.site.rglob("*.html")]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(">None<", text, str(path))
            self.assertNotIn("undefined", text, str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
