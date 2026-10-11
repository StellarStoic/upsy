#!/usr/bin/env python3
"""Write the upsy moments: the editor on top of the wire.

The rules in fetch.py only nominate candidates, because deciding that a machine did something
genuinely wild is a judgement call, not a keyword. This asks a model to make it, then writes
data/moments.json for the site and marks the chosen stories in data/news.json.

Credentials, in the order they are tried:
  1. GITHUB_TOKEN with `permissions: models: read` in the workflow - nothing to manage.
  2. UPSY_LLM_API_KEY against GitHub Models, or any OpenAI-compatible endpoint via
     UPSY_LLM_BASE_URL / UPSY_LLM_MODEL.
No credential, a refusal, or a broken reply is not a failure: the moments fall back to the ranked
candidates and the site still builds. That is deliberate - the wire must never depend on the editor.

    python3 tools/moments.py            # rewrites data/moments.json
    python3 tools/moments.py --force    # ignore the "recently written" guard
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEFAULT_BASE = "https://models.github.ai/inference"
DEFAULT_MODEL = "openai/gpt-4o-mini"
CANDIDATES = 60
MOMENTS = 5
REWRITE_AFTER = timedelta(hours=3)


def direct_network() -> None:
    if os.environ.get("UPSY_USE_PROXY") == "1":
        return
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(key, None)


def credentials() -> tuple[str, str, str, str]:
    """(token, base_url, model, label) - empty token means there is no editor tonight."""
    base = os.environ.get("UPSY_LLM_BASE_URL") or DEFAULT_BASE
    model = os.environ.get("UPSY_LLM_MODEL") or DEFAULT_MODEL
    if (os.environ.get("UPSY_LLM_API_KEY") or "").strip():
        return os.environ["UPSY_LLM_API_KEY"], base, model, f"{model} via key"
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"], base, model, f"{model} via workflow token"
    return "", base, model, ""


def candidates(items: list[dict], limit: int = CANDIDATES) -> list[dict]:
    """What the editor is allowed to choose from: recent, ranked, and actually about the topic."""
    nominated = [i for i in items if i["upsy"] and not i.get("promo")]
    scored = [i for i in items if not i.get("promo") and not i.get("question")]
    chosen: list[dict] = []
    for group in (nominated, scored):
        for item in group:
            if item not in chosen:
                chosen.append(item)
            if len(chosen) >= limit:
                break
    return chosen[:limit]


PROMPT = """You are the editor of upsy.si, a wire that watches superintelligence happen.

Below are candidate stories. Choose up to {moments} "upsy moments": cases where an AI system did
something absurd, uncanny, funny, alarming, or simply wild enough that the right reaction is a slow
"well, that happened". Prefer things that actually happened over opinion, speculation, and policy
arguments. If fewer than {moments} qualify, choose fewer - an empty rail is better than a padded one.

Then write a brief: two sentences on where superintelligence stood today, flat and specific.

Reply with JSON only, no prose around it, in exactly this shape:
{{"brief": "...", "moments": [{{"id": "<id from the list>", "why": "one sentence, at most 200 characters"}}]}}

The id must be copied exactly from the list. Do not invent ids, titles, or links.

Candidates:
{candidates}
"""


def build_prompt(items: list[dict]) -> str:
    lines = []
    for item in items:
        summary = re.sub(r"\s+", " ", item.get("summary") or "")[:200]
        lines.append(f"- id: {item['id']} | {item['sourceName']} | {item['title']}"
                     + (f" | {summary}" if summary else ""))
    return PROMPT.format(moments=MOMENTS, candidates="\n".join(lines))


def call_model(token: str, base: str, model: str, prompt: str) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You reply with JSON only. You never invent links."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 900,
    }).encode()
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "upsy.si/1.0 (+https://upsy.si/)"})
    with urllib.request.urlopen(request, timeout=90) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    return body["choices"][0]["message"]["content"]


def parse_reply(reply: str) -> dict:
    """Pull the JSON out of a reply that may be wrapped in prose or code fences."""
    text = reply.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def rules_engine(items: list[dict], now: datetime) -> dict:
    """The fallback editor. No model, no invention - it promotes the ranked candidates as they are."""
    picked = [i for i in items if i["upsy"]][:MOMENTS]
    if len(picked) < MOMENTS:
        for item in items:
            if len(picked) >= MOMENTS:
                break
            if item.get("promo") or item.get("question") or item in picked:
                continue
            if item.get("sourceCategory") in ("community", "aggregator") or item.get("breaking"):
                picked.append(item)
    return {
        "engine": "rules",
        "moments": [
            {"id": i["id"], "title": i["title"], "url": i["url"], "sourceName": i["sourceName"],
             "image": i.get("image", ""), "why": i.get("summary") or "Flagged by the stream - worth a look.",
             "published": i.get("published", "")}
            for i in picked[:MOMENTS]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="write moments even if the last set is fresh")
    args = parser.parse_args()

    direct_network()
    DATA.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    news_path, moments_path = DATA / "news.json", DATA / "moments.json"
    data = json.loads(news_path.read_text(encoding="utf-8"))
    items = data["items"]
    previous = json.loads(moments_path.read_text(encoding="utf-8")) if moments_path.exists() else {}

    if previous.get("engine") not in (None, "rules") and not args.force:
        try:
            written = datetime.fromisoformat(previous["generated"])
            top_now = [i["id"] for i in items[:5]]
            if now - written < REWRITE_AFTER and previous.get("topIds") == top_now:
                print(f"moments are {now - written} old and the top of the page has not moved; leaving them")
                return 0
        except Exception:
            pass

    token, base, model, label = credentials()
    moments: dict
    if token:
        try:
            reply = call_model(token, base, model, build_prompt(candidates(items)))
            parsed = parse_reply(reply)
            by_id = {i["id"]: i for i in items}
            resolved = []
            for moment in parsed.get("moments", []):
                item = by_id.get(str(moment.get("id", "")).strip())
                if not item:
                    continue  # the model invented an id: drop it rather than link nowhere
                resolved.append({
                    "id": item["id"], "title": item["title"], "url": item["url"],
                    "sourceName": item["sourceName"], "image": item.get("image", ""),
                    "published": item.get("published", ""),
                    "why": re.sub(r"\s+", " ", str(moment.get("why", "")))[:240],
                })
            moments = {"engine": label, "model": model,
                       "brief": re.sub(r"\s+", " ", str(parsed.get("brief", "")))[:400],
                       "moments": resolved}
            print(f"editor ({label}) returned {len(resolved)} moment(s)")
        except Exception as exc:  # noqa: BLE001 - the site must build whatever the model does
            print(f"editor unavailable ({type(exc).__name__}: {exc}); falling back to the rules")
            moments = rules_engine(items, now)
    else:
        print("no credential: nothing to write with, so the rules nominate instead")
        moments = rules_engine(items, now)

    moments["generated"] = now.isoformat(timespec="seconds")
    moments["topIds"] = [i["id"] for i in items[:5]]
    moments_path.write_text(json.dumps(moments, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # Mark the chosen stories so the stream and their own pages know why they were picked.
    picks = {m["id"]: m["why"] for m in moments["moments"]}
    for item in items:
        if item["id"] in picks:
            item["upsy"] = True
            item["upsyWhy"] = picks[item["id"]]
    data["counts"]["upsy"] = len([i for i in items if i["upsy"]])
    news_path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote data/moments.json ({moments['engine']}), marked {len(picks)} story(ies) as upsy moments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
