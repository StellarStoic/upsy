# upsy.si

A wire that watches superintelligence happen: breaking news in AI, and the "upsy moments" where a
machine does something wild enough to deserve a second look.

Static site, rebuilt by a GitHub Actions workflow every ten minutes. No server, no database, no
trackers, no third-party scripts. Standard-library Python does the work, so nothing needs installing.

## How it works

1. `tools/fetch.py` reads every feed in `sources.json` directly, parses RSS, Atom and RDF, keeps each
   story's image and (where the feed offers it) the full text, and writes `data/news.json`.
2. `tools/moments.py` asks a model to pick the upsy moments and write a two-sentence brief, and writes
   `data/moments.json`. No credential, a rate limit or a broken reply is not a failure: it falls back
   to the ranked candidates so the site always builds.
3. `tools/build_site.py` renders `_site/`: the console, a page per story, the feed, the sitemap and the
   sources table.
4. The workflow uploads `_site/` to GitHub Pages.

## Ranking

Energy is freshness plus source tier plus how squarely a story sits on superintelligence, with a few
corrective rules learned from watching real feeds:

- Terms match on **word boundaries**, so `AGI` cannot score inside "again" or "magic".
- Generic terms (`AI`, `ML`) are shown as topics but never scored, or every item becomes a hit.
- Aggregator links (Google News, Hacker News) are **demoted**: they point at reporting rather than doing
  it, and they repeat one story across many outlets.
- Promotions ("48 hours to Disrupt — register now") are demoted hard.
- Subreddit questions ("Serious Question: …") are demoted and kept out of the ticker.
- A story with an image gets a small bonus, because a story with a picture is worth more on a page.
- Breaking is capped at six, one per source: a ticker with twenty-two entries is not a ticker.
- No single source may take more than three of the top forty slots.

## Adding a source

Add an entry to `sources.json` and commit. Nothing else is needed:

```json
{
  "id": "example",
  "name": "Example AI",
  "url": "https://example.com/feed.xml",
  "site": "https://example.com/",
  "category": "lab",
  "tier": 1,
  "enabled": true
}
```

`id` must be unique. `category` is one of `lab`, `research`, `press`, `commentary`, `community`, `paper`, `aggregator`.
`tier` is 1 for first-party sources, 2 for press, 3 for community and aggregators. Sources that do not
work are kept in the file with `"enabled": false` and a `note` explaining why, so nobody re-adds them
blind. `tools/fetch.py` never scrapes a page that does not offer a feed.

## Credentials

None are required. The site builds, ranks and publishes without any.

If you want the upsy moments written by a model, the workflow already has everything it needs: it
passes `GITHUB_TOKEN` with `permissions: models: read`, so GitHub Models does the writing with no
secret to manage. To use something else instead, set either:

- `UPSY_LLM_API_KEY` — used against GitHub Models by default, or
- `UPSY_LLM_BASE_URL` + `UPSY_LLM_MODEL` — any OpenAI-compatible endpoint.

## Running it locally

```bash
python3 tools/fetch.py            # feed only, a few seconds
python3 tools/fetch.py --articles # also fetch pages to fill in images and summaries
python3 tools/moments.py          # written by a model, or by the rules
python3 tools/build_site.py       # writes _site/
python3 -m http.server -d _site 8000
python3 -m unittest discover -s tests -v
```

`fetch.py` treats the environment's proxy variables as noise unless `UPSY_USE_PROXY=1` is set: the
Action fetches direct, and a site that behaves differently locally than in CI is a trap.

## Etiquette and ownership

Headlines, images and article text belong to the outlets that published them. upsy.si links to the
original on every story and on every story page, marks where the text came from a feed, and never
republishes an article that the publisher did not put in its own feed. Feeds are fetched with a
descriptive user agent, honour ETag and Last-Modified, and are read no more often than the workflow's
ten-minute cycle.
