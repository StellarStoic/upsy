#!/usr/bin/env python3
"""Build upsy.si from data/news.json into _site/.

Design: the "signal" stance - a monitoring console. Near-black ground, monospace throughout, an
energy figure and meter per story, breaking and anomalies as flags, the source network on the right.
One page per story exists for sharing and for search engines, and every story links back to both its
original and its own page, so no page is orphaned.

Standard library only. Run tools/fetch.py first; this never touches the network.

    python3 tools/build_site.py            # writes _site/
"""
from __future__ import annotations

import html
import json
import pathlib
import re
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "_site"
DATA = ROOT / "data"
SITE_URL = "https://upsy.si"
SITE_NAME = "upsy.si"
TAGLINE = "watching superintelligence happen"
INDEX_ROWS = 120
FEED_ITEMS = 60


CACHE_IMAGES = True          # the test suite turns this off: it must never touch the network
IMAGE_CACHE = ROOT / "data" / "images"
MAX_IMAGE_BYTES = 1_500_000


def direct_network() -> None:
    import os
    if os.environ.get("UPSY_USE_PROXY") == "1":
        return
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(key, None)


def image_suffix(data: bytes) -> str:
    """The extension this really is, or "" if these bytes are not an image at all.

    Plenty of CDNs answer a request from a datacenter with 200 and an HTML challenge page, and writing
    that into img/ as ".jpg" produced exactly what you would expect: broken thumbnails on the page.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp":
        return ".avif"
    if data[:2] == b"BM":
        return ".bmp"
    return ""


def cache_images(items: list[dict], limit: int = 200) -> int:
    """Keep a copy of each story's picture inside the site.

    Hotlinking a publisher's CDN is both fragile - plenty of them refuse a request with a Referer
    from somewhere else, which shows the reader a broken thumbnail - and impolite, since the page
    would be spending someone else's bandwidth. So each picture is fetched once, kept under
    data/images/ (cached between builds in CI) and served from /img/ with the source credited.
    """
    if not CACHE_IMAGES:
        return 0
    import concurrent.futures as futures
    import hashlib
    import shutil
    import urllib.request

    direct_network()
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    (SITE / "img").mkdir(exist_ok=True)
    wanted = [i for i in items if (i.get("image") or "").startswith("http")][:limit]

    def one(item: dict) -> str:
        url = item["image"]
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        stored = next((p for p in IMAGE_CACHE.glob(digest + ".*")), None)
        if stored is not None and not image_suffix(stored.read_bytes()[:16]):
            # A cache written before the validation existed, or a truncated download: drop it and
            # fetch again rather than serve a broken picture for as long as the cache survives.
            stored.unlink(missing_ok=True)
            stored = None
        if stored is None:
            try:
                request = urllib.request.Request(url, headers={
                    "User-Agent": "upsy.si/1.0 (+https://upsy.si/)", "Referer": "https://upsy.si/"})
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = response.read(MAX_IMAGE_BYTES + 1)
                expected = response.headers.get("Content-Length")
            except Exception:
                return ""
            if not data or len(data) > MAX_IMAGE_BYTES:
                return ""
            # A truncated download starts with the right magic bytes and still renders as a broken
            # image, so a declared length that does not match is a failed download, not a picture.
            if expected and expected.isdigit() and int(expected) != len(data):
                return ""
            suffix = image_suffix(data)
            if not suffix:
                return ""       # a challenge page or an error page, not a picture
            stored = IMAGE_CACHE / (digest + suffix)
            stored.write_bytes(data)
        shutil.copy2(stored, SITE / "img" / stored.name)
        return f"/img/{stored.name}"

    cached = 0
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, local in zip(wanted, pool.map(one, wanted)):
            if local:
                item["imageLocal"] = local
                cached += 1
    return cached


def shot_note() -> str:
    """What the screenshot run did, readable from the published page.

    A step under continue-on-error that quietly does nothing is indistinguishable from a step that was
    never added, and the workflow logs are not reachable from outside, so the page carries the outcome.
    """
    path = IMAGE_CACHE.parent / "shots.json"
    if not path.exists():
        return "not run"
    try:
        status = json.loads(path.read_text(encoding="utf-8")).get(".status") or {}
    except (json.JSONDecodeError, OSError):
        return "unreadable"
    if not status:
        return "not run"
    browser = status.get("browser") or "none"
    note = f" ({status['note']})" if status.get("note") else ""
    return (f"{status.get('taken', 0)} shot, {status.get('refused', 0)} refused, "
            f"{status.get('wanted', 0)} wanted, browser {browser.split('/')[-1] or 'none'}{note}")


def attach_shots(items: list[dict], limit: int, site: pathlib.Path, shots: pathlib.Path) -> int:
    """Give a story whose feed carried no picture the screenshot of its own page, if one was taken.

    tools/screenshots.py names every shot after a digest of the story url, so nothing has to be kept
    in sync between the two scripts: a shot that exists is used, and a story that already has a
    picture is left alone. Anything that is not really a picture - a challenge page saved under a
    .png name - is refused by the same magic-byte check the downloads go through.
    """
    import hashlib
    import shutil

    if not shots.exists():
        return 0
    (site / "img").mkdir(exist_ok=True)
    attached = 0
    for item in items[:limit]:
        if item.get("imageLocal") or (item.get("image") or "").startswith("http"):
            continue
        url = item.get("url") or ""
        if not url.startswith("http"):
            continue
        stem = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        found = next(iter(shots.glob(stem + ".*")), None)
        if found is None or not image_suffix(found.read_bytes()[:16]):
            continue
        shutil.copy2(found, site / "img" / found.name)
        item["imageLocal"] = f"/img/{found.name}"
        attached += 1
    return attached


def editor_note(moments: dict) -> str:
    """Who wrote the rail, readable from the published page without any log access."""
    engine = moments.get("engine") or "rules"
    error = moments.get("editorError")
    return f"{engine}" + (f" (unavailable: {error})" if error else "")


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def age(stamp: str | None, now: datetime) -> str:
    if not stamp:
        return "-"
    try:
        when = datetime.fromisoformat(stamp)
    except Exception:
        return "-"
    minutes = max(0.0, (now - when).total_seconds() / 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{int(minutes)}m"
    if minutes < 60 * 36:
        return f"{int(minutes / 60)}h"
    return f"{int(minutes / 1440)}d"


def iso(stamp: str | None) -> str:
    return stamp or ""


def truncate(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "\u2026"


CSS = """
  :root {
    --bg:#08090c; --panel:#0d1014; --line:#1d222a; --fg:#e3e9f2; --dim:#7b8695; --dim2:#525c69;
    --hot:#ff5f4a; --cool:#4ad4ff; --upsy:#b6ff5f; --link:#8fd0ff;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 var(--mono); }
  a { color:inherit; text-decoration:none; }
  a:hover { color:var(--link); }
  a:focus-visible, button:focus-visible, input:focus-visible { outline:2px solid var(--cool); outline-offset:2px; }
  .skip { position:absolute; left:-9999px; }
  .skip:focus { left:8px; top:8px; background:var(--panel); padding:8px 12px; z-index:9; }
  header.top { display:flex; align-items:center; gap:14px; padding:12px 18px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .brand { font-size:21px; letter-spacing:.3em; text-transform:uppercase; }
  .brand b { color:var(--cool); font-weight:400; }
  .status { margin-left:auto; display:flex; gap:16px; align-items:center; font-size:11px; color:var(--dim); flex-wrap:wrap; }
  .status b { font-weight:400; color:var(--upsy); }
  .status .hot { color:var(--hot); }
  .dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--hot); margin-right:6px; animation:pulse 1.8s ease-in-out infinite; vertical-align:1px; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.2; } }
  .bar { display:flex; gap:10px; padding:9px 18px; border-bottom:1px solid var(--line); font-size:11.5px; color:var(--dim); flex-wrap:wrap; align-items:center; }
  .bar button { font:inherit; background:none; border:1px solid var(--line); color:var(--dim); padding:3px 9px; cursor:pointer; }
  .bar button[aria-pressed="true"] { border-color:var(--cool); color:var(--cool); }
  .bar input { font:inherit; background:var(--panel); border:1px solid var(--line); color:var(--fg); padding:4px 8px; min-width:190px; }
  .bar .spacer { margin-left:auto; }
  main { display:grid; grid-template-columns:minmax(0,1fr) 320px; align-items:start; }
  .rows { border-right:1px solid var(--line); min-width:0; }
  .row { display:grid; grid-template-columns:70px minmax(0,1fr) 112px; gap:14px; padding:12px 18px; border-bottom:1px solid var(--line); }
  .row:hover { background:var(--panel); }
  .net-item { display:flex; width:100%; justify-content:space-between; gap:8px; align-items:baseline;
    background:none; border:0; border-left:2px solid transparent; padding:5px 8px; cursor:pointer;
    font:inherit; color:inherit; text-align:left; }
  .net-item:hover { background:var(--panel); }
  .net-item .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .net-item.is-on { background:var(--panel); border-left-color:var(--upsy); color:var(--upsy); font-weight:600; }
  /* Appears once the reader is a few articles down. Hidden by display rather than [hidden]:
     an author rule outranks the browser's own, which is how a filter here once changed nothing. */
  #to-top { display:none; position:fixed; right:18px; bottom:18px; z-index:40; gap:5px;
    align-items:center; padding:9px 13px; border:1px solid var(--line); border-radius:999px;
    background:var(--panel); color:var(--ink); font:inherit; font-size:.82rem; cursor:pointer;
    box-shadow:0 6px 18px rgba(0,0,0,.45); }
  #to-top.is-on { display:flex; }
  #to-top:hover { border-color:var(--hot); color:var(--hot); }
  #to-top span { font-size:.72rem; letter-spacing:.04em; text-transform:uppercase; }
  /* Sits 700px into the document: once it leaves the viewport the reader is into the stories. */
  #top-mark { position:absolute; top:700px; left:0; width:1px; height:1px; }
  .net-all { margin:8px 0 0; font-size:.78rem; opacity:.7; }
  /* The source network folded behind the header icon. Same list, same filter, out of the stream's
     way - and one drawer serves both: full width on a phone, a slide-over on a desk, so there is
     no second implementation to drift from this one. */
  .net-toggle { display:inline-flex; align-items:center; gap:6px; background:none; border:1px solid var(--line);
    color:var(--dim); padding:4px 7px; cursor:pointer; font:inherit; }
  .net-toggle svg { width:17px; height:17px; display:block; }
  .net-toggle:hover { border-color:var(--cool); color:var(--cool); }
  .net-toggle[aria-expanded="true"] { border-color:var(--cool); color:var(--cool); }
  .net-toggle.is-filtered { border-color:var(--upsy); color:var(--upsy); }
  .net-count { min-width:15px; padding:0 4px; text-align:center; border:1px solid currentColor;
    border-radius:999px; font-size:10px; line-height:1.4; }
  .net-backdrop { position:fixed; inset:0; z-index:59; background:rgba(0,0,0,.55); opacity:0;
    pointer-events:none; transition:opacity .18s ease; }
  .net-backdrop.is-open { opacity:1; pointer-events:auto; }
  .net-drawer { position:fixed; top:0; right:0; height:100%; width:min(360px,88vw); z-index:60;
    background:var(--bg); border-left:1px solid var(--line); padding:16px 16px 28px; overflow-y:auto;
    visibility:hidden; transform:translateX(101%); transition:transform .18s ease, visibility .18s; }
  .net-drawer.is-open { visibility:visible; transform:none; }
  .net-drawer h3 { font-size:10.5px; letter-spacing:.18em; text-transform:uppercase; color:var(--dim); margin:0 0 10px; }
  .net-head { display:flex; align-items:center; gap:10px; }
  .net-head h3 { margin:0; }
  .net-head button { background:none; border:1px solid var(--line); color:var(--dim); font:inherit;
    font-size:11px; padding:2px 8px; cursor:pointer; }
  .net-head button:hover { border-color:var(--cool); color:var(--cool); }
  .net-head .net-x { margin-left:auto; padding:2px 7px; }
  .net-hint { margin:10px 0 12px; font-size:.72rem; line-height:1.45; color:var(--dim2); }
  body.net-open { overflow:hidden; }
  @media (prefers-reduced-motion: reduce) { .net-drawer, .net-backdrop { transition:none; } }
  /* `.row` sets display:grid, which outranks the browser's own [hidden] { display:none } - so a
     filtered-out row stayed on screen while the property said otherwise. Both forms are declared. */
  .row[hidden], .row.is-hidden { display:none; }
  .row.is-new { box-shadow:inset 3px 0 0 var(--cool); }
  .energy { display:flex; flex-direction:column; gap:3px; position:relative; cursor:help; }
  .energy .n { font-size:17px; }
  .energy .lbl { font-size:9px; letter-spacing:.12em; color:var(--dim2); text-transform:uppercase; }
  /* The figure is a number with no unit, so it explains itself: a panel on hover, never a tab stop -
     four hundred rows of focusable tooltips would bury every link on the page. */
  .energy .tip { position:absolute; left:0; top:calc(100% + 7px); z-index:40; width:246px;
    padding:9px 11px; background:var(--panel); border:1px solid var(--line); color:var(--dim);
    font-size:10.5px; line-height:1.55; letter-spacing:0; text-transform:none; text-align:left;
    visibility:hidden; opacity:0; transition:opacity .12s ease; pointer-events:none; }
  .energy .tip b { display:block; margin-bottom:5px; color:var(--fg); font-weight:500; font-size:11px; }
  .energy .tip em { color:var(--cool); font-style:normal; }
  .energy:hover .tip { visibility:visible; opacity:1; }
  @media (prefers-reduced-motion: reduce) { .energy .tip { transition:none; } }
  @media (max-width:640px) { .energy .tip { display:none; } }
  .meter { height:3px; background:var(--line); }
  .meter i { display:block; height:100%; background:linear-gradient(90deg,var(--cool),var(--upsy)); }
  .row h2 { font-size:15.5px; line-height:1.35; font-weight:500; margin:0; }
  .src { font-size:11px; color:var(--dim); margin-bottom:4px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .src .who { color:var(--cool); }
  .tag { font-size:9.5px; letter-spacing:.1em; text-transform:uppercase; border:1px solid currentColor; padding:0 4px; }
  .tag.breaking { color:var(--hot); }
  .tag.upsy { color:var(--upsy); }
  .tag.new { color:var(--cool); }
  .tag.q { color:var(--dim2); }
  .dek { font-size:12.5px; color:var(--dim); margin-top:5px; max-width:82ch; }
  .trace { font-size:11px; color:var(--dim2); margin-top:5px; }
  .trace i { font-style:normal; border-bottom:1px dotted var(--dim2); }
  .thumb { width:112px; height:76px; object-fit:cover; border:1px solid var(--line); filter:saturate(.9); }
  .thumb.empty { background:#101317; }
  aside { padding:14px 16px; }
  aside h3 { font-size:10.5px; letter-spacing:.18em; text-transform:uppercase; color:var(--dim); margin:0 0 10px; }
  aside section + section { margin-top:20px; border-top:1px solid var(--line); padding-top:16px; }
  .anomaly { border:1px solid #26330f; background:#0f1408; padding:10px 11px; margin-bottom:9px; }
  .anomaly h4 { font-size:13.5px; font-weight:500; margin:0; color:var(--upsy); }
  .anomaly p { font-size:11.5px; color:#9fb383; margin:5px 0 0; }
  .anomaly .meta { color:#6d7d58; font-size:10.5px; margin-top:6px; }
  .brief { font-size:12.5px; color:#a9b6a0; border-left:2px solid var(--upsy); padding-left:10px; }
  .net div { display:flex; justify-content:space-between; gap:8px; font-size:11.5px; color:var(--dim); border-bottom:1px dotted var(--line); padding:3px 0; }
  .net div:hover { color:var(--fg); }
  .net .num { color:var(--dim2); }
  .more { padding:16px 18px; }
  .more button { font:inherit; background:var(--panel); border:1px solid var(--line); color:var(--fg); padding:8px 14px; cursor:pointer; }
  footer { border-top:1px solid var(--line); padding:14px 18px; font-size:11px; color:var(--dim2); display:flex; gap:18px; flex-wrap:wrap; }
  footer a:hover { color:var(--fg); }
  /* story page */
  .story { max-width:820px; margin:0 auto; padding:26px 20px 60px; }
  .story h1 { font-size:27px; line-height:1.25; font-weight:500; margin:10px 0 12px; }
  .story .kicker { font-size:11px; color:var(--dim); letter-spacing:.08em; text-transform:uppercase; display:flex; gap:12px; flex-wrap:wrap; }
  .story img.lead { width:100%; border:1px solid var(--line); margin:16px 0; }
  .story .body { font-size:15px; line-height:1.65; color:#c8d2df; }
  .story .body p { margin:0 0 14px; }
  .story .body h2, .story .body h3 { font-size:16px; margin:22px 0 8px; }
  .story .body pre { background:var(--panel); border:1px solid var(--line); padding:10px; overflow-x:auto; font-size:12.5px; }
  .story .body blockquote { border-left:2px solid var(--line); margin:0 0 14px; padding-left:12px; color:var(--dim); }
  .cta { display:inline-block; margin:20px 0; border:1px solid var(--cool); color:var(--cool); padding:9px 14px; font-size:12.5px; }
  .cta:hover { background:#0d1c22; color:var(--cool); }
  .credit { font-size:10.5px; color:var(--dim2); margin:-10px 0 18px; }
  .related { border-top:1px solid var(--line); margin-top:26px; padding-top:14px; font-size:12.5px; color:var(--dim); }
  .related a { display:block; padding:4px 0; }
  .notice { background:#0f1408; border:1px solid #26330f; color:#9fb383; padding:10px 12px; font-size:12px; }
  table.sources { width:100%; border-collapse:collapse; font-size:12.5px; }
  table.sources th { text-align:left; font-weight:500; color:var(--dim); border-bottom:1px solid var(--line); padding:6px 8px 6px 0; font-size:11px; letter-spacing:.1em; text-transform:uppercase; }
  table.sources td { border-bottom:1px solid var(--line); padding:6px 8px 6px 0; color:#b9c4d1; }
  table.sources td.name { color:var(--fg); }
  .bad { color:var(--hot); }
  @media (max-width:920px) {
    main { grid-template-columns:minmax(0,1fr); }
    .rows { border-right:0; }
    .row { grid-template-columns:56px minmax(0,1fr); padding:11px 14px; }
    .row .thumb { display:none; }
    header.top, .bar { padding-left:14px; padding-right:14px; }
    .status { width:100%; margin-left:0; }
    /* The status line takes a row of its own on a phone, which drops the icon onto the next
       one at the left edge - as far from "top right" as a header gets. */
    .net-toggle { margin-left:auto; }
  }
"""

BASE_HEAD = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="{og_type}">
<meta property="og:site_name" content="{site_name}">
<meta property="og:title" content="{og_title}">
<meta property="og:description" content="{og_description}">
<meta property="og:url" content="{canonical}">
{og_image}<meta name="twitter:card" content="{twitter_card}">
<link rel="alternate" type="application/atom+xml" title="upsy.si" href="/feed.xml">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<meta name="upsy-editor" content="{editor}">
<meta name="upsy-shots" content="{shots}">
<style>{css}</style>
</head>
<body>
<a class="skip" href="#stream">skip to the stream</a>
"""

HEADER = """<header class="top">
  <a class="brand" href="/">up<b>sy</b></a>
  <div class="status">
    <span><i class="dot"></i>watching {sources} sources</span>
    <span class="hot">{breaking} breaking</span>
    <span><b>{upsy}</b> anomalies</span>
    <span>updated {updated}</span>
    <span><a href="/sources.html">sources</a></span>
  </div>
  <button id="net-toggle" class="net-toggle" type="button" aria-expanded="false"
          aria-controls="net-drawer" aria-label="source network" title="source network">
    <!-- network, from lucide (ISC licence): https://lucide.dev/icons/network -->
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/>
      <rect x="9" y="2" width="6" height="6" rx="1"/>
      <path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/>
    </svg>
    <span class="net-count" id="net-count" hidden></span>
  </button>
</header>
"""

def footer(items=None, sources=None) -> str:
    stats = f"<span>{items} stories from {sources} sources</span>" if items and sources else ""
    return f"""<footer>
  <span>upsy.si &mdash; {TAGLINE}</span>
  {stats}
  <span><a href="/feed.xml">atom feed</a></span>
  <span><a href="/sources.html">sources &amp; method</a></span>
  <span>every headline belongs to the outlet that published it; links go to the original</span>
</footer>"""


def src_key(name: str) -> str:
    """A stable key for a source, used to line the sidebar buttons up with the rows."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def chip_row(item: dict, now: datetime, new_ids: set[str]) -> str:
    tags = []
    if item["breaking"]:
        tags.append('<span class="tag breaking">breaking</span>')
    if item["upsy"]:
        tags.append('<span class="tag upsy">anomaly</span>')
    if item["id"] in new_ids:
        tags.append('<span class="tag new">new</span>')
    if item.get("promo"):
        tags.append('<span class="tag q">promo</span>')
    picture = item.get("imageLocal") or item.get("image")
    image = (f'<img class="thumb" src="{esc(picture)}" alt="{esc(item.get("imageAlt") or "")}" loading="lazy" width="112" height="76">'
             if picture else '<div class="thumb empty" aria-hidden="true"></div>')
    trace = " ".join(f"<i>{esc(t)}</i>" for t in ((item.get("aiTerms") or [])[:2] + (item.get("siTerms") or [])[:3]))
    dek = f'<p class="dek">{esc(truncate(item["summary"], 190))}</p>' if item.get("summary") else ""
    meter = min(100.0, max(5.0, float(item["score"]) * 0.7))
    return f"""<article class="row{' is-new' if item['id'] in new_ids else ''}" data-upsy="{int(item['upsy'])}" data-breaking="{int(item['breaking'])}" data-image="{int(bool(item.get('image')))}" data-src="{src_key(item['sourceName'])}" data-title="{esc((item['title'] + ' ' + item['sourceName']).lower())}">
  <div class="energy"><span class="n">{item['score']:.0f}</span><span class="lbl">energy</span><span class="meter"><i style="width:{meter:.0f}%"></i></span><span class="tip" aria-hidden="true"><b>How energy is counted</b>freshness: <em>100 &times; 0.5 ^ (age / 26 h)</em> &mdash; halves every 26 hours<br>source tier: <em>42</em> lab &middot; <em>22</em> press &middot; <em>8</em> community<br>AI terms in headline and summary: up to <em>+15</em><br>picture <em>+6</em> &middot; aggregator <em>&minus;12</em> &middot; question headline <em>&minus;18</em> &middot; promotion <em>&minus;30</em> &middot; <em>+7</em> per further outlet running the same story<br>the bar is score &times; 0.7</span></div>
  <div>
    <div class="src"><a class="who" href="/s/{esc(item['id'])}/">{esc(item['sourceName'])}</a><span>{age(item.get('published'), now)} ago</span>{''.join(tags)}</div>
    <h2><a href="{esc(item['url'])}" target="_blank" rel="noopener nofollow">{esc(truncate(item['title'], 150))}</a></h2>
    {dek}
    <div class="trace">{trace}</div>
  </div>
  {image}
</article>"""


FILTER_BAR = """<div class="bar">
  <span id="count"></span>
  <button id="f-breaking" aria-pressed="false">breaking</button>
  <button id="f-upsy" aria-pressed="false">anomalies</button>
  <button id="f-image" aria-pressed="false">with image</button>
  <input id="q" type="search" placeholder="filter titles and sources" aria-label="filter titles and sources">
  <span class="spacer"></span>
  <span id="freshness">static page, rebuilt by a workflow</span>
</div>
<button id="to-top" type="button" aria-label="back to the top">&#8593;<span>top</span></button>"""


# The source list, hoisted out of the right column: the header icon opens it. It is still the filter
# as well as the list - a tap narrows the stream to that source - so it has to be one tap from the
# page, never a page of its own.
NET_DRAWER = """<div class="net-backdrop" id="net-backdrop"></div>
<aside class="net-drawer" id="net-drawer" aria-label="source network">
  <div class="net-head">
    <h3>source network</h3>
    <button id="net-clear" type="button" hidden>clear</button>
    <button id="net-close" class="net-x" type="button" aria-label="close the source network">&#215;</button>
  </div>
  <p class="net-hint">Every source in this build, the busiest first; the number is what it contributed
  here. Tap one to read that source alone, tap it again to let the rest back in.</p>
  <div class="net" id="net">{net_rows}</div>
  <p class="net-all"><a href="/sources.html">all {total} sources &amp; the method</a></p>
</aside>"""

SIGNAL_JS = """<script>
(function () {
  // A live collection, not a snapshot: rows pulled in by "load more" answer to the same
  // filters, and the counter counts them. There were two snapshots, so neither saw the other's rows.
  const rows = document.getElementsByClassName('row');
  const count = document.getElementById('count');
  const state = { breaking: false, upsy: false, image: false, q: '', sources: new Set() };
  // One click shows that source alone, a second click drops it; with nothing
  // selected every feed is shown, which is where the page starts.
  for (const button of document.querySelectorAll('#net .net-item')) {
    button.addEventListener('click', function () {
      const key = button.dataset.src;
      if (state.sources.has(key)) state.sources.delete(key); else state.sources.add(key);
      const on = state.sources.has(key);
      button.classList.toggle('is-on', on);
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
      apply();
      // Deselecting shows every feed again, which means everything loaded, not just the first screen.
      if (window.upsyLoadAll) window.upsyLoadAll().then(apply);
    });
  }
  // The source network is folded behind the header icon now. The list inside is the same one that
  // narrows the stream, so opening and closing is all this code has to do - plus the badge that
  // says a source is selected, because a hidden filter the reader cannot see is a trap.
  const netToggle = document.getElementById('net-toggle');
  const netDrawer = document.getElementById('net-drawer');
  const netBackdrop = document.getElementById('net-backdrop');
  const netCount = document.getElementById('net-count');
  const netClear = document.getElementById('net-clear');
  const netClose = document.getElementById('net-close');
  function netOpen() { return netDrawer.classList.contains('is-open'); }
  function setNet(open) {
    netDrawer.classList.toggle('is-open', open);
    netBackdrop.classList.toggle('is-open', open);
    document.body.classList.toggle('net-open', open);
    netToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      const first = netDrawer.querySelector('.net-item');
      if (first) first.focus({ preventScroll: true });
    } else if (document.activeElement && netDrawer.contains(document.activeElement)) {
      netToggle.focus({ preventScroll: true });
    }
  }
  function syncNet() {
    const n = state.sources.size;
    netCount.hidden = n === 0;
    netCount.textContent = String(n);
    netClear.hidden = n === 0;
    netToggle.classList.toggle('is-filtered', n > 0);
    netToggle.setAttribute('aria-label', n ? 'source network, ' + n + ' selected' : 'source network');
  }
  netToggle.addEventListener('click', function () { setNet(!netOpen()); });
  netClose.addEventListener('click', function () { setNet(false); });
  netBackdrop.addEventListener('click', function () { setNet(false); });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && netOpen()) setNet(false);
  });
  netClear.addEventListener('click', function () {
    for (const button of document.querySelectorAll('#net .net-item')) {
      button.classList.remove('is-on');
      button.setAttribute('aria-pressed', 'false');
    }
    state.sources.clear();
    apply();
    if (window.upsyLoadAll) window.upsyLoadAll().then(apply);
  });
  function matches(row) {
    if (state.breaking && row.dataset.breaking !== '1') return false;
    if (state.upsy && row.dataset.upsy !== '1') return false;
    if (state.image && row.dataset.image !== '1') return false;
    if (state.q && !row.dataset.title.includes(state.q)) return false;
    if (state.sources.size && !state.sources.has(row.dataset.src)) return false;
    return true;
  }
  function apply() {
    let shown = 0;
    for (const row of rows) {
      const ok = matches(row);
      row.hidden = !ok;
      row.classList.toggle('is-hidden', !ok);
      if (ok) shown += 1;
    }
    count.textContent = shown + ' of ' + rows.length + ' shown';
    syncNet();
  }
  for (const name of ['breaking', 'upsy', 'image']) {
    const button = document.getElementById('f-' + name);
    button.addEventListener('click', function () {
      state[name] = !state[name];
      button.setAttribute('aria-pressed', String(state[name]));
      apply();
    });
  }
  const box = document.getElementById('q');
  let timer = null;
  box.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () { state.q = box.value.trim().toLowerCase(); apply(); }, 120);
  });
  apply();

  // Stories that were not here on the reader's last visit. Kept in local storage only.
  try {
    const seen = JSON.parse(localStorage.getItem('upsy.seen') || '[]');
    const known = new Set(seen);
    const ids = rows.map(r => (r.querySelector('.who') || {}).getAttribute?.('href') || '');
    rows.forEach(function (row, index) {
      if (seen.length && ids[index] && !known.has(ids[index])) row.classList.add('is-new');
    });
    localStorage.setItem('upsy.seen', JSON.stringify(ids.filter(Boolean)));
  } catch (error) { /* private mode, or storage disabled: the marker is a nicety, not a feature */ }

  // The page is static, so ask the generated meta.json whether a newer build exists.
  const stamp = document.body.dataset.built;
  if (stamp) {
    setInterval(async function () {
      try {
        const response = await fetch('/data/meta.json?cachebust=' + Date.now());
        const meta = await response.json();
        if (meta.generated && meta.generated !== stamp) {
          const bar = document.getElementById('freshness');
          if (bar) {
            bar.innerHTML = '<a href="/">new stories since this page was built &mdash; reload</a>';
          }
        }
      } catch (error) { /* offline or no data file: stay quiet */ }
    }, 300000);
  }
})();
  (function () {
    // Its own scope on purpose: a bare `const top` at the top level of a classic script collides with
    // window.top and throws a SyntaxError, which silently takes the whole script - filters included.
    const toTop = document.getElementById('to-top');
    const mark = document.getElementById('top-mark');
    function watchScroll() {
      toTop.classList.toggle('is-on', window.scrollY > 700);
    }
    window.addEventListener('scroll', watchScroll, { passive: true });
    // The observer is what actually drives it: it reacts to the marker leaving the viewport however
    // the reader got there, including a jump from the keyboard, a find-in-page or a restored position,
    // none of which reliably produce a scroll event this code would catch in time.
    // Belt and braces, and not decoration: some engines - a headless session with no compositor among
    // them - deliver neither a scroll event nor an observer callback for a programmatic scroll, so
    // without this the button simply never appears however far down the reader is. Four number
    // comparisons a second costs nothing.
    setInterval(watchScroll, 250);
    if (window.IntersectionObserver && mark) {
      new IntersectionObserver(function (entries) {
        toTop.classList.toggle('is-on', !entries[0].isIntersecting);
      }, { threshold: 0 }).observe(mark);
    }
    toTop.addEventListener('click', function () {
      // Instant, not smooth: this is the "get me back up quickly" control, and a smooth request is
      // silently ignored where there is no compositor to animate it - including in a headless check.
      window.scrollTo(0, 0);
    });
    watchScroll();
  })();
</script>"""


def build_index(data: dict, meta: dict, moments: dict, now: datetime) -> str:
    items = data["items"]
    counts = data["counts"]
    rows = "\n".join(chip_row(i, now, set()) for i in items[:INDEX_ROWS])
    anomalies = render_anomalies(moments, items, now)
    # Every source, not a top slice: the panel is the filter as well as the list. The number beside a
    # source is what it contributed to *this* build, not the lifetime total the health record keeps -
    # "1287" next to OpenAI when the page holds four hundred stories reads as a bug.
    from collections import Counter

    per_source = Counter(i.get("sourceName", "") for i in items)
    net_rows = "\n".join(
        f'<button type="button" class="net-item" data-src="{src_key(h["name"])}" aria-pressed="false"'
        f' title="{per_source.get(h["name"], 0)} in this build">'
        f'<span class="nm">{esc(h["name"])}</span>'
        f'<span class="num">{per_source.get(h["name"], 0)}</span></button>'
        for h in sorted(meta["health"], key=lambda h: (-per_source.get(h["name"], 0), h["name"].lower()))
    )
    head = BASE_HEAD.format(
        editor=esc(editor_note(moments)),
        shots=esc(shot_note()),
        title="upsy.si \u2014 watching superintelligence happen",
        description=truncate("Live wire on superintelligence: breaking news and upsy moments in AI, "
                             f"from {counts['sources']} sources and {counts['items']} stories.", 200),
        canonical=SITE_URL + "/", og_type="website", site_name=SITE_NAME,
        og_title="upsy.si \u2014 watching superintelligence happen",
        og_description=f"{counts['items']} stories, {counts['sources']} sources, {counts['breaking']} breaking.",
        og_image="", twitter_card="summary", css=CSS)
    header = HEADER.format(sources=counts["sources"], breaking=counts["breaking"],
                           upsy=counts["upsy"], updated=age(data["generated"], now))
    drawer = NET_DRAWER.format(net_rows=net_rows, total=len(meta["health"]))
    return f"""{head}{header}{drawer}{FILTER_BAR}
<span id="top-mark" aria-hidden="true"></span>
<main>
  <section class="rows" id="stream">
{rows}
    <div class="more"><button id="load-more" type="button">load {max(0, len(items) - INDEX_ROWS)} older stories</button></div>
  </section>
  <aside>
    <section>
      <h3>upsy moments</h3>
      {anomalies}
    </section>
    <section>
      <h3>what this is</h3>
      <p class="brief">{esc(truncate(moments.get('brief') or 'An automated wire: feeds are read every ten minutes, ranked by freshness, source and how squarely a story sits on superintelligence, and the notable ones are picked out for the rail.', 260))}</p>
    </section>
  </aside>
</main>
{footer(counts['items'], counts['sources'])}
{SIGNAL_JS}
<script>
(function () {{
  // A live collection, not a snapshot: rows pulled in by "load more" answer to the same
  // filters, and the counter counts them. There were two snapshots, so neither saw the other's rows.
  const rows = document.getElementsByClassName('row');
  const button = document.getElementById('load-more');
  let loaded = {INDEX_ROWS};
  const state = {{ breaking: false, upsy: false, image: false, q: '' }};
  function rowFor(item) {{
    const tags = [];
    if (item.breaking) tags.push('<span class="tag breaking">breaking</span>');
    if (item.upsy) tags.push('<span class="tag upsy">anomaly</span>');
    const image = item.image
      ? '<img class="thumb" src="' + item.image + '" alt="" loading="lazy" width="112" height="76">'
      : '<div class="thumb empty" aria-hidden="true"></div>';
    const dek = item.summary ? '<p class="dek">' + item.summary.slice(0, 190) + '</p>' : '';
    const meter = Math.min(100, Math.max(5, item.score * 0.7)).toFixed(0);
    const article = document.createElement('article');
    article.className = 'row';
    article.dataset.upsy = item.upsy ? '1' : '0';
    article.dataset.breaking = item.breaking ? '1' : '0';
    article.dataset.image = item.image ? '1' : '0';
    article.dataset.title = (item.title + ' ' + item.sourceName).toLowerCase();
    article.dataset.src = item.src || '';
    article.innerHTML =
      '<div class="energy"><span class="n">' + Math.round(item.score) + '</span>' +
      '<span class="lbl">energy</span><span class="meter"><i style="width:' + meter + '%"></i></span>' +
      '<span class="tip" aria-hidden="true"><b>How energy is counted</b>' +
      'freshness: <em>100 &times; 0.5 ^ (age / 26 h)</em> &mdash; halves every 26 hours<br>' +
      'source tier: <em>42</em> lab &middot; <em>22</em> press &middot; <em>8</em> community<br>' +
      'AI terms in headline and summary: up to <em>+15</em><br>' +
      'picture <em>+6</em> &middot; aggregator <em>&minus;12</em> &middot; question headline ' +
      '<em>&minus;18</em> &middot; promotion <em>&minus;30</em> &middot; <em>+7</em> per further ' +
      'outlet running the same story<br>the bar is score &times; 0.7</span></div>' +
      '<div><div class="src"><a class="who" href="/s/' + item.id + '/">' + item.sourceName + '</a>' +
      '<span>' + item.age + '</span>' + tags.join('') + '</div>' +
      '<h2><a href="' + item.url + '" target="_blank" rel="noopener nofollow">' + item.title + '</a></h2>' +
      dek + '</div>' + image;
    return article;
  }}
  button.addEventListener('click', async function () {{
    button.disabled = true;
    const response = await fetch('/data/news.json?cachebust=' + Date.now());
    const payload = await response.json();
    const stream = document.getElementById('stream');
    const batch = payload.items.slice(loaded, loaded + 120);
    for (const item of batch) stream.insertBefore(rowFor(item), stream.querySelector('.more'));
    loaded += batch.length;
    // the row list is live, so nothing needs appending here
    button.textContent = loaded >= payload.items.length ? 'that is every story tonight' : 'load more';
    button.disabled = loaded >= payload.items.length;
    button.dispatchEvent(new Event('upsy:applied'));
  }});
  const wait = ms => new Promise(r => setTimeout(r, ms));
  // A source's stories are mostly below the first screen, so choosing one has to fetch them or the
  // click lands on "0 of 120 shown" and reads as broken. Bounded: twenty batches, five seconds each.
  window.upsyLoadAll = async function () {{
    for (let i = 0; i < 20; i++) {{
      let waits = 0;
      while (button.disabled && waits++ < 20) await wait(250);
      if (waits >= 20) break;
      if (button.textContent.indexOf('every story') >= 0) break;
      button.click();
      await wait(250);
    }}
  }};
}})();
</script>
</body></html>
"""


def render_anomalies(moments: dict, items: list[dict], now: datetime) -> str:
    """Model-written moments when data/moments.json has them, ranked items when it does not."""
    cards = []
    for moment in moments.get("moments", [])[:5]:
        image = f'<img class="thumb" src="{esc(moment["image"])}" alt="" loading="lazy" width="112" height="76">' if moment.get("image") else ""
        link = moment.get("url") or "#"
        cards.append(
            f'<div class="anomaly"><h4><a href="{esc(link)}" target="_blank" rel="noopener nofollow">{esc(truncate(moment.get("title", ""), 90))}</a></h4>'
            f'<p>{esc(truncate(moment.get("why", ""), 170))}</p>'
            f'<div class="meta">{esc(moment.get("sourceName", ""))}</div>{image}</div>'
        )
    if not cards:
        for item in [i for i in items if i["upsy"]][:4] or items[:3]:
            cards.append(
                f'<div class="anomaly"><h4><a href="{esc(item["url"])}" target="_blank" rel="noopener nofollow">{esc(truncate(item["title"], 84))}</a></h4>'
                f'<p>{esc(truncate(item["summary"] or "Flagged by the stream as an upsy moment.", 150))}</p>'
                f'<div class="meta">{esc(item["sourceName"])} &middot; {age(item["published"], now)} ago</div></div>'
            )
    return "\n      ".join(cards) or '<div class="anomaly"><h4>nothing wild yet</h4><p>The stream has not flagged an anomaly tonight.</p></div>'


def text_to_html(text: str) -> str:
    """Feed text is markup of unknown provenance: keep the paragraph structure, drop everything else."""
    from html.parser import HTMLParser

    class Cleaner(HTMLParser):
        keep = {"p", "br", "h2", "h3", "blockquote", "pre", "li", "ul", "ol", "strong", "em", "b", "i", "code"}

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in self.keep:
                self.parts.append(f"<{tag}>")

        def handle_endtag(self, tag):
            if tag in self.keep:
                self.parts.append(f"</{tag}>")

        def handle_data(self, data):
            self.parts.append(html.escape(data))

    cleaner = Cleaner()
    cleaner.feed(text or "")
    body = "".join(cleaner.parts)
    if "<p>" not in body:
        body = "".join(f"<p>{part.strip()}</p>" for part in re.split(r"\n{2,}", body) if part.strip())
    return body


def build_story(item: dict, related: list[dict], now: datetime, moments: dict) -> str:
    # The social card points at our own copy, so a preview never depends on the publisher's CDN.
    card = item.get("imageLocal") or item.get("image")
    og_image = f'<meta property="og:image" content="{SITE_URL}{esc(card)}">\n' if card else ""
    head = BASE_HEAD.format(
        editor=esc(editor_note(moments)),
        shots=esc(shot_note()),
        title=f"{truncate(item['title'], 90)} | upsy.si",
        description=truncate(item["summary"] or item["title"], 200),
        canonical=f"{SITE_URL}/s/{item['id']}/", og_type="article", site_name=SITE_NAME,
        og_title=truncate(item["title"], 110), og_description=truncate(item["summary"] or item["title"], 200),
        og_image=og_image, twitter_card="summary_large_image" if item.get("image") else "summary", css=CSS)
    picture = item.get("imageLocal") or item.get("image")
    lead = (f'<img class="lead" src="{esc(picture)}" alt="{esc(item.get("imageAlt") or "")}" width="820">'
            f'<div class="credit">image: {esc(item["sourceName"])}</div>') if picture else ""
    if item.get("content"):
        body = text_to_html(item["content"])
        note = ""
    else:
        body = f'<p>{esc(item.get("summary") or "")}</p>' if item.get("summary") else ""
        note = ('<div class="notice">This source publishes only a summary in its feed, so the full article '
                'lives at the outlet. The link above and below goes to the original.</div>')
    tags = [f'<span class="tag{" breaking" if item["breaking"] else ""}{" upsy" if item["upsy"] else ""}">{t}</span>'
            for t in ([item["sourceName"]] + (item.get("siTerms") or [])[:3])]
    related_html = "\n      ".join(
        f'<a href="/s/{esc(r["id"])}/">{esc(truncate(r["title"], 96))} <span class="num">({esc(r["sourceName"])})</span></a>'
        for r in related
    ) or "<span>no related items yet</span>"
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "NewsArticle",
        "headline": truncate(item["title"], 110),
        "datePublished": item.get("published") or "",
        "dateModified": data_timestamp(),
        "url": f"{SITE_URL}/s/{item['id']}/",
        "image": [SITE_URL + (item.get("imageLocal") or item["image"])] if item.get("image") else [],
        "publisher": {"@type": "Organization", "name": SITE_NAME, "url": SITE_URL},
        "isBasedOn": item["url"],
    }, ensure_ascii=False)
    return f"""{head}<main>
  <div class="story">
    <div class="kicker">{''.join(tags)}<span>{age(item.get('published'), now)} ago</span><span>energy {item['score']:.0f}</span></div>
    <h1>{esc(item['title'])}</h1>
    {lead}
    <div class="body">{body}</div>
    <a class="cta" href="{esc(item['url'])}" target="_blank" rel="noopener nofollow">read the original at {esc(item['sourceName'])} &rarr;</a>
    {note}
    <div class="related"><h3>more from the stream</h3>
      {related_html}
      <a href="/">&larr; back to upsy.si</a>
    </div>
  </div>
</main>
{footer()}
<script type="application/ld+json">{ld}</script>
</body></html>
"""


def build_sources(meta: dict, sources: dict, now: datetime, moments: dict) -> str:
    rows = []
    by_id = {s["id"]: s for s in sources["sources"]}
    for health in sorted(meta["health"], key=lambda h: (h["status"][:2] != "ok", -h["items"])):
        source = by_id.get(health["id"], {})
        ok = health["status"].startswith("ok") or "kept" in health["status"]
        rows.append(
            f'<tr><td class="name">{esc(health["name"])}</td>'
            f'<td>{esc(source.get("category", ""))}</td>'
            f'<td>{source.get("tier", "")}</td>'
            f'<td class="{"" if ok else "bad"}">{esc(health["status"])}</td>'
            f'<td>{health["items"]}</td>'
            f'<td>{age(health.get("newest"), now)}</td>'
            f'<td><a href="{esc(source.get("url", ""))}" target="_blank" rel="noopener nofollow">feed</a> '
            f'{f"<a href=\"{esc(source.get('site',''))}\" target=\"_blank\" rel=\"noopener nofollow\">site</a>" if source.get("site") else ""}</td></tr>'
        )
    head = BASE_HEAD.format(
        editor=esc(editor_note(moments)),
        shots=esc(shot_note()),
        title="sources | upsy.si", description="Every feed upsy.si reads, with its health and freshness.",
        canonical=f"{SITE_URL}/sources.html", og_type="website", site_name=SITE_NAME,
        og_title="sources | upsy.si", og_description="Every feed upsy.si reads, with health and freshness.",
        og_image="", twitter_card="summary", css=CSS)
    return f"""{head}<main>
  <div class="story">
    <h1>sources</h1>
    <p class="brief">Feeds are read every ten minutes, directly, with no proxy and no scraping of pages
    that did not offer a feed. Adding one means adding an entry to <code>sources.json</code>.</p>
    <table class="sources">
      <thead><tr><th>source</th><th>kind</th><th>tier</th><th>status</th><th>items</th><th>newest</th><th></th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <div class="related"><a href="/">&larr; back to upsy.si</a></div>
  </div>
</main>
{footer()}
</body></html>
"""


def build_feed(items: list[dict], now: datetime) -> str:
    updated = now.isoformat(timespec="seconds")
    entries = []
    for item in items[:FEED_ITEMS]:
        entries.append(f"""  <entry>
    <title>{esc(item['title'])}</title>
    <link href="{esc(item['url'])}"/>
    <id>tag:upsy.si,2026:{esc(item['id'])}</id>
    <updated>{esc(item.get('published') or updated)}</updated>
    <summary>{esc(truncate(item.get('summary') or item['title'], 300))}</summary>
    <source url="{esc(item.get('sourceUrl', ''))}">{esc(item['sourceName'])}</source>
  </entry>""")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>upsy.si</title>
  <subtitle>{esc(TAGLINE)}</subtitle>
  <link href="{SITE_URL}/"/>
  <link rel="self" href="{SITE_URL}/feed.xml"/>
  <id>tag:upsy.si,2026:feed</id>
  <updated>{updated}</updated>
  <rights>Headlines and links belong to their publishers. upsy.si aggregates and links out.</rights>
{chr(10).join(entries)}
</feed>
"""


def build_sitemap(items: list[dict], now: datetime) -> str:
    urls = [f"  <url><loc>{SITE_URL}/</loc><lastmod>{now.date().isoformat()}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>",
            f"  <url><loc>{SITE_URL}/sources.html</loc><lastmod>{now.date().isoformat()}</lastmod><priority>0.4</priority></url>"]
    for item in items:
        lastmod = (item.get("published") or "")[:10]
        urls.append(f'  <url><loc>{SITE_URL}/s/{esc(item["id"])}/</loc>{f"<lastmod>{lastmod}</lastmod>" if lastmod else ""}<priority>0.6</priority></url>')
    return f'<?xml version="1.0" encoding="utf-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{chr(10).join(urls)}\n</urlset>\n'


ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" fill="#08090c"/>
<path d="M8 44 L20 44 L26 24 L34 52 L42 34 L48 44 L56 44" fill="none" stroke="#b6ff5f" stroke-width="4" stroke-linejoin="round"/>
<circle cx="26" cy="24" r="3.5" fill="#4ad4ff"/>
</svg>
"""


def data_timestamp() -> str:
    meta_path = DATA / "meta.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8")).get("generated", "")
        except Exception:
            return ""
    return ""


def main() -> int:
    now = datetime.now(timezone.utc)
    data = json.loads((DATA / "news.json").read_text(encoding="utf-8"))
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    moments_path = DATA / "moments.json"
    moments = json.loads(moments_path.read_text(encoding="utf-8")) if moments_path.exists() else {}
    items = data["items"]

    SITE.mkdir(exist_ok=True)
    cached = cache_images(items)
    shot_pictures = attach_shots(items, 200, SITE, IMAGE_CACHE.parent / "shots")
    (SITE / "s").mkdir(exist_ok=True)
    (SITE / "data").mkdir(exist_ok=True)
    (SITE / "index.html").write_text(build_index(data, meta, moments, now), encoding="utf-8")
    for item in items:
        related = [i for i in items if i["id"] != item["id"] and i["source"] == item["source"]][:3]
        if len(related) < 3:
            related += [i for i in items if i["id"] != item["id"] and i not in related][:3 - len(related)]
        folder = SITE / "s" / item["id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text(build_story(item, related, now, moments), encoding="utf-8")
    # Story pages and the sitemap cover the whole wire, not a slice of it. The wire carries four
    # hundred items and both loops used to stop at three, so anything past three hundred - a slow,
    # curated source sits exactly there - was linked and advertised as a path that did not exist.
    # The stream page loads older stories and checks for new builds from these two files.
    (SITE / "data" / "news.json").write_text(
        json.dumps({"generated": data["generated"], "counts": data["counts"],
                    "items": [{"id": i["id"], "title": esc(i["title"]), "url": esc(i["url"]),
                               "sourceName": esc(i["sourceName"]), "summary": esc(truncate(i["summary"], 200)),
                               "image": esc(i.get("imageLocal") or i["image"]), "score": i["score"], "breaking": i["breaking"],
                               "src": src_key(i["sourceName"]),
                               "upsy": i["upsy"], "age": age(i["published"], now)} for i in items]},
                   ensure_ascii=False), encoding="utf-8")
    (SITE / "data" / "meta.json").write_text(
        json.dumps({"generated": data["generated"], "counts": data["counts"]}, ensure_ascii=False), encoding="utf-8")
    (SITE / "sources.html").write_text(build_sources(meta, sources, now, moments), encoding="utf-8")
    (SITE / "feed.xml").write_text(build_feed(items, now), encoding="utf-8")
    (SITE / "sitemap.xml").write_text(build_sitemap(items, now), encoding="utf-8")
    (SITE / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")
    (SITE / "CNAME").write_text("upsy.si\n", encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    (SITE / "icon.svg").write_text(ICON, encoding="utf-8")
    pages = len(list((SITE / "s").iterdir()))
    print(f"built _site/: index.html ({min(len(items), INDEX_ROWS)} rows), {pages} story pages, "
          f"feed.xml, sitemap.xml, sources.html, {cached} cached image(s), {shot_pictures} screenshot(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
