#!/usr/bin/env python3
"""Generate throwaway design mockups for upsy.si from the real fetch.

Three stances, same data, so they can be compared rather than imagined. Throwaway on purpose: these
are for choosing a direction, not for shipping. Real headlines come from data/news.json, so what you
look at is what the site would actually carry tonight.

    python3 tools/sketch_mockups.py            # writes sketches/*/index.html + sketches/README.md
"""
from __future__ import annotations

import html
import json
import pathlib
import re
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKETCHES = ROOT / "sketches"


def load_items(limit: int = 26) -> list[dict]:
    data = json.loads((ROOT / "data" / "news.json").read_text(encoding="utf-8"))
    items = data["items"]
    breaking = [i for i in items if i["breaking"]][:3]
    upsy = [i for i in items if i["upsy"]][:4]
    rest = [i for i in items if i not in breaking and i not in upsy]
    seen, ordered = set(), []
    for group in (breaking, rest, upsy):
        for item in group:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            ordered.append(item)
    return ordered[:limit]


IMAGE_CACHE = ROOT / "sketches" / ".images"
MAX_IMAGE_BYTES = 1_500_000


def direct_network() -> None:
    """Same rule as the fetcher: the mockups are built direct, not through whatever proxy is exported."""
    import os
    if os.environ.get("UPSY_USE_PROXY") == "1":
        return
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(key, None)


def inline_images(items: list[dict], limit: int = 14) -> int:
    """Fold a few real pictures into the mockups.

    A mockup that hotlinks its images is a mockup that shows boxes when the network disagrees, and the
    whole point of looking at it is to judge how the pictures sit in the layout. Bounded on purpose:
    the first few, each under 400 KB, cached under sketches/.images so a regeneration is instant.
    """
    import base64
    import concurrent.futures as futures
    import hashlib
    import urllib.request

    direct_network()
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    targets = [i for i in items if i.get("image")][:limit]

    def one(item: dict) -> tuple[str, str | None]:
        url = item["image"]
        digest = hashlib.sha1(url.encode()).hexdigest()[:20]
        cached = IMAGE_CACHE / digest
        if cached.exists():
            data = cached.read_bytes()
        else:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "upsy.si/1.0 (+https://upsy.si/)"})
                data = urllib.request.urlopen(req, timeout=20).read(MAX_IMAGE_BYTES + 1)
            except Exception as exc:
                print(f"      image failed: {type(exc).__name__} {url[:70]}")
                return (item["id"], None)
            if len(data) > MAX_IMAGE_BYTES or not data:
                print(f"      image too big or empty ({len(data)} bytes): {url[:70]}")
                return (item["id"], None)
            cached.write_bytes(data)
        kind = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ("image/gif" if data[:3] == b"GIF" else "image/jpeg")
        return (item["id"], f"data:{kind};base64,{base64.b64encode(data).decode()}")

    inlined = 0
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        for identifier, uri in pool.map(one, targets):
            item = next((i for i in items if i["id"] == identifier), None)
            if item is None:
                continue
            if uri:
                item["image"] = uri
                inlined += 1
            else:
                item["image"] = ""
    return inlined


def ago(stamp: str | None) -> str:
    if not stamp:
        return "-"
    try:
        when = datetime.fromisoformat(stamp)
    except Exception:
        return "-"
    minutes = (datetime.now(timezone.utc) - when).total_seconds() / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    if minutes < 60 * 36:
        return f"{int(minutes / 60)}h"
    return f"{int(minutes / 1440)}d"


