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
MAX_IMAGE_BYTES = 8_000_000     # we download the publisher's picture, then shrink it ourselves
IMAGE_MAX_WIDTH = 720           # a card is drawn ~360 px wide and a story lead 820: 720 covers both
IMAGE_WEBP_QUALITY = 78


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


def optimise_picture(data: bytes, suffix: str) -> tuple[bytes, str]:
    """Shrink a publisher's picture to the size the page actually draws it.

    The wire used to keep whatever the CDN served, and its pictures measured 1.1 MB each; worse, the
    ones too big to mirror fell back to a hotlink to the original, so a dozen cards were pulling
    multi-megabyte files off somebody else's CDN on the reader's data plan. A 720 px WebP at this
    quality is 60-90 kB and is indistinguishable at the size the card is drawn.
    """
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:              # no Pillow: the build still works, the pictures are just heavy
        return data, suffix
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.mode in ("P", "LA", "RGBA") or "transparency" in image.info:
                # Flatten onto white: the card has no alpha, and a transparent PNG flattened to black
                # looks like a rendering bug.
                flat = Image.new("RGB", image.size, (255, 255, 255))
                rgba = image.convert("RGBA")
                flat.paste(rgba, mask=rgba.split()[-1])
                image = flat
            elif image.mode != "RGB":
                image = image.convert("RGB")
            if image.width > IMAGE_MAX_WIDTH:
                height = max(1, round(image.height * IMAGE_MAX_WIDTH / image.width))
                image = image.resize((IMAGE_MAX_WIDTH, height), Image.LANCZOS)
            buffer = BytesIO()
            animated = bool(getattr(image, "is_animated", False))
            if animated and len(data) > 2_000_000:
                # One 7.5 MB animation on a news card is a download nobody asked for; its first
                # frame is the picture the reader needs. Small animations keep their frames.
                image.seek(0)
                image.convert("RGB").save(buffer, format="WEBP", quality=IMAGE_WEBP_QUALITY, method=6)
            elif animated:
                frames = []
                for index in range(min(getattr(image, "n_frames", 1), 40)):
                    image.seek(index)
                    frames.append(image.convert("RGB").copy())
                frames[0].save(buffer, format="WEBP", quality=IMAGE_WEBP_QUALITY, method=6,
                               save_all=True, append_images=frames[1:], duration=image.info.get("duration", 100))
            else:
                image.save(buffer, format="WEBP", quality=IMAGE_WEBP_QUALITY, method=6)
            smaller = buffer.getvalue()
    except Exception:                # an exotic format is not a build failure
        return data, suffix
    if len(smaller) >= len(data):
        return data, suffix          # already lean: re-encoding it only loses quality
    return smaller, ".webp"


def picture_of(item: dict) -> str:
    """The picture this build actually has a copy of, or nothing.

    The publisher's own URL is deliberately not a fallback: that is how twelve cards came to pull
    multi-megabyte originals from someone else's CDN, on the reader's data, because the mirror had
    refused them for being too big. A card without a picture is the cheaper failure.
    """
    return item.get("imageLocal") or ""


def mirror_picture(url: str) -> str:
    """Fetch one picture, shrink it, and return the path it is served from ("" when it cannot be)."""
    import hashlib
    import shutil
    import urllib.request

    if not url.startswith("http"):
        return ""
    # The processing recipe is part of the key: a cache written before the pictures were shrunk
    # holds full-size originals, and reusing one would keep serving megabytes.
    # v2: the first pass left animated gifs at their original size, and the key has to change when the
    # processing does or the old full-size copy is reused forever.
    recipe = "w%d-webp%d-v2" % (IMAGE_MAX_WIDTH, IMAGE_WEBP_QUALITY)
    digest = hashlib.sha1((url + "|" + recipe).encode("utf-8")).hexdigest()[:20]
    stored = next((p for p in IMAGE_CACHE.glob(digest + ".*")), None)
    if stored is not None and not image_suffix(stored.read_bytes()[:16]):
        # A cache written before the validation existed, or a truncated download: drop it and fetch
        # again rather than serve a broken picture for as long as the cache survives.
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
        # A truncated download starts with the right magic bytes and still renders as a broken image,
        # so a declared length that does not match is a failed download, not a picture.
        if expected and expected.isdigit() and int(expected) != len(data):
            return ""
        suffix = image_suffix(data)
        if not suffix:
            return ""       # a challenge page or an error page, not a picture
        data, suffix = optimise_picture(data, suffix)
        stored = IMAGE_CACHE / (digest + suffix)
        stored.write_bytes(data)
    shutil.copy2(stored, SITE / "img" / stored.name)
    return f"/img/{stored.name}"


def cache_moment_images(moments: dict) -> int:
    """The sidebar's moments carry pictures too, and those were still hotlinked originals."""
    if not CACHE_IMAGES:
        return 0
    mirrored = 0
    for moment in moments.get("moments") or []:
        if moment.get("image") and moment["image"].startswith("http"):
            local = mirror_picture(moment["image"])
            if local:
                moment["imageLocal"] = local
                mirrored += 1
    return mirrored


