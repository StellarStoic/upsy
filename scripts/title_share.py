"""How much of each podcast's recent output actually names AI in its headline.

A show that is *about* AI can be admitted wholesale — its headlines are the topic. A show that is
about something else (China policy, markets, war) needs its episodes narrowed, and the honest signal is
the headline: descriptions mention AI constantly ("...and what AI means for it"), so gating on those
would let the whole feed through.

This prints, per podcast source, how many of the newest 20 headlines match the general AI term list.
Low shares are the shows that should carry the gate.

    python3 scripts/title_share.py [--json]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.json"
TOR = ["--socks5-hostname", "10.0.3.1:9050"]

# The gate's own list, imported rather than copied: this audit has to measure what the gate would
# actually do, and a copy here drifted the moment the list grew (the first version of this script
# measured an older vocabulary than the one in tools/fetch.py). Imported the way the tests do it —
# `tools/` is put on the path and the module imported by name — because this box has another package
# called `tools` on the interpreter's path and `import tools.fetch` finds that one instead.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fetch as _fetch  # noqa: E402

TERMS = _fetch.GENERAL_TERMS


def pattern(term: str) -> re.Pattern:
    body = re.escape(term).replace(r"\*", r"\w*")
    return re.compile(rf"(?<![\w-]){body}(?![\w-])", re.I)


PATTERNS = [(term, pattern(term)) for term in TERMS]


def hits(text: str) -> list[str]:
    return [term for term, regex in PATTERNS if regex.search(text or "")]


def titles(url: str, limit: int = 20) -> list[str]:
    cmd = ["curl", "-sSL", "--max-time", "30", "--proto", "=http,https", "--compressed", "-A",
           "upsy.si audit", *TOR, url]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=45).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for block in re.findall(r"<item\b[^>]*>([\s\S]*?)</item>", raw, re.I)[:limit]:
        match = re.search(r"<title[^>]*>([\s\S]*?)</title>", block, re.I)
        if match:
            text = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", match.group(1))
            out.append(re.sub(r"\s+", " ", text).strip())
    if out:
        return out
    for block in re.findall(r"<entry\b[^>]*>([\s\S]*?)</entry>", raw, re.I)[:limit]:
        match = re.search(r"<title[^>]*>([\s\S]*?)</title>", block, re.I)
        if match:
            out.append(re.sub(r"\s+", " ", match.group(1)).strip())
    return out


def main() -> int:
    data = json.loads(SOURCES.read_text())
    sources = data if isinstance(data, list) else data.get("sources", [])
    podcasts = [s for s in sources if (s.get("kind") == "podcast" or s.get("category") == "podcast")]
    rows = []
    for source in podcasts:
        gated = bool(source.get("require_title") or source.get("require"))
        sample = titles(source.get("url", ""))
        if not sample:
            rows.append((source.get("id", "?"), None, 0, gated, []))
            continue
        matched = [t for t in sample if hits(t)]
        rows.append((source.get("id", "?"), len(matched) / len(sample), len(sample), gated, matched[:3]))
    rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 1))
    if "--json" in sys.argv:
        print(json.dumps([{"id": r[0], "share": r[1], "sample": r[2], "gated": r[3]} for r in rows], indent=1))
        return 0
    print(f"{'show':<26} {'AI in headline':>14}  {'gate':<5} sample")
    for name, share, sample, gated, _ in rows:
        share_text = "unreadable" if share is None else f"{share * 100:.0f}%"
        print(f"{name[:26]:<26} {share_text:>14}  {'yes' if gated else '—':<5} {sample}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
