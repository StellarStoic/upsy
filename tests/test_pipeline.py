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
        ids, known = set(), {"lab", "research", "press", "commentary", "community", "paper",
                             "aggregator", "podcast"}
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


class PerSourceCap(unittest.TestCase):
    def test_no_single_feed_fills_the_wire(self):
        """Fifty-four sources mean little if the one that publishes a hundred items a day takes the tail."""
        items = [{"source": "prolific", "score": 100 - n} for n in range(20)]
        items += [{"source": "quiet", "score": 10}, {"source": "quiet", "score": 9}]
        kept = fetcher.cap_per_source(items, 8)
        self.assertEqual(len([i for i in kept if i["source"] == "prolific"]), 8)
        self.assertEqual(len([i for i in kept if i["source"] == "quiet"]), 2)
        self.assertEqual([i["score"] for i in kept if i["source"] == "prolific"], [100, 99, 98, 97, 96, 95, 94, 93],
                         "the cap keeps the best of them, in score order")

    def test_the_cap_leaves_a_source_below_it_untouched(self):
        items = [{"source": "small", "score": 5}] * 8
        self.assertEqual(len(fetcher.cap_per_source(items, 8)), 8)


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
                "siTerms", "upsyTerms", "upsyWhy", "promo", "question",
                "kind", "audio", "audioType", "duration", "transcript", "transcriptType",
                "chapters", "transcriptText",
                "contentHtml")

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
        self.assertIn("more from the stream", page)
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

    def test_a_guid_written_before_the_link_does_not_become_the_link(self):
        """Apple's RSS puts <guid> first, and its guid is a slug: an identifier, never an address."""
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
          <item><guid>mlx-distributed-inference</guid>
            <title>Local and distributed LLM inference</title>
            <link>https://machinelearning.apple.com/research/mlx-distributed-inference</link>
            <pubDate>Thu, 15 Oct 2026 00:00:00 GMT</pubDate></item>
        </channel></rss>"""
        items = fetcher.parse_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"],
                         "https://machinelearning.apple.com/research/mlx-distributed-inference")

    def test_a_feed_that_links_to_its_own_posts_relatively_still_publishes(self):
        """Jekyll's Atom writes <link href="/blog/post">, which is a story once you know the feed's host."""
        feed = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
          <entry><title>Open house 2026</title>
            <id>/open-house-2026</id>
            <link href="/open-house-2026" rel="alternate" type="text/html"/>
            <updated>2026-10-11T10:00:00Z</updated></entry></feed>"""
        self.assertEqual(fetcher.parse_feed(feed), [],
                         "with no feed address there is nothing to resolve against, so it is dropped")
        items = fetcher.parse_feed(feed, "https://sakana.ai/feed")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://sakana.ai/open-house-2026")

    def test_a_bare_slug_is_not_mistaken_for_a_route(self):
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
          <item><title>Only an identifier</title><guid>some-slug</guid></item>
        </channel></rss>"""
        self.assertEqual(fetcher.parse_feed(feed, "https://example.test/feed"), [])

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


class PageWeight(unittest.TestCase):
    """What the page actually sends: a heading, a card, and pictures at the size it draws them."""

    def fixture(self, **item_overrides):
        item = {
            "id": "abc123", "title": "A frontier lab shipped something strange",
            "url": "https://example.com/story", "source": "example", "sourceName": "Example AI",
            "sourceUrl": "https://example.com/feed", "sourceCategory": "media", "tier": 2,
            "published": "2026-10-11T09:00:00+00:00", "summary": "It did the strange thing on a Tuesday.",
            "content": "", "contentFrom": "", "image": "https://example.com/story.jpg",
            "author": "A", "score": 100.0, "breaking": True, "upsy": True, "upsyWhy": "Because it did.",
            "siTerms": ["superintelligence"], "promo": False, "question": False, "tags": ["t"],
        }
        item.update(item_overrides)
        counts = {"items": 1, "sources": 1, "breaking": 1, "upsy": 1, "withImage": 1,
                  "withFullText": 0, "new": 1, "promo": 0}
        return {"generated": "2026-10-11T10:00:00+00:00", "counts": counts, "items": [item]}

    def test_the_front_page_has_a_heading(self):
        page = build_site.build_index(self.fixture(), {"health": []}, {}, build_site.datetime.now(
            build_site.timezone.utc))
        self.assertIn("<h1", page)
        self.assertIn("superintelligence</h1>", page)

    def test_the_front_page_shares_a_picture_this_build_owns(self):
        data = self.fixture(imageLocal="/img/abc123.webp")
        page = build_site.build_index(data, {"health": []}, {}, build_site.datetime.now(
            build_site.timezone.utc))
        self.assertIn('property="og:image" content="https://upsy.si/img/abc123.webp"', page)
        self.assertIn('name="twitter:card" content="summary_large_image"', page)

    def test_a_publisher_url_is_never_used_as_a_picture(self):
        # The measured failure: twelve cards hotlinking multi-megabyte originals because the mirror
        # had refused them. A card with no picture is the cheaper outcome.
        self.assertEqual(build_site.picture_of({"image": "https://example.com/huge.jpg"}), "")
        self.assertEqual(build_site.picture_of({"imageLocal": "/img/a.webp",
                                                "image": "https://example.com/huge.jpg"}), "/img/a.webp")

    def test_pictures_are_shrunk_to_the_width_the_page_draws(self):
        try:
            from io import BytesIO

            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed here; the workflow installs it")
        buffer = BytesIO()
        Image.new("RGB", (2000, 1200), (200, 30, 30)).save(buffer, format="PNG")
        original = buffer.getvalue()
        shrunk, suffix = build_site.optimise_picture(original, ".png")
        self.assertEqual(suffix, ".webp")
        self.assertLess(len(shrunk), len(original))
        with Image.open(BytesIO(shrunk)) as result:
            self.assertEqual(result.width, build_site.IMAGE_MAX_WIDTH)

    def test_a_picture_no_page_names_is_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = pathlib.Path(tmp)
            (site / "img").mkdir()
            (site / "img" / "used.webp").write_bytes(b"RIFF0000WEBP")
            (site / "img" / "orphan.webp").write_bytes(b"RIFF0000WEBP")
            (site / "index.html").write_text('<img src="/img/used.webp">', encoding="utf-8")
            original = build_site.SITE
            build_site.SITE = site
            try:
                removed = build_site.prune_unused_pictures()
            finally:
                build_site.SITE = original
            self.assertEqual(removed, 1)
            self.assertTrue((site / "img" / "used.webp").exists())
            self.assertFalse((site / "img" / "orphan.webp").exists())


class PodcastLane(unittest.TestCase):
    """An episode is audio with a page, and it stays out of the wire's stream."""

    FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel><title>A show</title>
    <item>
      <title>Episode one</title>
      <link>https://example.com/ep1</link>
      <pubDate>Sat, 24 Oct 2026 10:00:00 +0000</pubDate>
      <description>&lt;p&gt;A long and meaningful description of what this episode is about,
      whom it interviews and why it is worth an hour of anyone's day. It runs well past any
      teaser length, which is the point of keeping it whole.&lt;/p&gt;</description>
      <enclosure url="https://cdn.example.com/ep1.mp3" type="audio/mpeg" length="52428800"/>
      <itunes:duration>1:02:03</itunes:duration>
      <podcast:transcript url="https://example.com/ep1.vtt" type="text/vtt"/>
    </item>
    <item>
      <title>Episode two</title>
      <link>https://example.com/ep2</link>
      <pubDate>Sat, 17 Oct 2026 10:00:00 +0000</pubDate>
      <description>Short.</description>
      <enclosure url="https://cdn.example.com/ep2-art.jpg" type="image/jpeg" length="2048"/>
    </item>
  </channel>