def short(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"


def dek(item: dict) -> str:
    text = item.get("summary") or ""
    text = re.sub(r"^[A-Za-z0-9 .'&]+ - ", "", text)  # Google News prefixes the outlet name
    return short(text, 220)


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


# --------------------------------------------------------------------------- variant 1: the wire

WIRE_CSS = """
  :root { --paper:#f5f2ea; --ink:#16150f; --rule:#cfc7b4; --accent:#a8410a; --muted:#6d6858; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--paper); color:var(--ink); font:15px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; padding-bottom:60px; }
  a { color:inherit; }
  header { border-bottom:2px solid var(--ink); padding:10px 18px 8px; display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; }
  .brand { font-size:30px; font-weight:700; letter-spacing:.22em; text-transform:uppercase; }
  .brand span { color:var(--accent); }
  .tagline { color:var(--muted); font-size:12px; letter-spacing:.06em; text-transform:uppercase; }
  .stamp { margin-left:auto; font-size:11px; color:var(--muted); }
  .ticker { display:flex; gap:0; border-bottom:1px solid var(--rule); background:var(--ink); color:var(--paper); overflow:hidden; }
  .ticker b { background:var(--accent); padding:5px 10px; font-size:11px; letter-spacing:.18em; flex:none; }
  .ticker .clip { overflow:hidden; flex:1; }
  .ticker .roll { display:flex; gap:26px; padding:5px 12px; font-size:12px; white-space:nowrap; animation:roll 38s linear infinite; }
  .ticker .roll span::before { content:"\25B8 "; color:var(--accent); }
  @keyframes roll { from { transform:translateX(0); } to { transform:translateX(-50%); } }
  main { display:grid; grid-template-columns:minmax(0,1fr) 310px; gap:0; align-items:start; }
  .stream { border-right:1px solid var(--rule); }
  .stream-head { display:flex; gap:14px; align-items:center; padding:8px 18px; border-bottom:1px solid var(--rule); font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); }
  .stream-head button { font:inherit; font-size:11px; letter-spacing:.12em; text-transform:uppercase; background:none; border:1px solid var(--rule); color:inherit; padding:3px 8px; cursor:pointer; }
  .stream-head button[aria-pressed="true"] { background:var(--ink); color:var(--paper); border-color:var(--ink); }
  .item { display:grid; grid-template-columns:26px minmax(0,1fr) 92px; gap:12px; padding:12px 18px; border-bottom:1px solid var(--rule); }
  .item:hover { background:#efeade; }
  .num { color:var(--muted); font-size:11px; padding-top:2px; }
  .meta { font-size:11px; color:var(--muted); letter-spacing:.04em; text-transform:uppercase; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .meta .src { color:var(--accent); }
  .flag { border:1px solid var(--accent); color:var(--accent); padding:0 4px; }
  .flag.upsy { border-color:#3f6b3a; color:#3f6b3a; }
  .flag.new { border-color:var(--ink); color:var(--ink); }
  .item h2 { font-size:17px; line-height:1.28; margin:3px 0 4px; font-weight:600; }
  .item h2 a { text-decoration:none; }
  .item h2 a:hover { text-decoration:underline; text-decoration-color:var(--accent); }
  .dek { font-size:13px; color:#413d31; max-width:74ch; }
  .thumb { width:92px; height:66px; object-fit:cover; border:1px solid var(--rule); background:#e6e0d1; }
  .terms { font-size:10.5px; color:var(--muted); margin-top:4px; }
  .terms i { font-style:normal; border-bottom:1px dotted var(--muted); }
  aside { padding:12px 16px; }
  aside h3 { font-size:11px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); border-bottom:1px solid var(--rule); padding-bottom:5px; margin-bottom:9px; }
  .upsy-card { border-left:3px solid #3f6b3a; padding:4px 0 10px 10px; margin-bottom:11px; }
  .upsy-card h4 { font-size:14px; line-height:1.3; }
  .upsy-card p { font-size:12px; color:#413d31; margin-top:3px; }
  table.health { width:100%; border-collapse:collapse; font-size:11.5px; }
  table.health td { border-bottom:1px dotted var(--rule); padding:3px 0; }
  table.health td:last-child { text-align:right; color:var(--muted); }
  .dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:#3f6b3a; margin-right:6px; }
  footer { border-top:1px solid var(--rule); margin-top:20px; padding:12px 18px; font-size:11px; color:var(--muted); }
  @media (max-width:900px) { main { grid-template-columns:minmax(0,1fr); } .stream { border-right:0; } }
"""


def wire_item(item: dict, index: int) -> str:
    flags = []
    if item["breaking"]:
        flags.append('<span class="flag">breaking</span>')
    if item["upsy"]:
        flags.append('<span class="flag upsy">upsy moment</span>')
    if item.get("id"):
        flags.append('<span class="flag new">new</span>')
    terms = "".join(f"<i>{esc(t)}</i> " for t in (item.get("siTerms") or [])[:3])
    image = (f'<img class="thumb" src="{esc(item["image"])}" alt="" loading="lazy">'
             if item.get("image") else '<div class="thumb"></div>')
    return f"""      <article class="item" data-upsy="{int(item['upsy'])}" data-breaking="{int(item['breaking'])}">
        <div class="num">{index:02d}</div>
        <div>
          <div class="meta"><span>{ago(item['published'])} ago</span><span class="src">{esc(item['sourceName'])}</span>{''.join(flags)}</div>
          <h2><a href="{esc(item['url'])}" target="_blank" rel="noopener">{esc(short(item['title'], 130))}</a></h2>
          <p class="dek">{esc(dek(item))}</p>
          <div class="terms">{terms}<span>score {item['score']}</span></div>
        </div>
        {image}
      </article>"""


def build_wire(items: list[dict], data: dict, health: list[dict]) -> str:
    breaking = [i for i in items if i["breaking"]] or items[:3]
    ticker = "".join(f"<span>{esc(short(i['title'], 96))} &mdash; {esc(i['sourceName'])}</span>"
                     for i in breaking * 3)
    stream = "\n".join(wire_item(i, n + 1) for n, i in enumerate(items))
    upsy = "\n".join(
        f'      <div class="upsy-card"><h4><a href="{esc(i["url"])}" target="_blank" rel="noopener">{esc(short(i["title"], 78))}</a></h4>'
        f'<p>{esc(short(dek(i), 120))}</p><p class="meta">{esc(i["sourceName"])} &middot; {ago(i["published"])} ago</p></div>'
        for i in [x for x in items if x["upsy"]][:4] or items[4:8])
    rows = "".join(f"<tr><td><span class='dot'></span>{esc(h['name'][:26])}</td><td>{h['items']} items</td></tr>"
                   for h in health[:11])
    counts = data["counts"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>upsy.si &mdash; the wire on superintelligence (mockup: the wire)</title>
<style>{WIRE_CSS}</style></head>
<body>
<header>
  <div class="brand">up<span>sy</span>.si</div>
  <div class="tagline">the wire on superintelligence &mdash; {counts['items']} stories from {counts['sources']} sources</div>
  <div class="stamp">built {esc(ago(data['generated']))} ago</div>
</header>
<div class="ticker"><b>BREAKING</b><div class="clip"><div class="roll">{ticker}</div></div></div>
<main>
  <section class="stream">
    <div class="stream-head">
      <span>ranked stream</span>
      <button id="f-breaking" aria-pressed="false">breaking only</button>
      <button id="f-upsy" aria-pressed="false">upsy only</button>
      <span id="count" style="margin-left:auto"></span>
    </div>
{stream}
  </section>
  <aside>
    <h3>upsy moments</h3>
{upsy}
    <h3 style="margin-top:18px">sources tonight</h3>
    <table class="health">{rows}</table>
  </aside>
</main>
<footer>mockup only &mdash; real headlines from tonight's fetch. this layout is the "wire" stance: dense, monospaced, one accent, built to scan.</footer>
<script>
  const items = [...document.querySelectorAll('.item')];
  const count = document.getElementById('count');
  let filter = null;
  function apply() {{
    let shown = 0;
    for (const el of items) {{
      const ok = !filter || el.dataset[filter] === '1';
      el.style.display = ok ? '' : 'none';
      if (ok) shown += 1;
    }}
    count.textContent = shown + ' shown';
    for (const id of ['breaking', 'upsy']) {{
      const b = document.getElementById('f-' + id);
      b.setAttribute('aria-pressed', String(filter === id));
    }}
  }}
  for (const id of ['breaking', 'upsy']) {{
    document.getElementById('f-' + id).addEventListener('click', () => {{
      filter = filter === id ? null : id;
      apply();
    }});
  }}
  apply();
</script>
</body></html>
"""


# --------------------------------------------------------------------------- variant 2: broadcast

BROADCAST_CSS = """
  :root { --paper:#fffdf8; --ink:#14110c; --rule:#e6dfd0; --accent:#d33f1f; --upsy:#2f6b4f; --muted:#6f6a5e; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--paper); color:var(--ink); font:16px/1.55 Georgia,"Times New Roman",serif; }
  header { padding:22px 26px 14px; border-bottom:1px solid var(--rule); display:flex; align-items:baseline; gap:18px; flex-wrap:wrap; }
  .brand { font-size:34px; letter-spacing:.02em; font-weight:700; }
  .brand span { color:var(--accent); }
  .tagline { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:13px; color:var(--muted); }
  nav { margin-left:auto; display:flex; gap:18px; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:14px; }
  nav button { font:inherit; background:none; border:0; border-bottom:2px solid transparent; padding:4px 0; cursor:pointer; color:var(--muted); }
  nav button[aria-pressed="true"] { color:var(--ink); border-color:var(--accent); font-weight:600; }
  .hero { display:grid; grid-template-columns:1.35fr 1fr; gap:26px; padding:26px; border-bottom:1px solid var(--rule); align-items:center; }
  .hero img { width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:3px; background:#efe9dd; }
  .hero .kicker { font-family:-apple-system,sans-serif; font-size:12px; letter-spacing:.14em; text-transform:uppercase; color:var(--accent); }
  .hero h1 { font-size:34px; line-height:1.14; margin:8px 0 10px; }
  .hero p { font-size:18px; color:#3b352b; }
  .hero .by { font-family:-apple-system,sans-serif; font-size:13px; color:var(--muted); margin-top:12px; }
  .grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:26px; padding:26px; }
  .card img { width:100%; aspect-ratio:3/2; object-fit:cover; border-radius:3px; background:#efe9dd; }
  .card h3 { font-size:22px; line-height:1.22; margin:10px 0 6px; }
  .card h3 a { text-decoration:none; color:inherit; }
  .card h3 a:hover { text-decoration:underline; text-decoration-color:var(--accent); }
  .card .by { font-family:-apple-system,sans-serif; font-size:12.5px; color:var(--muted); }
  .upsy-band { background:#f3f7f4; border-top:1px solid var(--rule); border-bottom:1px solid var(--rule); padding:22px 26px; }
  .upsy-band h2 { font-family:-apple-system,sans-serif; font-size:13px; letter-spacing:.16em; text-transform:uppercase; color:var(--upsy); margin-bottom:14px; }
  .upsy-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:18px; }
  .upsy-card { background:#fff; border:1px solid #d8e5dc; border-left:4px solid var(--upsy); padding:12px 14px; border-radius:3px; }
  .upsy-card h4 { font-size:18px; line-height:1.25; }
  .upsy-card p { font-family:-apple-system,sans-serif; font-size:13px; color:#40544a; margin-top:6px; }
  .pill { display:inline-block; font-family:-apple-system,sans-serif; font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; background:var(--accent); color:#fff; padding:2px 7px; border-radius:2px; margin-right:6px; vertical-align:2px; }
  footer { padding:22px 26px; font-family:-apple-system,sans-serif; font-size:13px; color:var(--muted); }
  @media (max-width:1000px) { .hero { grid-template-columns:1fr; } .grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .upsy-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
"""


def broadcast_card(item: dict, hero: bool = False) -> str:
    image = (f'<img src="{esc(item["image"])}" alt="" loading="lazy">'
             if item.get("image") else '<div style="aspect-ratio:3/2;background:#efe9dd;border-radius:3px"></div>')
    pill = '<span class="pill">breaking</span>' if item["breaking"] else ""
    if hero:
        return f"""    <article class="hero" data-upsy="{int(item['upsy'])}" data-breaking="{int(item['breaking'])}">
      <div>{image}</div>
      <div>
        <div class="kicker">{esc(item['sourceName'])} &middot; {ago(item['published'])} ago</div>
        <h1>{pill}<a href="{esc(item['url'])}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">{esc(short(item['title'], 92))}</a></h1>
        <p>{esc(short(dek(item), 240))}</p>
        <div class="by">{esc(item.get('author') or item['sourceName'])} &middot; score {item['score']}</div>
      </div>
    </article>"""
    return f"""    <article class="card" data-upsy="{int(item['upsy'])}" data-breaking="{int(item['breaking'])}">
      {image}
      <div class="by">{pill}{esc(item['sourceName'])} &middot; {ago(item['published'])} ago</div>
      <h3><a href="{esc(item['url'])}" target="_blank" rel="noopener">{esc(short(item['title'], 104))}</a></h3>
      <p style="font-size:15px;color:#3b352b">{esc(short(dek(item), 150))}</p>
    </article>"""


def build_broadcast(items: list[dict], data: dict, health: list[dict]) -> str:
    # The sketch picks a hero that has a picture, otherwise the layout is judged on an empty box.
    hero = next((i for i in items if i.get("image")), items[0])
    rest = [i for i in items if i is not hero][:12]
    cards = "\n".join(broadcast_card(i) for i in rest)
    upsy_items = [i for i in items if i["upsy"]][:4] or items[13:17]
    upsy = "\n".join(
        f'      <div class="upsy-card"><h4><a href="{esc(i["url"])}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">{esc(short(i["title"], 72))}</a></h4>'
        f'<p>{esc(short(dek(i), 110))}</p><p class="by" style="font-family:-apple-system,sans-serif;font-size:12px;color:#40544a;margin-top:6px">{esc(i["sourceName"])} &middot; {ago(i["published"])} ago</p></div>'
        for i in upsy_items
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>upsy.si &mdash; superintelligence, daily (mockup: broadcast)</title>
<style>{BROADCAST_CSS}</style></head>
<body>
<header>
  <div class="brand">up<span>sy</span></div>
  <div class="tagline">superintelligence, daily &middot; {data['counts']['items']} stories, {data['counts']['sources']} sources</div>
  <nav>
    <button id="n-latest" aria-pressed="true">front page</button>
    <button id="n-breaking" aria-pressed="false">breaking</button>
    <button id="n-upsy" aria-pressed="false">upsy moments</button>
  </nav>
</header>
{broadcast_card(hero, hero=True)}
<section class="upsy-band">
  <h2>upsy moments &mdash; a machine did something worth a second look</h2>
  <div class="upsy-grid">
{upsy}
  </div>
</section>
<section class="grid" id="grid">
{cards}
</section>
<footer>mockup only &mdash; real headlines from tonight's fetch. the "broadcast" stance: a familiar daily front page, image-led, generous type.</footer>
<script>
  const cards = [...document.querySelectorAll('[data-upsy]')];
  const buttons = {{ latest: document.getElementById('n-latest'), breaking: document.getElementById('n-breaking'), upsy: document.getElementById('n-upsy') }};
  let view = 'latest';
  function apply() {{
    for (const el of cards) {{
      const ok = view === 'latest' || el.dataset[view] === '1';
      el.style.display = ok ? '' : 'none';
    }}
    for (const [name, b] of Object.entries(buttons)) b.setAttribute('aria-pressed', String(name === view));
  }}
  for (const [name, b] of Object.entries(buttons)) b.addEventListener('click', () => {{ view = name; apply(); }});
  apply();
</script>
</body></html>
"""


# --------------------------------------------------------------------------- variant 3: signal

SIGNAL_CSS = """
  :root { --bg:#08090c; --panel:#0e1014; --line:#1e232b; --fg:#dfe6ef; --dim:#7d8794; --hot:#ff5f4a; --cool:#4ad4ff; --upsy:#b6ff5f; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--fg); font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  a { color:inherit; text-decoration:none; }
  header { display:flex; align-items:center; gap:14px; padding:12px 18px; border-bottom:1px solid var(--line); }
  .brand { font-size:22px; letter-spacing:.3em; text-transform:uppercase; }
  .brand span { color:var(--cool); }
  .live { margin-left:auto; font-size:11px; color:var(--dim); display:flex; gap:14px; align-items:center; }
  .live b { color:var(--upsy); font-weight:400; }
  .pulse { width:8px; height:8px; border-radius:50%; background:var(--hot); animation:p 1.6s ease-in-out infinite; }
  @keyframes p { 0%,100% { opacity:1; } 50% { opacity:.15; } }
  .bar { display:flex; gap:10px; padding:9px 18px; border-bottom:1px solid var(--line); font-size:11.5px; color:var(--dim); flex-wrap:wrap; align-items:center; }
  .bar button { font:inherit; background:none; border:1px solid var(--line); color:var(--dim); padding:3px 9px; cursor:pointer; }
  .bar button[aria-pressed="true"] { border-color:var(--cool); color:var(--cool); }
  main { display:grid; grid-template-columns:minmax(0,1fr) 300px; }
  .rows { border-right:1px solid var(--line); }
  .row { display:grid; grid-template-columns:64px minmax(0,1fr) 104px; gap:14px; padding:11px 18px; border-bottom:1px solid var(--line); position:relative; }
  .row:hover { background:var(--panel); }
  .energy { display:flex; flex-direction:column; gap:3px; }
  .energy .n { font-size:16px; color:var(--fg); }
  .energy .lbl { font-size:9.5px; letter-spacing:.1em; color:var(--dim); text-transform:uppercase; }
  .meter { height:3px; background:var(--line); margin-top:5px; }
  .meter i { display:block; height:100%; background:linear-gradient(90deg,var(--cool),var(--upsy)); }
  .row h2 { font-size:15.5px; line-height:1.35; font-weight:500; }
  .row .src { font-size:11px; color:var(--dim); margin-bottom:4px; display:flex; gap:10px; flex-wrap:wrap; }
  .row .src b { color:var(--cool); font-weight:400; }
  .tag { font-size:9.5px; letter-spacing:.1em; text-transform:uppercase; border:1px solid currentColor; padding:0 4px; }
  .tag.breaking { color:var(--hot); }
  .tag.upsy { color:var(--upsy); }
  .trace { font-size:11px; color:var(--dim); margin-top:5px; }
  .thumb { width:104px; height:70px; object-fit:cover; filter:saturate(.85) contrast(1.05); border:1px solid var(--line); }
  aside { padding:14px 16px; }
  aside h3 { font-size:10.5px; letter-spacing:.18em; text-transform:uppercase; color:var(--dim); margin-bottom:9px; }
  .anomaly { border:1px solid #26330f; background:#0f1408; padding:10px 11px; margin-bottom:9px; }
  .anomaly h4 { font-size:13.5px; font-weight:500; color:var(--upsy); }
  .anomaly p { font-size:11.5px; color:#9fb383; margin-top:5px; }
  .net { font-size:11.5px; color:var(--dim); }
  .net div { display:flex; justify-content:space-between; border-bottom:1px dotted var(--line); padding:3px 0; }
  footer { border-top:1px solid var(--line); padding:12px 18px; font-size:11px; color:var(--dim); }
  @media (max-width:900px) { main { grid-template-columns:minmax(0,1fr); } .rows { border-right:0; } }
"""


def signal_row(item: dict, index: int) -> str:
    tags = []
    if item["breaking"]:
        tags.append('<span class="tag breaking">breaking</span>')
    if item["upsy"]:
        tags.append('<span class="tag upsy">anomaly</span>')
    width = min(100, max(6, item["score"] * 0.7))
    trace = " ".join(f"<i>{esc(t)}</i>" for t in (item.get("siTerms") or [])[:3])
    image = (f'<img class="thumb" src="{esc(item["image"])}" alt="" loading="lazy">'
             if item.get("image") else '<div class="thumb" style="background:#101317"></div>')
    return f"""      <article class="row" data-upsy="{int(item['upsy'])}" data-breaking="{int(item['breaking'])}">
        <div class="energy"><span class="n">{item['score']:.0f}</span><span class="lbl">energy</span><span class="meter"><i style="width:{width:.0f}%"></i></span></div>
        <div>
          <div class="src"><b>{esc(item['sourceName'])}</b><span>{ago(item['published'])} ago</span>{''.join(tags)}</div>
          <h2><a href="{esc(item['url'])}" target="_blank" rel="noopener">{esc(short(item['title'], 132))}</a></h2>
          <div class="trace">{trace}</div>
        </div>
        {image}
      </article>"""


def build_signal(items: list[dict], data: dict, health: list[dict]) -> str:
    rows = "\n".join(signal_row(i, n) for n, i in enumerate(items))
    anomalies = "\n".join(
        f'      <div class="anomaly"><h4><a href="{esc(i["url"])}" target="_blank" rel="noopener">{esc(short(i["title"], 66))}</a></h4>'
        f'<p>{esc(short(dek(i), 104))}</p><p style="margin-top:6px">{esc(i["sourceName"])} &middot; {ago(i["published"])} ago</p></div>'
        for i in ([x for x in items if x["upsy"]][:3] or items[5:8])
    )
    net = "".join(f"<div><span>{esc(h['name'][:24])}</span><span>{h['items']}</span></div>" for h in health[:12])
    counts = data["counts"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>upsy.si &mdash; superintelligence monitor (mockup: signal)</title>
<style>{SIGNAL_CSS}</style></head>
<body>
<header>
  <div class="brand">up<span>sy</span></div>
  <div class="live"><span class="pulse"></span><span>watching {counts['sources']} sources</span><span><b>{counts['breaking']}</b> breaking</span><span><b>{counts['upsy']}</b> anomalies</span><span>{esc(ago(data['generated']))} ago</span></div>
</header>
<div class="bar">
  <span>{counts['items']} signals ranked</span>
  <button id="f-breaking" aria-pressed="false">breaking</button>
  <button id="f-upsy" aria-pressed="false">anomalies</button>
  <button id="f-image" aria-pressed="false">with image</button>
  <span id="count" style="margin-left:auto"></span>
</div>
<main>
  <section class="rows">
{rows}
  </section>
  <aside>
    <h3>anomalies / upsy moments</h3>
{anomalies}
    <h3 style="margin-top:18px">source network</h3>
    <div class="net">{net}</div>
  </aside>
</main>
<footer>mockup only &mdash; real headlines from tonight's fetch. the "signal" stance: a monitoring console, dark, monospaced, energy-ranked.</footer>
<script>
  const rows = [...document.querySelectorAll('.row')];
  const count = document.getElementById('count');
  const state = {{ breaking:false, upsy:false, image:false }};
  function apply() {{
    let shown = 0;
    for (const el of rows) {{
      let ok = true;
      if (state.breaking && el.dataset.breaking !== '1') ok = false;
      if (state.upsy && el.dataset.upsy !== '1') ok = false;
      if (state.image && !el.querySelector('img')) ok = false;
      el.style.display = ok ? '' : 'none';
      if (ok) shown += 1;
    }}
    count.textContent = shown + ' / ' + rows.length;
  }}
  for (const name of Object.keys(state)) {{
    document.getElementById('f-' + name).addEventListener('click', event => {{
      state[name] = !state[name];
      event.currentTarget.setAttribute('aria-pressed', String(state[name]));
      apply();
    }});
  }}
  apply();
</script>
</body></html>
"""


README = """## upsy.si — three looks, same data

Throwaway mockups, generated from tonight's real fetch (`tools/sketch_mockups.py`). Open any
`index.html` in a browser. Each is self-contained; images are hotlinked from the sources for the
sketch only.

| | **001 the wire** | **002 broadcast** | **003 signal** |
|---|---|---|---|
| Stance | utilitarian newsroom wire | familiar daily front page | monitoring console |
| Density | high | medium | high |
| Typography | monospace throughout | serif headlines, sans meta | monospace, tabular numbers |
| Images | small thumbs beside the text | large, layout-defining | small, desaturated |
| Ground | paper and ink, one accent | warm paper, accent red, green upsy | near-black, cyan/amber |
| Strong at | scanning 400 items, feeds with no images, feeling like a machine wire | readers who want a front page, image-rich stories | the "we are watching this happen" angle, anomalies |
| Weak at | feeling like a magazine | feeds with no image (half of ours) | photographs, casual readers |
| Primary action | scan rank, filter to breaking | read the top story | watch the stream, spot anomalies |

Every variant carries the same skeleton: a breaking ticker or pill, a ranked stream, an upsy rail,
filters, and per-source counts.

**Recommendation:** `001 the wire` as the base — half of tonight's sources carry no image, and the
wire reads well at 400 items without needing them. Borrow `003`'s energy meter and dark mode as an
optional theme, and `002`'s hero treatment for the single top story.
"""


def main() -> int:
    items = load_items()
    if not items:
        raise SystemExit("no items in data/news.json - run tools/fetch.py first")
    data = json.loads((ROOT / "data" / "news.json").read_text(encoding="utf-8"))
    meta = json.loads((ROOT / "data" / "meta.json").read_text(encoding="utf-8"))
    health = [h for h in meta["health"] if h["status"].startswith("ok")]
    print(f"   inlined {inline_images(items)} image(s)")
    for slug, builder in (("001-the-wire", build_wire), ("002-broadcast", build_broadcast), ("003-signal", build_signal)):
        target = SKETCHES / slug
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text(builder(items, data, health), encoding="utf-8")
        print(f"   wrote sketches/{slug}/index.html")
    (SKETCHES / "README.md").write_text(README, encoding="utf-8")
    print("   wrote sketches/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