def cache_images(items: list[dict], limit: int = 200) -> int:
    """Keep a copy of each story's picture inside the site.

    Hotlinking a publisher's CDN is both fragile - plenty of them refuse a request with a Referer
    from somewhere else, which shows the reader a broken thumbnail - and impolite, since the page
    would be spending someone else's bandwidth. So each picture is fetched once, shrunk to the size
    the page draws it, kept under data/images/ (cached between builds in CI) and served from /img/
    with the source credited.
    """
    if not CACHE_IMAGES:
        return 0
    import concurrent.futures as futures

    direct_network()
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    (SITE / "img").mkdir(exist_ok=True)
    wanted = [i for i in items if picture_of(i) == "" and (i.get("image") or "").startswith("http")][:limit]

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, local in zip(wanted, pool.map(lambda i: mirror_picture(i["image"]), wanted)):
            if local:
                item["imageLocal"] = local
    return sum(1 for item in items if item.get("imageLocal"))


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


def prune_unused_pictures() -> int:
    """Delete pictures in img/ that no page names.

    Every build copies the pictures its stories use out of the cache, and nothing ever removed the
    ones an earlier build had copied. The measurement that found this: 232 files in img/, 116 of them
    unreferenced, 11.2 MB of a page that served none of it.
    """
    folder = SITE / "img"
    if not folder.exists():
        return 0
    used = set()
    for page in SITE.rglob("*.html"):
        try:
            text = page.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        used.update(re.findall(r"/img/([A-Za-z0-9._-]+)", text))
    removed = 0
    for picture in folder.iterdir():
        if picture.is_file() and picture.name not in used:
            picture.unlink(missing_ok=True)
            removed += 1
    return removed


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
  /* The wire had no h1 at all: a page with no h1 has no title a screen reader or a crawler
     can name it by. This keeps the drawn page exactly as designed. */
  /* the listening lane and the player: same palette, nothing new to learn */
  .listen-row { display:grid; grid-template-columns:18px 1fr; gap:2px 8px; padding:8px 0;
                border-bottom:1px solid var(--line); text-decoration:none; }
  .listen-row:last-of-type { border-bottom:0; }
  .listen-row .play { grid-row:span 2; color:var(--upsy); font-size:12px; line-height:1.5; }
  .listen-row .lt { color:var(--fg); font-size:13px; line-height:1.35; }
  .listen-row:hover .lt { color:var(--link); }
  .listen-row .lm { color:var(--dim); font-size:11px; }
  audio.player { width:100%; margin:14px 0 4px; filter:saturate(.75); }
  .player-wrap { position:relative; }
  .player-wrap.is-loading audio.player { opacity:.55; }
  .player-wrap.is-loading::after { content:""; position:absolute; left:0; right:0; top:0; height:3px;
    background:linear-gradient(90deg, transparent, var(--upsy), transparent); background-size:50% 100%;
    background-repeat:no-repeat; animation:player-scan 1.1s linear infinite; }
  @keyframes player-scan {
    0% { background-position:-50% 0; }
    100% { background-position:150% 0; }
  }
  .player-state { margin:7px 0 0; font-size:11.5px; color:var(--upsy); letter-spacing:.04em; }
  .player-state::before { content:"▸ "; }
  @media (prefers-reduced-motion: reduce) {
    .player-wrap.is-loading::after { animation:none; background-position:50% 0; opacity:.6; }
  }
  button.chapter { font:inherit; font-size:inherit; color:var(--cool); background:none; border:0;
    border-bottom:1px dotted currentColor; padding:0 1px; cursor:pointer; }
  button.chapter:hover, button.chapter:focus-visible { color:var(--upsy); border-bottom-color:var(--upsy); }
  .listen-list { max-width:760px; margin:0 auto; padding:24px 16px 48px; }
  .listen-list h1 { font-size:22px; margin:0 0 6px; }
  .listen-item { border-top:1px solid var(--line); padding:16px 0; }
  .listen-item h2 { font-size:16px; margin:6px 0; line-height:1.3; }
  .listen-item h2 a { color:var(--fg); text-decoration:none; }
  .listen-item h2 a:hover { color:var(--link); }
  .listen-item .dek { color:var(--dim); font-size:13px; margin:0 0 8px; }
  .listen-play { font-size:12px; color:var(--upsy); text-decoration:none; }
  .listen-more { margin:22px 0 0; padding-top:14px; border-top:1px solid var(--line);
    font-size:11.5px; color:var(--dim2); line-height:1.6; }
  .badge { font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--upsy);
           border:1px solid var(--upsy); border-radius:3px; padding:0 4px; }
  .vh { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
        clip-path:inset(50%); white-space:nowrap; border:0; }
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
  /* A story page is one column. It inherited the front page's grid (story + 320px rail), so the
     article was laid out inside the left column and read as "stuck to the left" on any wide screen. */
  main.single { display:block; }
  .story { max-width:820px; margin:0 auto; padding:26px 20px 60px; }
  .story h1 { font-size:27px; line-height:1.25; font-weight:500; margin:10px 0 12px; }
  .story .kicker, .listen-item .kicker { font-size:11px; color:var(--dim); letter-spacing:.08em;
    text-transform:uppercase; display:flex; gap:24px; row-gap:8px; flex-wrap:wrap; align-items:center; }
  /* A dot between them, so the eye reads three facts rather than one run-on label. */
  .story .kicker > span + span::before, .listen-item .kicker > span + span::before {
    /* The middle dot is written as the character itself. Writing the CSS escape inside a
       Python string does not survive: the backslash-zero becomes a NUL byte and the page
       showed a placeholder glyph followed by "0b7". */
    content:"·"; margin-right:24px; color:var(--dim2); }
  .story img.lead { width:100%; border:1px solid var(--line); margin:16px 0; }
  .story .body { font-size:15px; line-height:1.65; color:#c8d2df; }
  .story .body p { margin:0 0 14px; }
  .story .body h2, .story .body h3 { font-size:16px; margin:22px 0 8px; }
  .story .body pre { background:var(--panel); border:1px solid var(--line); padding:10px; overflow-x:auto; font-size:12.5px; }
  .story .body blockquote { border-left:2px solid var(--line); margin:0 0 14px; padding-left:12px; color:var(--dim); }
  .story-top { max-width:820px; margin:0 auto; padding:16px 20px 0; display:flex; align-items:center;
               justify-content:space-between; gap:12px; }
  .story-top a.back { display:inline-flex; align-items:center; gap:8px; border:1px solid var(--line);
                      background:var(--panel); color:var(--fg); padding:9px 15px; font-size:13px;
                      text-decoration:none; border-radius:5px; }
  .story-top a.back:hover, .story-top a.back:focus-visible { border-color:var(--cool); color:var(--cool); }
  .story-top .crumb { font-size:12px; color:var(--dim); text-decoration:none; }
  .story-top .crumb:hover { color:var(--link); }
  /* An episode is listened to, not read line by line: centred, with the player in the middle. */
  .story.episode { text-align:center; }
  /* An episode's artwork is a thumbnail, not a banner: it identifies the show, the words are the page. */
  .story.episode img.lead { width:auto; max-width:300px; max-height:300px; margin:14px auto;
                            display:block; }
  .chapters { list-style:none; margin:8px 0 22px; padding:0; display:flex; flex-wrap:wrap;
              gap:6px 10px; justify-content:center; }
  .chapters li { margin:0; }
  .transcript { margin:26px auto 0; max-width:66ch; text-align:left; }
  .transcript summary { cursor:pointer; font-family:var(--mono); font-size:11px; letter-spacing:.14em;
                        text-transform:uppercase; color:var(--muted); padding:10px 0; }
  .transcript p { margin:0 0 12px; }
  h2.sub { font-family:var(--mono); font-size:11px; letter-spacing:.14em; text-transform:uppercase;
           color:var(--muted); font-weight:400; margin:26px 0 10px; }
  .story.episode .kicker { justify-content:center; }
  .story.episode h1 { font-size:29px; }
  .story.episode .body { max-width:66ch; margin:0 auto; }
  .story.episode .related a { text-align:center; }
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
{extra_meta}
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
    <span><a href="/listen.html">Podcasts</a></span>
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
    picture = picture_of(item)
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


def render_listen_lane(episodes: list[dict], now: datetime, limit: int = 3) -> str:
    """The front page's listening strip: three newest episodes, each one a link to its own page."""
    if not episodes:
        return ""
    rows = []
    for episode in episodes[:limit]:
        length = duration_label(episode.get("duration"))
        meta_bits = " &middot; ".join(bit for bit in (esc(episode["sourceName"]), esc(length),
                                                      age(episode.get("published"), now) + " ago") if bit)
        rows.append(f'<a class="listen-row" href="/s/{esc(episode["id"])}/">'
                    f'<span class="play" aria-hidden="true">&#9654;</span>'
                    f'<span class="lt">{esc(truncate(episode["title"], 78))}</span>'
                    f'<span class="lm">{meta_bits}</span></a>')
    return ('<section class="listen">\n      <h3>Podcasts</h3>\n      '
            + "\n      ".join(rows)
            + f'\n      <p class="brief"><a href="/listen.html">every episode and how to play it</a></p>\n    </section>')


def build_index(data: dict, meta: dict, moments: dict, now: datetime) -> str:
    items = data["items"]
    counts = data["counts"]
    # The stream is stories. An episode in the stream would be a row with no player, in a list that
    # sorts by freshness - so the newest episode of the week would push a breaking story down.
    stories = stories_of(items)
    rows = "\n".join(chip_row(i, now, set()) for i in stories[:INDEX_ROWS])
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
    # The front page shared a link with no picture while every story page carried one.
    lead_picture = next((picture_of(i) for i in items if picture_of(i)), "")
    head = BASE_HEAD.format(
        editor=esc(editor_note(moments)),
        shots=esc(shot_note()),
        title="upsy.si \u2014 watching superintelligence happen",
        description=truncate("Live wire on superintelligence: breaking news and upsy moments in AI, "
                             f"from {counts['sources']} sources and {counts['items']} stories.", 200),
        canonical=SITE_URL + "/", og_type="website", site_name=SITE_NAME,
        og_title="upsy.si \u2014 watching superintelligence happen",
        og_description=f"{counts['items']} stories, {counts['sources']} sources, {counts['breaking']} breaking.",
        og_image=(f'<meta property="og:image" content="{SITE_URL}{lead_picture}">\n' if lead_picture else ""),
        twitter_card="summary_large_image" if lead_picture else "summary",
        extra_meta="", css=CSS)
    header = HEADER.format(sources=counts["sources"], breaking=counts["breaking"],
                           upsy=counts["upsy"], updated=age(data["generated"], now))
    drawer = NET_DRAWER.format(net_rows=net_rows, total=len(meta["health"]))
    listen_lane = render_listen_lane(episodes_of(items), now, limit=3)
    return f"""{head}<h1 class="vh">upsy.si \u2014 a live wire on superintelligence</h1>{header}{drawer}{FILTER_BAR}
<span id="top-mark" aria-hidden="true"></span>
<main>
  <section class="rows" id="stream">
{rows}
    <div class="more"><button id="load-more" type="button">load {max(0, len(stories) - INDEX_ROWS)} older stories</button></div>
  </section>
  <aside>
    {listen_lane}
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
        image = f'<img class="thumb" src="{esc(moment["imageLocal"])}" alt="" loading="lazy" width="112" height="76">' if moment.get("imageLocal") else ""
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


LISTEN_HEADER = """<header class="top">
  <a class="brand" href="/">up<b>sy</b></a>
  <div class="status">
    <span><i class="dot"></i>{shows} shows</span>
    <span><b>{episodes}</b> episodes</span>
    <span>updated {updated}</span>
    <span><a href="/">the wire</a></span>
  </div>
  <button id="net-toggle" class="net-toggle" type="button" aria-expanded="false"
          aria-controls="net-drawer" aria-label="the shows we watch" title="the shows we watch">
    <!-- network, from lucide (ISC licence): https://lucide.dev/icons/network -->
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/>
      <rect x="9" y="2" width="6" height="6" rx="1"/>
      <path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/>
    </svg>
    <span class="net-count" id="net-count" hidden></span>
  </button>
</header>"""

LISTEN_DRAWER = """<div class="net-backdrop" id="net-backdrop"></div>
<aside class="net-drawer" id="net-drawer" aria-label="the shows we watch">
  <div class="net-head">
    <h3>shows we watch</h3>
    <button id="net-clear" type="button" hidden>clear</button>
    <button id="net-close" class="net-x" type="button" aria-label="close the show list">&#215;</button>
  </div>
  <p class="net-hint">Every podcast on the wire, the busiest first; the number is how many episodes
  each one has here. Tap one to listen to that show alone, tap it again to let the rest back in.</p>
  <div class="net" id="net">{net_rows}</div>
  <p class="net-all"><a href="/sources.html">every source &amp; the method</a></p>
</aside>"""

LISTEN_JS = """<script>
(function () {
  // The same drawer the front page folds behind this icon, pointed at the show list instead of the
  // stream: the button was in the header of this page with nothing behind it, so it did nothing at
  // all. Fold, filter, and say how many shows are selected.
  const toggle = document.getElementById('net-toggle');
  const drawer = document.getElementById('net-drawer');
  const backdrop = document.getElementById('net-backdrop');
  const count = document.getElementById('net-count');
  const clear = document.getElementById('net-clear');
  const close = document.getElementById('net-close');
  if (!toggle || !drawer) return;
  const selected = new Set();
  function open(flag) {
    drawer.classList.toggle('is-open', flag);
    backdrop.classList.toggle('is-open', flag);
    document.body.classList.toggle('net-open', flag);
    toggle.setAttribute('aria-expanded', flag ? 'true' : 'false');
    if (flag) {
      const first = drawer.querySelector('.net-item');
      if (first) first.focus({ preventScroll: true });
    } else if (document.activeElement && drawer.contains(document.activeElement)) {
      toggle.focus({ preventScroll: true });
    }
  }
  function apply() {
    for (const item of document.querySelectorAll('.listen-item')) {
      item.hidden = selected.size > 0 && !selected.has(item.dataset.src);
    }
    count.hidden = selected.size === 0;
    count.textContent = String(selected.size);
    clear.hidden = selected.size === 0;
    toggle.classList.toggle('is-filtered', selected.size > 0);
    toggle.setAttribute('aria-label', selected.size
      ? 'the shows we watch, ' + selected.size + ' selected' : 'the shows we watch');
  }
  toggle.addEventListener('click', function () { open(!drawer.classList.contains('is-open')); });
  close.addEventListener('click', function () { open(false); });
  backdrop.addEventListener('click', function () { open(false); });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && drawer.classList.contains('is-open')) open(false);
  });
  clear.addEventListener('click', function () {
    selected.clear();
    for (const button of document.querySelectorAll('#net .net-item')) {
      button.classList.remove('is-on');
      button.setAttribute('aria-pressed', 'false');
    }
    apply();
  });
  for (const button of document.querySelectorAll('#net .net-item')) {
    button.addEventListener('click', function () {
      const key = button.dataset.src;
      const wasOn = selected.has(key);
      if (wasOn) selected.delete(key); else selected.add(key);
      button.classList.toggle('is-on', !wasOn);
      button.setAttribute('aria-pressed', wasOn ? 'false' : 'true');
      apply();
    });
  }
})();
</script>"""

TIMESTAMP = re.compile(r"(?is)^(\s*)(\(?\s*\d{1,3}:\d{2}(?::\d{2})?\s*\)?)")
BLOCK_START = re.compile(
    r"(?is)(<(?:p|li|div)(?:\s[^>]*)?>)(\s*)((?:<(?:span|strong|em|b|i)>)?)(\s*)"
    r"(\(?\s*\d{1,3}:\d{2}(?::\d{2})?\s*\)?)")


def timestamp_seconds(label: str) -> int:
    """"(09:54)", "9:54" and "1:02:03" as a number of seconds; -1 when it is not a timestamp."""
    parts = re.findall(r"\d+", label or "")
    if not 2 <= len(parts) <= 3:
        return -1
    numbers = [int(part) for part in parts]
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        if minutes > 59 or seconds > 59:
            return -1
        return hours * 3600 + minutes * 60 + seconds
    minutes, seconds = numbers
    if seconds > 59:
        return -1
    return minutes * 60 + seconds


# An agenda is one paragraph with its lines broken by <br>: "05:00 Will we run out of energy?<br> 19:00
# Will specialised chips threaten NVIDIA's dominance?<br> ...". A block's first timestamp is not the only
# one that means navigation - every line that *begins* with a time is an entry, and a time in the middle
# of a line is part of the sentence.
LINE_START = re.compile(
    r"(?is)(<br\s*/?>|\n)(\s*)((?:<(?:span|strong|em|b|i)>)?)(\s*)"
    r"(\(?\s*\d{1,3}:\d{2}(?::\d{2})?\s*\)?)(?=[\s&]|$|<(?![/]))")


# A description often carries its list inline: "We cover: 1) Is Sundar Pichai team product or team model?
# 2) Google's attempt to build a unified assistant 3) Which Google teams get priority? …" - eleven items
# run together into one paragraph, which is a wall of words rather than a list. When the numbering counts
# up from one, each item gets its own line.
NUMBERED_ITEM = re.compile(r"(?<![\w)\]])(\d{1,2})[.)]\s+")


def numbered_lines(html: str) -> str:
    """Put each item of an inline numbered list on its own line.

    Only a list that counts up from one is touched, and only three items or more: "1) only" is a
    parenthetical, "3) reasons" is not a list at all, and a reference to "2)" further down an article is
    a reference. Tags are stepped over so attribute text and links are never rewritten.
    """
    parts = re.split(r"(<[^>]+>)", html)
    for index, part in enumerate(parts):
        if not part or part.startswith("<"):
            continue
        matches = list(NUMBERED_ITEM.finditer(part))
        if len(matches) < 3:
            continue
        numbers = [int(match.group(1)) for match in matches]
        if numbers != list(range(1, len(numbers) + 1)):
            continue
        pieces, last = [], 0
        for match in matches:
            before = part[last:match.start()]
            pieces.append(before)
            # the intro sentence ends where the list starts, so the first item gets a line of its own too
            if before.strip():
                pieces.append("<br>")
            last = match.start()
        pieces.append(part[last:])
        parts[index] = "".join(pieces)
    return "".join(parts)


def chapterise(html: str) -> str:
    """A timestamp at the start of a description line becomes a button that seeks the player.

    Shows publish their chapters as text ("(09:54) Weekly episode preview"), which is navigation a
    reader can read and cannot use. Only a line's *first* timestamp is converted, and only when the time
    opens the line: a time in the middle of a sentence is part of the sentence.
    """
    def mark(match: re.Match) -> str:
        opening, space, inner, space2, label = match.groups()
        seconds = timestamp_seconds(label)
        if seconds < 0:
            return match.group(0)
        # whatever wrapped the timestamp stays where it was; only the text becomes a button
        return (f'{opening}{space}{inner}{space2}<button type="button" class="chapter" '
                f'data-at="{seconds}">{label.strip()}</button>&nbsp;')

    def mark_after_break(match: re.Match) -> str:
        brk, space, inner, space2, label = match.groups()
        seconds = timestamp_seconds(label)
        if seconds < 0:
            return match.group(0)
        return (f'{brk}{space}{inner}{space2}<button type="button" class="chapter" '
                f'data-at="{seconds}">{label.strip()}</button>&nbsp;')

    # the block's own opening timestamp first, then every entry that follows a line break
    chapterised = BLOCK_START.sub(mark, html)
    return LINE_START.sub(mark_after_break, chapterised)


EPISODE_JS = """<script>
(function () {
  // Chapters are the show's own index; a timestamp that cannot be pressed is a note to self. Pressing
  // one seeks the player and starts it, and brings the player into view so the reader can see it move.
  //
  // The file comes from the show's own host, so there is a gap between the press and the sound: a
  // player that sits there silently is indistinguishable from one that ignored the press. Every state
  // the media element reports is written under it, and a scan line runs while it waits.
  const player = document.querySelector('audio.player');
  const state = document.getElementById('player-state');
  const wrap = document.getElementById('player-wrap');
  if (!player) return;
  let timer = null;
  function say(text) {
    if (!state) return;
    state.hidden = !text;
    state.textContent = text || '';
    if (wrap) wrap.classList.toggle('is-loading', Boolean(text));
  }
  function later(text, delay) {
    clearTimeout(timer);
    timer = setTimeout(function () { say(text); }, delay);
  }
  player.addEventListener('playing', function () { clearTimeout(timer); say(''); });
  player.addEventListener('canplay', function () { if (state && !state.hidden) later('', 400); });
  player.addEventListener('waiting', function () { say('buffering\u2026'); });
  player.addEventListener('stalled', function () { say('buffering\u2026'); });
  player.addEventListener('error', function () {
    say('the show\u2019s host would not start the file \u2014 press play to try again');
  });
  for (const mark of document.querySelectorAll('button.chapter[data-at]')) {
    mark.addEventListener('click', function () {
      const at = Number(mark.dataset.at);
      if (!isFinite(at) || at < 0) return;
      if (isFinite(player.duration) && player.duration > 0 && at > player.duration) return;
      say('loading from ' + mark.textContent.trim() + '\u2026');
      later('still loading \u2014 the file comes from the show\u2019s own host', 15000);
      try { player.currentTime = at; } catch (error) { say('this browser would not seek'); return; }
      const started = player.play();
      if (started && started.catch) {
        started.catch(function () { say('press play to start \u2014 this browser wanted a gesture'); });
      }
      player.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  }
  player.addEventListener('play', function () {
    if (player.readyState < 3) say('loading\u2026');
  });
})();
</script>"""

def duration_label(seconds: int) -> str:
    """"1 h 04 min", "48 min" - a length a reader can judge, not a number of seconds."""
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    hours, minutes = divmod(seconds // 60, 60)
    if hours and minutes:
        return f"{hours} h {minutes:02d} min"
    if hours:
        return f"{hours} h"
    return f"{minutes or 1} min"


def episodes_of(items: list[dict]) -> list[dict]:
    """The podcast episodes on the wire, newest first."""
    return sorted((i for i in items if i.get("kind") == "podcast" and i.get("audio")),
                  key=lambda i: i.get("published") or "", reverse=True)


def stories_of(items: list[dict]) -> list[dict]:
    """Everything that is news. Episodes have their own lane and never enter the breaking ticker."""
    return [i for i in items if i.get("kind") != "podcast"]


def player_html(item: dict) -> str:
    """The player: a native audio element, so nothing here is a third party's tracker.

    `preload="none"` means the reader's browser asks the podcast's own host for the file only when
    they press play - which is the whole reason this is not an embedded player from a platform.
    """
    if item.get("kind") != "podcast" or not item.get("audio"):
        return ""
    credits = [esc(item.get("sourceName") or "")]
    length = duration_label(item.get("duration"))
    if length:
        credits.append(esc(length))
    if item.get("transcript"):
        credits.append(f'<a href="{esc(item["transcript"])}" rel="noopener">the show&#39;s own transcript</a>')
    # A file that arrives from the show's own host takes a moment, and a frozen player reads as a
    # broken one. The wrap holds a line the script can talk through, and a scan line while it waits.
    return (f'<div class="player-wrap" id="player-wrap">'
            f'<audio class="player" controls preload="none" src="{esc(item["audio"])}"></audio>'
            f'<p class="player-state" id="player-state" aria-live="polite" hidden></p></div>\n'
            f'    <div class="credit">{" &middot; ".join(credits)}</div>')


def build_story(item: dict, related: list[dict], now: datetime, moments: dict) -> str:
    # The social card points at our own copy, so a preview never depends on the publisher's CDN.
    card = picture_of(item)
    og_image = f'<meta property="og:image" content="{SITE_URL}{esc(card)}">\n' if card else ""
    head = BASE_HEAD.format(
        editor=esc(editor_note(moments)),
        shots=esc(shot_note()),
        title=f"{truncate(item['title'], 90)} | upsy.si",
        description=truncate(item["summary"] or item["title"], 200),
        canonical=f"{SITE_URL}/s/{item['id']}/", og_type="article", site_name=SITE_NAME,
        og_title=truncate(item["title"], 110), og_description=truncate(item["summary"] or item["title"], 200),
        og_image=og_image, twitter_card="summary_large_image" if card else "summary",
        extra_meta=(f'<meta property="article:published_time" content="{esc(item["published"])}">\n'
                    f'<meta property="article:section" content="{esc(item["sourceName"])}">\n'
                    if item.get("published") else ""), css=CSS)
    picture = picture_of(item)
    lead = (f'<img class="lead" src="{esc(picture)}" alt="{esc(item.get("imageAlt") or "")}" width="820">'
            f'<div class="credit">image: {esc(item["sourceName"])}</div>') if picture else ""
    if item.get("kind") == "podcast" and item.get("contentHtml"):
        # An episode keeps the description's own markup: the sponsors are already gone from it, and
        # its links stay clickable with the words the show gave them ("LinkedIn", "Go Exploit
        # framework") instead of a bare URL in a paragraph.
        body = chapterise(numbered_lines(item["contentHtml"]))
        note = ""
        # The show's own index, when the feed did not carry it: times that seek the player.
        if item.get("chapters"):
            entries = "".join(
                f'<li><button type="button" class="chapter" data-at="{int(chapter["at"])}">'
                f'{esc(chapter["label"])}</button></li>'
                for chapter in item["chapters"] if chapter.get("label"))
            body += f'<h2 class="sub">Chapters</h2><ol class="chapters">{entries}</ol>'
        # And the transcript, folded away: it is the page's evidence, not its first impression.
        if item.get("transcriptText"):
            paragraphs = "".join(f"<p>{esc(block)}</p>"
                                 for block in item["transcriptText"].split("\n\n") if block.strip())
            body += (f'<details class="transcript"><summary>Read the transcript</summary>'
                     f'{paragraphs}</details>')
    elif item.get("content"):
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
    is_episode = item.get("kind") == "podcast"
    cta_label = (f"the episode at {esc(item['sourceName'])}" if is_episode
                 else f"read the original at {esc(item['sourceName'])}")
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "PodcastEpisode" if is_episode else "NewsArticle",
        "headline": truncate(item["title"], 110),
        "datePublished": item.get("published") or "",
        "dateModified": data_timestamp(),
        "url": f"{SITE_URL}/s/{item['id']}/",
        "image": [SITE_URL + (item.get("imageLocal") or item["image"])] if item.get("image") else [],
        "publisher": {"@type": "Organization", "name": SITE_NAME, "url": SITE_URL},
        "isBasedOn": item["url"],
        **({"associatedMedia": {"@type": "AudioObject", "contentUrl": item["audio"]},
            "partOfSeries": {"@type": "PodcastSeries", "name": item["sourceName"]}}
           if is_episode and item.get("audio") else {}),
    }, ensure_ascii=False)
    # The way back was only at the bottom of the page, under "more from the stream" - which is no use
    # to somebody who arrived from a link and started listening.
    # An episode belongs to the listening feed, so that is where its way back goes - the wire is the
    # crumb beside it. A story still returns to the wire, with the source list as its crumb.
    if is_episode:
        back = '<a class="back" href="/listen.html">&larr; Podcasts</a>'
        crumb = '<a class="crumb" href="/">the wire</a>'
    else:
        back = '<a class="back" href="/">&larr; upsy.si</a>'
        crumb = '<a class="crumb" href="/sources.html">sources</a>'
    story_class = "story episode" if is_episode else "story"
    top = f'<nav class="story-top">{back}{crumb}</nav>\n'
    # "energy" is how a story was ranked; an episode is not ranked that way, so its kicker says how
    # long it runs instead.
    third = duration_label(item.get("duration")) if is_episode else f"energy {item['score']:.0f}"
    episode_js = EPISODE_JS if is_episode and item.get("audio") else ""
    return f"""{head}{top}<main class="single">
  <div class="{story_class}">
    <div class="kicker">{''.join(tags)}<span>{age(item.get('published'), now)} ago</span><span>{third}</span></div>
    <h1>{esc(item['title'])}</h1>
    {player_html(item)}
    {lead}
    <div class="body">{body}</div>
    <a class="cta" href="{esc(item['url'])}" target="_blank" rel="noopener nofollow">{cta_label} &rarr;</a>
    {note}
    <div class="related"><h3>{'more podcasts' if is_episode else 'more from the stream'}</h3>
      {related_html}
      <a href="{'/listen.html' if is_episode else '/'}">&larr; back to {'Podcasts' if is_episode else 'upsy.si'}</a>
    </div>
  </div>
</main>
{episode_js}
{footer()}
<script type="application/ld+json">{ld}</script>
</body></html>
"""


LISTEN_PER_SHOW = 12          # one show may occupy at most this many rows on the listening page


def build_listen(items: list[dict], now: datetime, moments: dict) -> str:
    """Every episode on the wire, newest first, each one leading to a page with a player on it.

    The list itself carries no audio elements: a page with two hundred players on it asks the
    reader's browser to build two hundred controls, and the publisher's host to be referenced two
    hundred times before anything is played.
    """
    episodes = episodes_of(items)
    rows = []
    seen_per_show: dict[str, int] = {}
    trimmed: dict[str, int] = {}
    for episode in episodes:
        show = episode["sourceName"]
        seen_per_show[show] = seen_per_show.get(show, 0) + 1
        if seen_per_show[show] > LISTEN_PER_SHOW:
            # A daily show would otherwise own the top of this page. It keeps its newest handful here
            # and every episode keeps its own page, so nothing is lost - it is just not all shown.
            trimmed[show] = trimmed.get(show, 0) + 1
            continue
        length = duration_label(episode.get("duration"))
        badges = (['<span class="badge">transcript</span>']
              if episode.get("transcript") or episode.get("transcriptText") else [])
        when = age(episode.get("published"), now) + " ago" if episode.get("published") else ""
        rows.append(f'<article class="listen-item" data-src="{src_key(episode["sourceName"])}">\n'
                    f'      <div class="kicker"><span>{esc(episode["sourceName"])}</span>'
                    f'<span>{esc(when)}</span>' + (f'<span>{esc(length)}</span>' if length else "")
                    + "".join(badges) + '</div>\n'
                    f'      <h2><a href="/s/{esc(episode["id"])}/">{esc(episode["title"])}</a></h2>\n'
                    f'      <p class="dek">{esc(truncate(episode.get("summary") or "", 240))}</p>\n'
                    f'      <a class="listen-play" href="/s/{esc(episode["id"])}/">play on its page</a>\n'
                    f'    </article>')
    head = BASE_HEAD.format(
        editor=esc(editor_note(moments)),
        shots=esc(shot_note()),
        title="Podcasts | upsy.si",
        description=("AI podcasts worth the time, each with a description you can read before you press "
                     "play and a player that asks the publisher's host for the file only when you do."),
        canonical=f"{SITE_URL}/listen.html", og_type="website", site_name=SITE_NAME,
        og_title="Podcasts | upsy.si",
        og_description=f"{len(episodes)} episodes from {len({e['sourceName'] for e in episodes})} shows.",
        og_image="", twitter_card="summary", extra_meta="", css=CSS)
    header = LISTEN_HEADER.format(shows=len({e["sourceName"] for e in episodes}),
                                  episodes=len(episodes), updated=age(data_timestamp(), now))
    body = "\n    ".join(rows) or "<p>No episodes on the wire yet.</p>"
    if trimmed:
        hidden = sum(trimmed.values())
        names = ", ".join(f"{esc(name)} ({count})" for name, count in sorted(trimmed.items(),
                                                                          key=lambda pair: -pair[1]))
        body += (f'\n    <p class="listen-more">Showing the newest {LISTEN_PER_SHOW} of each show. '
                 f'{hidden} older episode(s) still have their own pages: {names}.</p>')
    # The drawer lists the shows, busiest first, with what each one contributed - the same idea as the
    # source network on the wire, because a reader who wants one show wants it here too.
    per_show = {}
    for episode in episodes:
        per_show.setdefault(episode["sourceName"], 0)
        per_show[episode["sourceName"]] += 1
    net_rows = "\n".join(
        f'<button type="button" class="net-item" data-src="{src_key(name)}" aria-pressed="false"'
        f' title="{number} episode(s) on the wire">'
        f'<span class="nm">{esc(name)}</span><span class="num">{number}</span></button>'
        for name, number in sorted(per_show.items(), key=lambda pair: (-pair[1], pair[0].lower())))
    drawer = LISTEN_DRAWER.format(net_rows=net_rows)
    return (f"""{head}<h1 class="vh">upsy.si \u2014 podcasts</h1>{header}{drawer}
<main>
  <div class="listen-list">
    <h1>Podcasts</h1>
    <p class="brief">Podcast episodes from shows that publish a transcript or a description worth
    reading. The player is the browser's own, and the file is asked for only when you press play.</p>
    {body}
  </div>
</main>
{footer(len(episodes), len({e['sourceName'] for e in episodes}))}
{LISTEN_JS}
</body></html>
""")


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
        og_image="", twitter_card="summary", extra_meta="", css=CSS)
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


def build_sitemap(items: list[dict], now: datetime, extra_pages: tuple[str, ...] = ("/listen.html",)) -> str:
    urls = [f"  <url><loc>{SITE_URL}/</loc><lastmod>{now.date().isoformat()}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>",
            f"  <url><loc>{SITE_URL}/sources.html</loc><lastmod>{now.date().isoformat()}</lastmod><priority>0.4</priority></url>"]
    for page in extra_pages:
        urls.append(f"  <url><loc>{SITE_URL}{page}</loc><lastmod>{now.date().isoformat()}</lastmod>"
                    f"<changefreq>daily</changefreq><priority>0.5</priority></url>")
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
    moment_pictures = cache_moment_images(moments)
    (SITE / "s").mkdir(exist_ok=True)
    (SITE / "data").mkdir(exist_ok=True)
    (SITE / "index.html").write_text(build_index(data, meta, moments, now), encoding="utf-8")
    (SITE / "listen.html").write_text(build_listen(items, now, moments), encoding="utf-8")
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
    # The stream is stories. An episode in "load more" would arrive as a row with no player, so
    # episodes are served beside it in their own file - which is also the file a machine should read
    # when it wants to know what is worth listening to.
    (SITE / "data" / "news.json").write_text(
        json.dumps({"generated": data["generated"], "counts": data["counts"],
                    "items": [{"id": i["id"], "title": esc(i["title"]), "url": esc(i["url"]),
                               "sourceName": esc(i["sourceName"]), "summary": esc(truncate(i["summary"], 200)),
                               "image": esc(picture_of(i)), "score": i["score"], "breaking": i["breaking"],
                               "src": src_key(i["sourceName"]),
                               "upsy": i["upsy"], "age": age(i["published"], now)}
                              for i in stories_of(items)]},
                   ensure_ascii=False), encoding="utf-8")
    (SITE / "data" / "episodes.json").write_text(
        json.dumps({"generated": data["generated"],
                    "items": [{"id": i["id"], "title": esc(i["title"]), "show": esc(i["sourceName"]),
                               "page": f"{SITE_URL}/s/{i['id']}/", "url": esc(i["url"]),
                               "audio": esc(i["audio"]), "audioType": esc(i["audioType"]),
                               "durationSeconds": i["duration"], "transcript": esc(i["transcript"]),
                               "transcriptType": esc(i["transcriptType"]), "published": i["published"],
                               "summary": esc(truncate(i.get("summary") or "", 400))}
                              for i in episodes_of(items)]},
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
    pruned = prune_unused_pictures()
    pages = len(list((SITE / "s").iterdir()))
    print(f"pruned {pruned} unused picture(s)")
    print(f"built _site/: index.html ({min(len(stories_of(items)), INDEX_ROWS)} rows), {pages} story pages "
          f"({len(episodes_of(items))} episodes), listen.html, "
          f"feed.xml, sitemap.xml, sources.html, {cached} cached image(s), {moment_pictures} moment picture(s), "
          f"{shot_pictures} screenshot(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
