#!/usr/bin/env python3
"""Screenshot the stories whose feeds carry no picture.

Roughly half of the sources publish no image at all, and a card with a blank where a picture should
be reads as broken rather than sparse. A headless browser can shoot the story's own page instead.

Three things make this cheap enough to run every ten minutes:

* Chrome is preinstalled on the GitHub runner images, so there is nothing to download and nothing to
  install. Anywhere without a browser the script exits quietly and the site builds exactly as before.
* Each shot is written to ``data/shots/<digest of the story url>.png``, which ``data/shots`` rides the
  same ``actions/cache`` entry as the downloaded pictures. A page is therefore shot once and then
  reused, so a steady-state run shoots only the handful of stories that are new.
* A page that refuses a headless visit, or renders to nearly nothing, is remembered in
  ``data/shots.json`` and not tried again for a week - so a hostile site costs one attempt, not one
  attempt every ten minutes.

The builder finds the shots by digest rather than by bookkeeping in ``news.json``: nothing has to be
kept in sync, and a shot that exists is used whether or not this script ran recently.

Only the top of the page is worth shooting. Nothing below the fold is rendered by the site's initial
view, so ``--limit`` bounds the work to the stories a reader can actually see.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
NEWS = ROOT / "data" / "news.json"
SHOTS = ROOT / "data" / "shots"
SHOT_STATE = ROOT / "data" / "shots.json"

BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome",
            "msedge")
# A real browser string: several sites answer a "HeadlessChrome" agent with a challenge page.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
# A blank or challenged render compresses to almost nothing; a real page never does.
MIN_BYTES = 6_000
RETRY_AFTER_DAYS = 7


def digest(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


def find_browser() -> str:
    """The runner's Chrome, or nothing at all."""
    explicit = os.environ.get("UPSY_CHROME")
    if explicit and pathlib.Path(explicit).exists():
        return explicit
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    return ""


def existing_shot(url: str) -> pathlib.Path | None:
    return next((p for p in SHOTS.glob(digest(url) + ".*") if p.stat().st_size >= MIN_BYTES), None)


def unpictured_urls() -> set:
    """Stories whose own picture could not be kept by the build, so a card has nothing to show.

    The builder drops a picture it cannot mirror, shrink or validate, and a story with an unmirrorable
    image used to look "pictured" to this step and was skipped - which is how a card ended up with an
    empty placeholder and no screenshot either. The builder writes what it could not keep, and those
    stories are candidates here.
    """
    path = NEWS.parent / "pictures.json"
    if not path.exists():
        return set()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001 - a missing list is not a failure
        return set()
    return set(record.get("unpictured") or [])


# What a site answers when it does not mean to show you the page. The render is not blank, so the size
# gate passes it, and the card ends up illustrated with "Sorry, you have been blocked" - measured on
# techmeme.com, whose block page became two cards' picture before anyone noticed.
BLOCK_MARKERS = (
    "you have been blocked", "you are unable to access", "access denied", "request blocked",
    "request unsuccessful", "unusual traffic from your", "are you a robot", "verify you are human",
    "checking your browser", "attention required", "complete the captcha", "solve the captcha",
    "captcha verification", "enable javascript and cookies to continue", "403 forbidden",
    "this page is not available in your", "access to this page has been denied",
)


def blocked_reason(text: str) -> str:
    """The phrase that says this is a refusal page rather than an article, if there is one.

    Read against the page's *visible* words: every site in the world ships a `grecaptcha-badge` class and
    a "captcha" string in its stylesheet, and matching the raw html refused a perfectly good Alignment
    Forum article on the strength of a CSS rule.
    """
    if not text:
        return ""
    visible = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    visible = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible)).lower()
    for marker in BLOCK_MARKERS:
        if marker in visible:
            return marker
    return ""