</rss>"""

    def raw(self):
        return fetcher.parse_feed(self.FEED)

    def test_durations_are_read_from_all_three_shapes_feeds_publish(self):
        self.assertEqual(fetcher.duration_seconds("3723"), 3723)
        self.assertEqual(fetcher.duration_seconds("62:03"), 3723)
        self.assertEqual(fetcher.duration_seconds("1:02:03"), 3723)
        self.assertEqual(fetcher.duration_seconds(""), 0)

    def test_an_episode_keeps_its_audio_its_length_and_its_transcript(self):
        fields = fetcher.episode_fields({"kind": "podcast"}, self.raw()[0])
        self.assertEqual(fields["kind"], "podcast")
        self.assertEqual(fields["audio"], "https://cdn.example.com/ep1.mp3")
        self.assertEqual(fields["duration"], 3723)
        self.assertEqual(fields["transcript"], "https://example.com/ep1.vtt")
        self.assertEqual(fields["transcriptType"], "text/vtt")
        # the description is the page's only text before pressing play, so it is kept whole
        self.assertIn("worth an hour", fields.get("content", ""))

    def test_a_picture_in_an_enclosure_is_not_audio(self):
        fields = fetcher.episode_fields({"kind": "podcast"}, self.raw()[1])
        self.assertEqual(fields["audio"], "")
        self.assertEqual(fields["duration"], 0)

    def test_news_items_carry_the_same_fields_empty(self):
        fields = fetcher.episode_fields({"kind": "news"}, self.raw()[0])
        self.assertEqual(fields["kind"], "news")
        self.assertEqual(fields["audio"], "")
        self.assertEqual(fields["duration"], 0)

    def test_the_editor_is_never_shown_an_episode(self):
        items = [{"id": "a", "upsy": True, "promo": False, "question": False, "kind": "podcast",
                  "title": "Episode", "sourceName": "Show", "summary": ""},
                 {"id": "b", "upsy": False, "promo": False, "question": False, "kind": "news",
                  "title": "Story", "sourceName": "Outlet", "summary": ""}]
        self.assertEqual([i["id"] for i in editor.candidates(items)], ["b"])

    def test_duration_labels_read_like_a_length(self):
        self.assertEqual(build_site.duration_label(3723), "1 h 02 min")
        self.assertEqual(build_site.duration_label(3000), "50 min")
        self.assertEqual(build_site.duration_label(3600), "1 h")
        self.assertEqual(build_site.duration_label(0), "")

    def test_the_player_asks_for_nothing_until_play_is_pressed(self):
        episode = {"kind": "podcast", "audio": "https://cdn.example.com/ep1.mp3",
                   "sourceName": "A show", "duration": 3723,
                   "transcript": "https://example.com/ep1.vtt"}
        player = build_site.player_html(episode)
        self.assertIn('preload="none"', player)
        self.assertIn('controls', player)
        self.assertIn("https://cdn.example.com/ep1.mp3", player)
        self.assertIn("the show&#39;s own transcript", player)
        self.assertIn("1 h 02 min", player)
        self.assertEqual(build_site.player_html({"kind": "news", "audio": "https://x/y.mp3"}), "")


class PodcastSite(unittest.TestCase):
    """Build the whole site from a fixture that holds one episode and one story."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        (root / "data").mkdir()
        story = {"id": "news1", "title": "A lab shipped something strange",
                 "url": "https://example.com/story", "source": "example", "sourceName": "Example AI",
                 "sourceUrl": "https://example.com/feed", "sourceSite": "https://example.com",
                 "sourceCategory": "media", "tier": 2, "published": "2026-10-11T09:00:00+00:00",
                 "summary": "It did the strange thing.", "content": "", "contentFrom": "",
                 "image": "", "imageLocal": "", "imageAlt": "", "imageFrom": "", "author": "A",
                 "score": 100.0, "breaking": True, "upsy": True, "upsyWhy": "", "upsyTerms": [],
                 "aiTerms": [], "siTerms": ["superintelligence"], "promo": False, "question": False,
                 "kind": "news", "audio": "", "audioType": "", "duration": 0, "transcript": "",
                 "transcriptType": ""}
        episode = {**story, "id": "ep1", "title": "An hour on superintelligence",
                   "url": "https://example.com/ep1", "source": "show", "sourceName": "A Show",
                   "sourceCategory": "podcast", "published": "2026-10-11T11:00:00+00:00",
                   "summary": "A long and meaningful description of the episode.",
                   "breaking": False, "upsy": False, "kind": "podcast",
                   "audio": "https://cdn.example.com/ep1.mp3", "audioType": "audio/mpeg",
                   "duration": 3723, "transcript": "https://example.com/ep1.vtt",
                   "transcriptType": "text/vtt"}
        items = [story, episode]
        news = {"generated": "2026-10-11T12:00:00+00:00",
                "counts": {"items": 2, "sources": 2, "breaking": 1, "upsy": 1, "withImage": 0,
                           "withFullText": 0, "new": 2, "promo": 0}, "items": items}
        meta = {"generated": news["generated"], "counts": news["counts"],
                "health": [{"id": "show", "name": "A Show", "status": "ok 200", "items": 1, "new": 1,
                            "newest": episode["published"]}]}
        (root / "data" / "news.json").write_text(json.dumps(news), encoding="utf-8")
        (root / "data" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (root / "sources.json").write_text((ROOT / "sources.json").read_text(encoding="utf-8"),
                                           encoding="utf-8")
        cls._site, cls._data, cls._root = build_site.SITE, build_site.DATA, build_site.ROOT
        build_site.SITE, build_site.DATA, build_site.ROOT = root / "_site", root / "data", root
        build_site.CACHE_IMAGES = False
        build_site.main()
        cls.site = root / "_site"

    @classmethod
    def tearDownClass(cls):
        build_site.SITE, build_site.DATA, build_site.ROOT = cls._site, cls._data, cls._root
        cls.tmp.cleanup()

    def test_the_episode_has_a_page_with_a_player(self):
        page = (self.site / "s" / "ep1" / "index.html").read_text(encoding="utf-8")
        self.assertIn("<audio class=\"player\"", page)
        self.assertIn('preload="none"', page)
        self.assertIn("https://cdn.example.com/ep1.mp3", page)
        self.assertIn('"@type": "PodcastEpisode"', page)
        self.assertIn("https://example.com/ep1.vtt", page)

    def test_the_story_page_has_no_player(self):
        page = (self.site / "s" / "news1" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<audio", page)

    def test_the_listen_page_lists_the_episode_and_not_the_story(self):
        page = (self.site / "listen.html").read_text(encoding="utf-8")
        self.assertIn("An hour on superintelligence", page)
        self.assertNotIn("A lab shipped something strange", page)
        self.assertIn('class="badge">transcript', page)

    def test_the_stream_carries_stories_and_the_lane_carries_episodes(self):
        page = (self.site / "index.html").read_text(encoding="utf-8")
        stream = page.split('<section class="rows" id="stream">')[1].split("</section>")[0]
        self.assertIn("A lab shipped something strange", stream)
        self.assertNotIn("An hour on superintelligence", stream)
        self.assertIn('class="listen-row"', page)
        self.assertIn("An hour on superintelligence", page)     # in the lane, not the stream

    def test_the_served_json_keeps_them_apart(self):
        news = json.loads((self.site / "data" / "news.json").read_text(encoding="utf-8"))
        episodes = json.loads((self.site / "data" / "episodes.json").read_text(encoding="utf-8"))
        self.assertEqual([i["id"] for i in news["items"]], ["news1"])
        self.assertEqual([i["id"] for i in episodes["items"]], ["ep1"])
        self.assertEqual(episodes["items"][0]["audio"], "https://cdn.example.com/ep1.mp3")
        self.assertEqual(episodes["items"][0]["durationSeconds"], 3723)

    def test_the_sitemap_lists_the_listen_page(self):
        sitemap = (self.site / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("https://upsy.si/listen.html", sitemap)


class StoryPageFurniture(unittest.TestCase):
    """The way back, the single column, and a centred episode: the things a reader asked for."""

    ITEM = {"id": "ep1", "title": "An hour on superintelligence", "url": "https://example.com/ep1",
            "source": "show", "sourceName": "A Show", "sourceUrl": "https://example.com/feed",
            "sourceSite": "", "sourceCategory": "podcast", "tier": 2,
            "published": "2026-10-11T11:00:00+00:00", "summary": "A description.",
            "content": "A description long enough to be page text.", "contentFrom": "feed",
            "image": "", "imageLocal": "", "imageAlt": "", "imageFrom": "", "author": "A",
            "score": 100.0, "breaking": False, "upsy": False, "upsyWhy": "", "upsyTerms": [],
            "aiTerms": [], "siTerms": ["superintelligence"], "promo": False, "question": False,
            "kind": "podcast", "audio": "https://cdn.example.com/ep1.mp3", "audioType": "audio/mpeg",
            "duration": 3723, "transcript": "", "transcriptType": ""}

    def page(self, **overrides):
        item = {**self.ITEM, **overrides}
        return build_site.build_story(item, [], build_site.datetime.now(build_site.timezone.utc), {})

    def test_an_episode_goes_back_to_the_listening_feed(self):
        page = self.page()
        self.assertIn('<a class="back" href="/listen.html">&larr; Podcasts</a>', page)
        self.assertIn('<a class="crumb" href="/">the wire</a>', page)
        self.assertNotIn('<a class="back" href="/">', page)

    def test_a_story_still_goes_back_to_the_wire(self):
        page = self.page(kind="news", audio="", duration=0)
        self.assertIn('<a class="back" href="/">&larr; upsy.si</a>', page)
        self.assertIn('<a class="crumb" href="/sources.html">sources</a>', page)

    def test_the_way_back_is_at_the_top_of_the_page(self):
        for page in (self.page(), self.page(kind="news", audio="", duration=0)):
            self.assertIn('<nav class="story-top">', page)
            # above the article, not buried under it
            self.assertLess(page.index('class="story-top"'), page.index('<h1>'))

    def test_the_foot_of_the_page_agrees_with_the_top(self):
        episode = self.page()
        self.assertIn('<a href="/listen.html">&larr; back to Podcasts</a>', episode)
        story = self.page(kind="news", audio="", duration=0)
        self.assertIn('<a href="/">&larr; back to upsy.si</a>', story)

    def test_a_story_page_is_one_column_not_the_front_pages_grid(self):
        self.assertIn('<main class="single">', self.page())

    def test_an_episode_is_centred_and_carries_the_episode_class(self):
        page = self.page()
        self.assertIn('<div class="story episode">', page)
        self.assertIn('.story.episode { text-align:center; }', build_site.CSS)
        self.assertIn('.story.episode .kicker { justify-content:center; }', build_site.CSS)
        self.assertIn('main.single { display:block; }', build_site.CSS)

    def test_an_episode_kicker_says_how_long_it_runs_not_its_energy(self):
        page = self.page()
        self.assertIn("<span>1 h 02 min</span>", page)
        self.assertNotIn("energy 100", page)
        # a story still shows its energy
        self.assertIn("energy 100", self.page(kind="news", audio="", duration=0))

    def test_the_kicker_gets_room_between_its_spans(self):
        self.assertIn("gap:24px", build_site.CSS)
        self.assertIn('.story .kicker > span + span::before', build_site.CSS)


class SponsorsInDescriptions(unittest.TestCase):
    """Adverts out of an episode's description, and the links left clickable."""

    SPONSOR_BLOCK = ('<p>AI is changing cybersecurity.</p>'
                     '<p>Featuring:</p><ul><li>Spencer – <a href="https://example.com/spencer">LinkedIn</a></li></ul>'
                     '<p>Links:</p><ul><li><a href="https://www.vulncheck.com/">VulnCheck</a></li>'
                     '<li>Go Exploit framework: https://github.com/vulncheck-oss/go-exploit</li></ul>'
                     '<p>Sponsors:</p><ul><li><a href="https://www.hostinger.com/">Hostinger</a>: use code '
                     'practical for 10 percent off.</li></ul>'
                     '<p>Resources and Events:</p><ul><li>Meet us at https://example.com/summit</li></ul>')

    PLAIN_BLOCK = """Hosts examine whether capital sets the pace of intelligence.
For full show notes read the episode page:https://example.com/notes
Sponsors:
OutSystems: the leading agentic platform. Start at https://outsystems.com/tcr
Parallel: web search for agents at https://parallel.ai/tcr
CHAPTERS:
(00:00) Weekly episode preview
(04:58) Recursive self improvement
SOCIAL LINKS:
Website: https://example.com/show"""

    def test_the_sponsor_block_is_gone(self):
        cleaned = fetcher.clean_episode_html(self.SPONSOR_BLOCK)
        self.assertNotIn("hostinger", cleaned.lower())
        self.assertNotIn("10 percent off", cleaned.lower())
        self.assertNotIn("Sponsors:", cleaned)

    def test_what_comes_after_the_sponsors_survives(self):
        cleaned = fetcher.clean_episode_html(self.SPONSOR_BLOCK)
        self.assertIn("AI is changing cybersecurity", cleaned)
        self.assertIn("Meet us at", cleaned)          # the next labelled section
        self.assertIn("https://www.vulncheck.com/", cleaned)

    def test_links_keep_their_words(self):
        cleaned = fetcher.clean_episode_html(self.SPONSOR_BLOCK)
        self.assertIn('<a href="https://www.vulncheck.com/" rel="noopener nofollow" target="_blank">VulnCheck</a>',
                      cleaned)
        self.assertIn(">LinkedIn</a>", cleaned)

    def test_a_bare_url_becomes_a_link(self):
        cleaned = fetcher.clean_episode_html(self.SPONSOR_BLOCK)
        self.assertIn('<a href="https://github.com/vulncheck-oss/go-exploit"', cleaned)
        self.assertIn('<a href="https://example.com/summit"', cleaned)

    def test_a_description_with_no_markup_is_read_line_by_line(self):
        cleaned = fetcher.clean_episode_html(self.PLAIN_BLOCK)
        self.assertNotIn("outsystems.com", cleaned.lower())
        self.assertNotIn("parallel.ai", cleaned.lower())
        self.assertIn("CHAPTERS:", cleaned)            # the advertised chapters are not an advert block
        self.assertIn("Recursive self improvement", cleaned)
        self.assertIn('<a href="https://example.com/notes"', cleaned)
        self.assertIn('<a href="https://example.com/show"', cleaned)

    def test_nothing_that_can_run_survives(self):
        dirty = ('<p onclick="steal()">Hello</p><script>alert(1)</script>'
                 '<p><a href="javascript:alert(2)">click</a><a href="https://ok.example/">fine</a></p>'
                 '<iframe src="https://evil.example/"></iframe><img src=x onerror="x()">')
        cleaned = fetcher.clean_episode_html(dirty)
        for forbidden in ("<script", "onclick", "javascript:", "<iframe", "<img", "onerror"):
            self.assertNotIn(forbidden, cleaned.lower())
        self.assertIn('href="https://ok.example/"', cleaned)
        self.assertIn("Hello", cleaned)


class NoStrayBytes(unittest.TestCase):
    """A CSS escape written inside a Python string is not an escape (a NUL byte shipped once)."""

    def test_the_stylesheet_carries_no_control_characters(self):
        control = [hex(ord(ch)) for ch in build_site.CSS if ord(ch) < 9 or ord(ch) == 0]
        self.assertEqual(control, [], f"control characters in the stylesheet: {control}")
        self.assertNotIn("\ufffd", build_site.CSS)

    def test_the_kicker_separator_is_the_character_itself(self):
        self.assertIn('content:"\u00b7"', build_site.CSS)

    def test_a_rendered_page_carries_no_nul_and_no_replacement_character(self):
        item = dict(StoryPageFurniture.ITEM)
        page = build_site.build_story(item, [], build_site.datetime.now(build_site.timezone.utc), {})
        self.assertNotIn("\x00", page)
        self.assertNotIn("\ufffd", page)
        self.assertIn("\u00b7", build_site.CSS)


class ListeningShows(PodcastSite):
    """The header's drawer on the listening page has to open and hold every show we watch."""

    def test_the_page_carries_a_drawer_and_a_button_that_controls_it(self):
        page = (self.site / "listen.html").read_text(encoding="utf-8")
        self.assertIn('id="net-toggle"', page)
        self.assertIn('aria-controls="net-drawer"', page)
        self.assertIn('id="net-drawer"', page)
        self.assertIn('id="net-backdrop"', page)
        self.assertIn('aria-label="the shows we watch"', page)

    def test_the_drawer_lists_the_show_with_what_it_contributed(self):
        page = (self.site / "listen.html").read_text(encoding="utf-8")
        drawer = page.split('id="net-drawer"')[1]
        self.assertIn("A Show", drawer)
        self.assertIn('<span class="num">1</span>', drawer)

    def test_every_episode_row_names_a_show_the_drawer_can_filter_by(self):
        page = (self.site / "listen.html").read_text(encoding="utf-8")
        rows = re.findall(r'<article class="listen-item" data-src="([^"]+)"', page)
        net = re.findall(r'<button type="button" class="net-item" data-src="([^"]+)"', page)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows), set(net))

    def test_the_script_that_opens_it_is_on_the_page(self):
        page = (self.site / "listen.html").read_text(encoding="utf-8")
        self.assertIn("getElementById('net-drawer')", page)
        self.assertIn("classList.toggle('is-open'", page)
        self.assertIn(".listen-item", page)

    def test_the_header_counts_shows_and_episodes_not_breaking_news(self):
        page = (self.site / "listen.html").read_text(encoding="utf-8")
        # the header slice, not everything before </header> - the stylesheet names a .tag.breaking
        header = page.split('<header class="top">')[1].split("</header>")[0]
        self.assertIn("1 shows", header)
        self.assertIn("<b>1</b> episodes", header)
        self.assertIn("the wire", header)
        self.assertNotIn("breaking", header)
        self.assertNotIn("anomalies", header)


class ChaptersPlay(unittest.TestCase):
    """A timestamp at the start of a block is navigation, so it should move the player."""

    def test_timestamps_are_read_in_every_shape_feeds_use(self):
        self.assertEqual(build_site.timestamp_seconds("(09:54)"), 594)
        self.assertEqual(build_site.timestamp_seconds("1:02:03"), 3723)
        self.assertEqual(build_site.timestamp_seconds("9:54"), 594)
        self.assertEqual(build_site.timestamp_seconds("0:00"), 0)
        self.assertEqual(build_site.timestamp_seconds("99:99"), -1)
        self.assertEqual(build_site.timestamp_seconds("part one"), -1)

    def test_a_chapter_becomes_a_button_that_carries_its_second(self):
        html = build_site.chapterise("<p>(09:54) Weekly episode preview</p>")
        self.assertIn('<button type="button" class="chapter" data-at="594">(09:54)</button>', html)
        self.assertIn("Weekly episode preview", html)

    def test_a_timestamp_inside_a_list_item_or_a_span_still_works(self):
        html = build_site.chapterise("<ul><li><span>(26:25)</span> Sponsor: Tasklet</li></ul>")
        self.assertIn('data-at="1585"', html)
        self.assertIn("Sponsor: Tasklet", html)

    def test_a_time_in_the_middle_of_a_sentence_is_left_alone(self):
        html = "<p>Recursive self improvement (Part 1) and it ran 09:54 long</p>"
        self.assertEqual(build_site.chapterise(html), html)

    def test_the_page_carries_the_script_that_seeks_the_player(self):
        item = dict(StoryPageFurniture.ITEM)
        page = build_site.build_story(item, [], build_site.datetime.now(build_site.timezone.utc), {})
        self.assertIn("button.chapter[data-at]", page)
        self.assertIn("player.currentTime = at", page)
        self.assertIn("player.play()", page)
        self.assertIn("audio.player", page)

    def test_a_story_page_carries_no_player_script(self):
        item = dict(StoryPageFurniture.ITEM, kind="news", audio="", duration=0)
        page = build_site.build_story(item, [], build_site.datetime.now(build_site.timezone.utc), {})
        self.assertNotIn("player.currentTime", page)

    def test_an_episode_without_its_own_art_borrows_the_shows(self):
        artwork = "https://cdn.example.com/show.jpg"
        raw = {"title": "An episode", "link": "https://example.com/e1", "summary": "A description.",
               "summaryHtml": "<p>A description.</p>", "image": artwork, "showImage": artwork,
               "audio": "https://cdn.example.com/e1.mp3", "audioType": "audio/mpeg", "duration": 600,
               "transcript": "", "transcriptType": ""}
        fields = fetcher.episode_fields({"kind": "podcast"}, raw)
        self.assertEqual(fields["kind"], "podcast")
        self.assertEqual(fields["duration"], 600)


class PlayerFeedback(unittest.TestCase):
    """The gap between pressing a chapter and hearing anything has to be visible."""

    ITEM = dict(StoryPageFurniture.ITEM)

    def page(self):
        return build_site.build_story(self.ITEM, [], build_site.datetime.now(build_site.timezone.utc), {})

    def test_the_player_carries_a_line_it_can_talk_through(self):
        page = self.page()
        self.assertIn('id="player-state"', page)
        self.assertIn('aria-live="polite"', page)
        self.assertIn('class="player-wrap" id="player-wrap"', page)

    def test_pressing_a_chapter_says_it_is_loading_and_says_from_where(self):
        page = self.page()
        self.assertIn("loading from ", page)
        self.assertIn("still loading", page)          # after 15s, name the reason
        self.assertIn("player.currentTime = at", page)

    def test_buffering_and_failure_each_say_their_own_thing(self):
        page = self.page()
        self.assertIn("addEventListener('waiting'", page)
        self.assertIn("addEventListener('error'", page)
        self.assertIn("would not start the file", page)

    def test_the_waiting_state_is_cleared_when_sound_starts(self):
        page = self.page()
        playing = page.split("addEventListener('playing'")[1].split("});")[0]
        self.assertIn("say('')", playing)

    def test_there_is_something_moving_while_it_waits(self):
        self.assertIn(".player-wrap.is-loading::after", build_site.CSS)
        self.assertIn("@keyframes player-scan", build_site.CSS)
        self.assertIn(".player-wrap.is-loading audio.player { opacity", build_site.CSS)
        # and it stops for a reader who asked motion to stop
        self.assertIn("@media (prefers-reduced-motion: reduce)", build_site.CSS)

    def test_a_story_page_gets_none_of_it(self):
        item = dict(self.ITEM, kind="news", audio="", duration=0)
        page = build_site.build_story(item, [], build_site.datetime.now(build_site.timezone.utc), {})
        self.assertNotIn('id="player-state"', page)


class ListeningCap(unittest.TestCase):
    """One daily show must not own the listening page."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        (root / "data").mkdir()
        template = {
            "url": "https://example.com/e", "source": "show", "sourceName": "Daily Show",
            "sourceUrl": "https://example.com/feed", "sourceSite": "", "sourceCategory": "podcast",
            "tier": 2, "summary": "A description.", "content": "A description.",
            "contentFrom": "feed", "image": "", "imageLocal": "", "imageAlt": "", "imageFrom": "",
            "author": "A", "score": 10.0, "breaking": False, "upsy": False, "upsyWhy": "",
            "upsyTerms": [], "aiTerms": [], "siTerms": [], "promo": False, "question": False,
            "kind": "podcast", "audio": "https://cdn.example.com/e.mp3", "audioType": "audio/mpeg",
            "duration": 600, "transcript": "", "transcriptType": "", "contentHtml": "",
        }
        items = []
        for n in range(15):
            items.append({**template, "id": f"daily{n}", "title": f"Daily episode {n}",
                          "published": f"2026-10-{15 - n:02d}T10:00:00+00:00"})
        for n in range(3):
            items.append({**template, "id": f"other{n}", "title": f"Other episode {n}",
                          "source": "other", "sourceName": "Other Show",
                          "published": f"2026-10-{14 - n:02d}T10:00:00+00:00"})
        news = {"generated": "2026-10-16T00:00:00+00:00",
                "counts": {"items": len(items), "sources": 2, "breaking": 0, "upsy": 0, "withImage": 0,
                           "withFullText": 0, "new": len(items), "promo": 0}, "items": items}
        meta = {"generated": news["generated"], "counts": news["counts"], "health": []}
        (root / "data" / "news.json").write_text(json.dumps(news), encoding="utf-8")
        (root / "data" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (root / "sources.json").write_text((ROOT / "sources.json").read_text(encoding="utf-8"),
                                           encoding="utf-8")
        cls._site, cls._data, cls._root = build_site.SITE, build_site.DATA, build_site.ROOT
        build_site.SITE, build_site.DATA, build_site.ROOT = root / "_site", root / "data", root
        build_site.CACHE_IMAGES = False
        build_site.main()
        cls.site = root / "_site"

    @classmethod
    def tearDownClass(cls):
        build_site.SITE, build_site.DATA, build_site.ROOT = cls._site, cls._data, cls._root
        cls.tmp.cleanup()

    def test_one_show_is_capped_and_the_rest_appear(self):
        page = (self.site / "listen.html").read_text(encoding="utf-8")
        self.assertEqual(page.count("Daily episode"), 12)
        self.assertEqual(page.count("Other episode"), 3)
        self.assertIn("Showing the newest 12 of each show", page)
        self.assertIn("Daily Show (3)", page)

    def test_a_capped_episode_still_has_its_own_page(self):
        self.assertTrue((self.site / "s" / "daily14" / "index.html").exists())

    def test_every_episode_is_still_in_the_data_file(self):
        payload = json.loads((self.site / "data" / "episodes.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["items"]), 18)


class NoSubscriptions(unittest.TestCase):
    """An article behind a wall is not an article."""

    def test_a_short_page_that_names_a_subscription_is_walled(self):
        page = ("<nav><a href=/subscribe>Subscribe</a></nav><h1>Title</h1>"
                "<p>This article is for subscribers only.</p><p>Two lines of teaser.</p>")
        self.assertIn("says it is for subscribers", fetcher.paywall_reasons(page))

    def test_a_long_article_is_not_walled_by_its_own_marketing(self):
        # The Verge runs Zephr and declares isAccessibleForFree false, then hands over the whole piece.
        page = ('<script>{"isAccessibleForFree": false}</script><nav>Subscribe now</nav><p>'
                + "word " * 1200 + "</p>")
        self.assertEqual(fetcher.paywall_reasons(page), [])

    def test_a_masthead_subscribe_link_means_nothing(self):
        page = "<nav><a href=/subscribe>Subscribe</a><a href=/login>Sign In</a></nav><p>Body.</p>"
        self.assertEqual(fetcher.paywall_reasons(page), [])

    def test_the_schema_flag_alone_does_not_condemn_a_page(self):
        self.assertEqual(fetcher.paywall_reasons(
            '<meta property="article:content_tier" content="metered">' + "<p>" + "word " * 900 + "</p>"), [])

    def test_a_paid_substack_teaser_in_a_feed_is_walled(self):
        feed_text = "The first two paragraphs. This post is for paid subscribers."
        self.assertIn("says it is for paid subscribers", fetcher.paywall_reasons(feed_text))

    def test_an_ordinary_summary_is_left_alone(self):
        self.assertEqual(fetcher.paywall_reasons("A paper on model routing, 12 pages."), [])


class EpisodeAdmission(unittest.TestCase):
    """A general show, and an episode nobody would press play on."""

    def test_a_general_show_admits_only_episodes_that_name_it_in_the_headline(self):
        source = {"require_title": ["ai", "agent*"]}
        self.assertTrue(fetcher.title_hits(source, {"title": "Why AI is eating software"}))
        self.assertTrue(fetcher.title_hits(source, {"title": "How agents break software"}))
        self.assertFalse(fetcher.title_hits(
            source, {"title": "A pottery masterclass", "summary": "we talk about AI for a minute"}))

    def test_a_source_without_a_gate_keeps_everything(self):
        self.assertEqual(fetcher.title_hits({}, {"title": "anything at all"}), [])

    def test_the_gate_reads_words_not_letters(self):
        source = {"require_title": ["ai"]}
        self.assertFalse(fetcher.title_hits(source, {"title": "Against the again-and-again economy"}))

    def test_an_episode_with_nothing_to_read_is_refused(self):
        thin = {"kind": "podcast", "summary": "Ten minutes on models.", "transcript": ""}
        self.assertTrue(fetcher.episode_is_thin({"kind": "podcast"}, thin))

    def test_a_description_of_substance_is_admitted(self):
        full = {"kind": "podcast", "summary": "x" * 400, "transcript": ""}
        self.assertFalse(fetcher.episode_is_thin({"kind": "podcast"}, full))

    def test_a_transcript_excuses_a_short_description(self):
        episode = {"kind": "podcast", "summary": "", "transcript": "https://example.com/e.vtt"}
        self.assertFalse(fetcher.episode_is_thin({"kind": "podcast"}, episode))

    def test_a_show_may_lower_the_bar_for_itself(self):
        episode = {"kind": "podcast", "summary": "x" * 90, "transcript": ""}
        self.assertTrue(fetcher.episode_is_thin({"kind": "podcast"}, episode))
        self.assertFalse(fetcher.episode_is_thin({"kind": "podcast", "minDescriptionChars": 60}, episode))

    def test_a_news_item_is_never_thin(self):
        self.assertFalse(fetcher.episode_is_thin({"kind": "news"}, {"kind": "news", "summary": ""}))

    def test_the_title_gate_names_the_subject_and_its_synonyms(self):
        terms = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
        gate = next(source for source in terms if source["id"] == "decoder")["require_title"]
        for title in ("Sam Altman on superintelligence", "How agentic workflows break",
                      "Llama 4 and open weights", "Benchmarking the new frontier model",
                      "Attention is all you need: the paper", "A survey of model training",
                      "Deep learning for weather read the arxiv preprint"):
            self.assertTrue(fetcher.title_hits({"require_title": gate}, {"title": title}), title)
        for title in ("A pottery masterclass", "Why inflation is sticky", "The again and again economy"):
            self.assertFalse(fetcher.title_hits({"require_title": gate}, {"title": title}), title)

    def test_dwarkesh_is_back_and_the_exception_is_written_down(self):
        sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
        dwarkesh = next(source for source in sources if source["id"] == "dwarkesh")
        self.assertEqual(dwarkesh["minDescriptionChars"], 80)
        self.assertIn("exception", dwarkesh["note"])
        short = {"kind": "podcast", "summary": "x" * 90, "transcript": ""}
        self.assertFalse(fetcher.episode_is_thin(dwarkesh, short))
        # and another show with the same feed is still refused
        self.assertTrue(fetcher.episode_is_thin({"kind": "podcast"}, short))

    def test_the_general_shows_are_title_gated_and_only_two_are_throttled(self):
        sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
        gated = {s["id"] for s in sources if s.get("require_title")}
        self.assertTrue({"lexfridman", "decoder", "ezra-klein", "allin", "oddlots",
                         "techmeme-ridehome", "twentyvc"} <= gated)
        throttled = {s["id"]: s["pollEveryHours"] for s in sources if s.get("pollEveryHours")}
        for source_id, hours in throttled.items():
            self.assertGreaterEqual(hours, 1, source_id)
        # a title-gated show still needs a description on its episodes, like everything else
        for source in sources:
            if source.get("require_title"):
                self.assertIsNone(source.get("minDescriptionChars"),
                                  f"{source['id']} lowered the description bar")


class EpisodePageExtras(StoryPageFurniture):
    """Chapters and a transcript read off the show's own page."""

    PAGE = (
        "<html><body><h2>Timestamps</h2>"
        "<p>(00:00:00) – Intro</p><p>(00:00:47) – How Intel brings up a new process node</p>"
        "<p>(00:03:22) – What Intel learned from 10nm</p><p>(00:13:13) – Why High-NA EUV pays off</p>"
        "<h2>Sponsors</h2><p>(00:20:00) – A sponsor read that is not a chapter</p>"
        "<h2>Transcript</h2>"
        + "".join(f"<p>Speaker {i}</p><p>This is a long spoken line number {i} about semiconductors.</p>"
                  for i in range(40))
        + "<h2>Share</h2><p>Share this post</p></body></html>"
    )

    def test_chapters_are_read_with_their_seconds(self):
        chapters = fetcher.chapters_from_page(self.PAGE)
        self.assertEqual(len(chapters), 4)
        self.assertEqual(chapters[0], {"at": 0, "label": "Intro"})
        self.assertEqual(chapters[1]["at"], 47)
        self.assertEqual(chapters[3]["at"], 793)
        # the list stops at the next heading, so an advertised read is not a chapter
        self.assertNotIn("A sponsor read that is not a chapter", [c["label"] for c in chapters])

    def test_a_single_timestamp_is_not_a_chapter_list(self):
        self.assertEqual(fetcher.chapters_from_page("<p>00:12:34 the middle of a sentence</p>"), [])

    def test_the_transcript_is_read_and_stops_at_the_next_section(self):
        text = fetcher.transcript_from_page(self.PAGE)
        self.assertIn("long spoken line number 0", text)
        self.assertIn("long spoken line number 39", text)
        self.assertNotIn("Share this post", text)

    def test_a_page_with_no_transcript_yields_none(self):
        self.assertEqual(fetcher.transcript_from_page("<p>Notes and a player.</p>"), "")

    def test_an_audio_url_is_not_an_episode_page(self):
        self.assertEqual(fetcher.episode_extras("https://cdn.example.com/e.mp3", {}, [3]), {})

    def test_the_episode_artwork_is_a_thumbnail_not_a_banner(self):
        self.assertIn(".story.episode img.lead", build_site.CSS)
        self.assertIn("max-width:300px", build_site.CSS)

    def test_an_episode_page_shows_chapters_and_the_folded_transcript(self):
        page = self.page(contentHtml="<p>A description.</p>",
                         chapters=[{"at": 47, "label": "How Intel brings up a node"}],
                         transcriptText="One line.\n\nAnother line.")
        self.assertIn('class="chapter" data-at="47"', page)
        self.assertIn("How Intel brings up a node", page)
        self.assertIn('<details class="transcript">', page)
        self.assertIn("Read the transcript", page)
        self.assertIn("<p>Another line.</p>", page)

    def test_an_episode_without_extras_has_no_empty_sections(self):
        page = self.page(contentHtml="<p>A description.</p>")
        self.assertNotIn('<details class="transcript">', page)
        self.assertNotIn('class="chapters"', page)


class EntitiesInText(unittest.TestCase):
    """A feed that escapes its own escaping must not put &#8217; in a headline."""

    def test_double_escaped_entities_are_undone(self):
        self.assertEqual(fetcher.deep_unescape("Intel&amp;#8217;s most advanced fab"),
                         "Intel\u2019s most advanced fab")

    def test_a_single_escape_is_undone(self):
        self.assertEqual(fetcher.deep_unescape("Intel&#8217;s fab"), "Intel\u2019s fab")
        self.assertEqual(fetcher.deep_unescape("A &amp; B"), "A & B")

    def test_a_plain_title_is_left_alone(self):
        self.assertEqual(fetcher.deep_unescape("A plain title"), "A plain title")
        self.assertEqual(fetcher.deep_unescape(""), "")


class CacheDoesNotOutliveItsVerdict(unittest.TestCase):
    """A page cached before a rule existed has still never been judged by it."""

    def test_an_unjudged_cache_entry_is_read_again(self):
        source = pathlib.Path("tools/fetch.py").read_text(encoding="utf-8")
        # the cache is only trusted when it carries the wall verdict
        self.assertIn('existing.get("paywallChecked")', source)
        self.assertIn('found["paywallChecked"] = True', source)

    def test_an_episode_page_with_nothing_on_it_is_settled_for_a_week(self):
        source = pathlib.Path("tools/fetch.py").read_text(encoding="utf-8")
        self.assertIn('existing.get("extrasChecked")', source)
        self.assertIn('found["extrasChecked"]', source)

    def test_a_revived_item_is_cleaned_like_a_fresh_one(self):
        revived = fetcher._revive([{"title": "MOFCOM&amp;#x27;s New Rules", "summary": "A &amp; B",
                                    "published": "2026-10-25T10:00:00+00:00"}])
        self.assertEqual(revived[0]["title"], "MOFCOM's New Rules")
        self.assertEqual(revived[0]["summary"], "A & B")


class WalledArticlesAreDropped(unittest.TestCase):
    """A paywall can be markup, not just copy: the body in a hidden container settles it."""

    HIDDEN = ('<div class="paywall-preview"><p>First paragraph of the teaser.</p></div>'
              '<div class="paywall-hidden-content"><p>' + "Hidden body word " * 300 + '</p></div>'
              '<section class="paywall-content">Subscribe to The Decoder</section>'
              '<script>{"isAccessibleForFree": "False", "hasPart": {"@type": "WebPageElement", '
              '"isAccessibleForFree": "False", "cssSelector": ".paywall-active"}}</script>')

    def test_the_decoder_shape_is_a_wall(self):
        reasons = fetcher.paywall_reasons(self.HIDDEN)
        self.assertIn("the body is in a hidden container", reasons)
        self.assertIn("the article is marked paywall-active", reasons)
        self.assertIn("schema splits the article into a free part and a walled part", reasons)

    def test_hidden_words_do_not_count_as_readable(self):
        # the page holds 300 words; the reader sees one paragraph
        self.assertLess(fetcher.visible_words(self.HIDDEN), 60)
        self.assertGreater(fetcher.page_words(self.HIDDEN), 250)

    def test_a_free_article_from_the_same_site_is_kept(self):
        page = ('<div class="content-wrapper"><p>' + "A publishable paragraph. " * 60 + "</p></div>"
                '<meta property="article:content_tier" content="free">')
        self.assertEqual(fetcher.paywall_reasons(page), [])

    def test_the_decoder_is_disabled_in_the_catalogue(self):
        sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
        decoder = next(s for s in sources if s["id"] == "the-decoder")
        self.assertFalse(decoder.get("enabled", True))
        self.assertIn("paywall-hidden-content", decoder["note"])

    def test_every_line_of_an_agenda_becomes_a_chapter(self):
        agenda = ("<p><strong>AGENDA:</strong></p><p>05:00 Will we run out of energy?"
                  "<br> 19:00 Will specialised chips threaten NVIDIA's dominance?"
                  "<br> 26:00 Can we trust AI agents more than humans?"
                  "<br> 59:00 Why are coding agents the most overrated AI investment?</p>")
        buttons = re.findall(r'data-at="(\d+)">([^<]+)<', build_site.chapterise(agenda))
        self.assertEqual([int(at) for at, _ in buttons], [300, 1140, 1560, 3540])
        # and nothing is doubled: the first line is one entry, not one as a block start and one after a break
        self.assertEqual(len(buttons), 4)

    def test_a_time_inside_a_sentence_is_still_a_sentence(self):
        page = build_site.chapterise("<p>Then at 19:00 we will see what happens next.</p>")
        self.assertNotIn('class="chapter"', page)
        self.assertIn("at 19:00 we will see", page)

    def test_the_agenda_heading_survives(self):
        page = build_site.chapterise("<p><strong>AGENDA:</strong></p><p>05:00 Opening<br> 19:00 Second</p>")
        self.assertIn("<strong>AGENDA:</strong>", page)