def probe(url: str, timeout: int = 12) -> tuple[int, str]:
    """Ask for the page before rendering it: a site that blocks us must cost one request, not a picture."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                  "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read(250_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, "http %s" % exc.code
    except Exception as exc:                           # noqa: BLE001 - a failed probe is not fatal
        return 0, type(exc).__name__
    return status, blocked_reason(body)


def select(items: list[dict], shots: dict, failures: dict, limit: int, most: int,
           today: dt.date, force: bool = False, unpictured: set | None = None) -> list[dict]:
    """The stories that need a picture: no image of their own, no shot yet, not recently refused.

    Pure on purpose - it is the part worth testing, and it is the part that decides the run's cost.
    """
    unpictured = unpictured or set()
    picked: list[dict] = []
    for item in items[:limit]:
        url = item.get("url") or ""
        if item.get("image") and url not in unpictured:
            continue                      # the feed or the article page already gave us a picture
        if not url.startswith("http"):
            continue
        record = shots.get(url) or {}
        if record.get("file") and existing_shot(url) and not force:
            continue                      # already shot in an earlier run
        failed = failures.get(url)
        if failed and not force:
            try:
                when = dt.date.fromisoformat(failed)
            except ValueError:
                when = today
            if (today - when).days < RETRY_AFTER_DAYS:
                continue                  # it refused us recently; do not keep knocking
        picked.append(item)
        if len(picked) >= most:
            break
    return picked


def record_status(shots: dict, failures: dict, status: dict) -> None:
    """Keep the last run's outcome in the bookkeeping file so the build can publish it.

    The workflow logs are not readable from outside, and a step under continue-on-error that quietly
    does nothing looks exactly like a step that was never added. The published page reports it instead.
    """
    status["date"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    status["failures"] = len(failures)
    shots[".status"] = status


def shoot(browser: str, url: str, out: pathlib.Path, width: int, height: int, wait_ms: int,
          timeout: int) -> tuple[bool, str]:
    """Render one page to a PNG. Returns (ok, reason)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    command = [
        browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--hide-scrollbars", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--force-device-scale-factor=1", f"--window-size={width},{height}",
        f"--user-agent={USER_AGENT}", f"--virtual-time-budget={wait_ms}",
        f"--screenshot={out}", url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, f"could not run the browser ({type(exc).__name__})"
    if not out.exists():
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return False, (detail[-1][:90] if detail else f"no file (exit {result.returncode})")
    size = out.stat().st_size
    if size < MIN_BYTES:
        out.unlink(missing_ok=True)
        return False, f"blank or refused render ({size} bytes)"
    return True, ""


def save(shots: dict) -> None:
    SHOT_STATE.parent.mkdir(parents=True, exist_ok=True)
    SHOT_STATE.write_text(json.dumps(shots, ensure_ascii=False, indent=0), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max", type=int, default=60, help="how many to shoot in this run")
    parser.add_argument("--limit", type=int, default=160, help="only stories this high on the page")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=600)
    parser.add_argument("--wait-ms", type=int, default=6000, help="virtual time for JS and images")
    parser.add_argument("--timeout", type=int, default=25, help="seconds per page")
    parser.add_argument("--budget", type=int, default=420, help="seconds for the whole run")
    parser.add_argument("--force", action="store_true", help="re-shoot anything already shot")
    parser.add_argument("--dry-run", action="store_true", help="list what would be shot, shoot nothing")
    parser.add_argument("--verify", type=int, default=40,
                        help="re-check this many shots already taken, dropping block pages and repeats")
    args = parser.parse_args()

    if not NEWS.exists():
        print("no data/news.json: run tools/fetch.py first", file=sys.stderr)
        return 0
    items = json.loads(NEWS.read_text(encoding="utf-8")).get("items", [])
    shots = json.loads(SHOT_STATE.read_text(encoding="utf-8")) if SHOT_STATE.exists() else {}
    failures: dict = shots.pop(".failures", {}) if isinstance(shots.get(".failures"), dict) else {}

    today = dt.datetime.now(dt.timezone.utc).date()

    # A shot already taken can be a block page - the render is not blank, so nothing refused it. The page
    # is asked for again here: if it now says we are blocked, the picture goes and the url joins the
    # refusals. Two shots with identical bytes are the same page by definition, and one of them is a lie.
    if args.verify and not args.dry_run:
        checked = dropped = 0
        for url, record in list(shots.items()):
            if url.startswith(".") or not isinstance(record, dict) or not record.get("file"):
                continue
            path = SHOTS / record["file"]
            if not path.exists() or checked >= args.verify:
                continue
            checked += 1
            _status, reason = probe(url)
            if reason:
                path.unlink(missing_ok=True)
                shots.pop(url, None)
                failures[url] = today.isoformat()
                dropped += 1
                print(f"   dropped a shot of {reason} :: {url[:64]}")
        seen: dict[str, str] = {}
        for url, record in list(shots.items()):
            if url.startswith(".") or not isinstance(record, dict) or not record.get("file"):
                continue
            path = SHOTS / record["file"]
            if not path.exists():
                continue
            mark = hashlib.sha1(path.read_bytes()).hexdigest()
            if mark in seen:
                path.unlink(missing_ok=True)
                shots.pop(url, None)
                failures[url] = today.isoformat()
                dropped += 1
                print(f"   dropped a repeated shot :: {url[:64]}")
            else:
                seen[mark] = url
        if checked or dropped:
            print(f"   verified {checked} shot(s), dropped {dropped}")
            save(shots)

    todo = select(items, shots, failures, args.limit, args.max, today, args.force, unpictured_urls())
    print(f"{len(items)} story(ies); {sum(1 for i in items if i.get('image'))} already have a picture; "
          f"{len(todo)} need a screenshot")

    if not SHOT_STATE.exists():
        save({})

    if args.dry_run:
        for item in todo[:20]:
            print(f"   would shoot: {item['url'][:100]}")
        if len(todo) > 20:
            print(f"   ... and {len(todo) - 20} more")
        return 0
    if not todo:
        return 0

    browser = find_browser()
    if not browser:
        # Not a failure: without a browser the site simply keeps the text-first cards it has.
        print("no browser on this machine, so no screenshots this run")
        # Written before returning: this is the case the record exists for, and returning first is
        # why the page kept reporting "not run" for a run that had already decided.
        record_status(shots, failures, {"browser": "", "wanted": len(todo), "taken": 0,
                                        "refused": 0, "note": "no browser found"})
        save(shots)
        return 0
    print(f"   using {browser}")

    SHOTS.mkdir(parents=True, exist_ok=True)
    picture_digests: dict[str, str] = {}
    for existing in SHOTS.glob("*.png"):
        try:
            picture_digests[hashlib.sha1(existing.read_bytes()).hexdigest()] = existing.name
        except OSError:
            continue
    started = dt.datetime.now()
    taken = refused = 0
    for item in todo:
        if (dt.datetime.now() - started).total_seconds() > args.budget:
            print("   out of time for this run; the rest will be picked up next time")
            break
        url = item["url"]
        out = SHOTS / (digest(url) + ".png")
        status, refused_because = probe(url)
        if refused_because:
            failures[url] = today.isoformat()
            refused += 1
            print(f"   skip {item.get('sourceName', '?')[:22]:<22} {refused_because} :: {url[:60]}")
            continue
        ok, why = shoot(browser, url, out, args.width, args.height, args.wait_ms, args.timeout)
        if ok and out.exists():
            # Two stories cannot have the same picture. When they do, it is the site's block page or a
            # challenge screen rendered once per url - the bytes are identical because the page is.
            same = picture_digests.get(hashlib.sha1(out.read_bytes()).hexdigest())
            if same:
                out.unlink(missing_ok=True)
                failures[url] = today.isoformat()
                refused += 1
                print(f"   skip {item.get('sourceName', '?')[:22]:<22} the same page as another shot "
                      f":: {url[:50]}")
                continue
            picture_digests[hashlib.sha1(out.read_bytes()).hexdigest()] = url
        if ok:
            shots[url] = {"file": out.name, "date": today.isoformat(),
                          "source": item.get("sourceName", "")}
            failures.pop(url, None)
            taken += 1
            print(f"   shot {item.get('sourceName', '?')[:22]:<22} {url[:70]}")
        else:
            failures[url] = today.isoformat()
            refused += 1
            print(f"   skip {item.get('sourceName', '?')[:22]:<22} {why} :: {url[:60]}")

    shots[".failures"] = failures
    record_status(shots, failures, {"browser": browser, "wanted": len(todo), "taken": taken,
                                    "refused": refused,
                                    "note": "budget reached" if taken + refused < len(todo) else ""})
    save(shots)
    total = sum(1 for v in shots.values() if isinstance(v, dict) and v.get("file"))
    print(f"shot {taken}, refused {refused}, {total} shot(s) on file, "
          f"{len(failures)} page(s) on the no-go list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
