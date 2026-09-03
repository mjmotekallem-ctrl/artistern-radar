#!/usr/bin/env python3
"""Artistern Radar - build data.json from the source registry in feeds_config.json.

Design notes
------------
* Every feed is fetched in isolation. A feed that times out, 403s or returns
  garbage is recorded in the ``errors`` array and never aborts the run.
* Items are kept for a 30-day rolling window. Most sources here publish weekly
  or monthly, so a 24h window would leave the page empty most days.
* Each feed is capped so a daily publisher (The Art Newspaper) cannot bury a
  monthly one (Barjeel).
* Undated items are kept only if the feed gives us nothing dated, and are
  sorted last - some gallery feeds omit pubDate entirely.
"""

from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

import feedparser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
CONFIG_PATH = os.path.join(HERE, "feeds_config.json")
OUTPUT_PATH = os.path.join(REPO_ROOT, "data.json")

# Defaults; both are overridable from feeds_config.json ("window_days",
# "max_items_per_feed") so the cadence can be tuned without touching Python.
WINDOW_DAYS = 180
MAX_ITEMS_PER_FEED = 8
FETCH_TIMEOUT = 30
RETRIES = 2
SUMMARY_CHARS = 320

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 (+ArtisternRadar/1.0)"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8,fa;q=0.7,fr;q=0.7",
}

# Items mentioning any of these get a "MENA" flag in the UI. The wide news
# sources (Artnet, ArtNews, Hyperallergic) publish mostly Western art news, so
# without this the regional signal is buried.
REGION_TERMS = [
    "middle east", "mena", "arab", "gulf", "levant", "north africa", "maghreb",
    "iran", "iranian", "tehran", "persian", "farsi",
    "uae", "emirat", "dubai", "abu dhabi", "sharjah", "ajman",
    "saudi", "riyadh", "jeddah", "diriyah", "alula", "althra", "ithra",
    "qatar", "doha", "kuwait", "bahrain", "manama", "oman", "muscat",
    "egypt", "cairo", "alexandria", "lebanon", "beirut", "syria", "damascus",
    "aleppo", "jordan", "amman", "iraq", "baghdad", "basra", "palestin",
    "gaza", "west bank", "jerusalem", "ramallah", "yemen", "sudan",
    "morocc", "marrakech", "casablanca", "rabat", "tangier", "fez",
    "tunisia", "tunis", "algeria", "algiers", "libya", "tripoli",
    "turkey", "turkish", "istanbul", "ottoman", "kurdish",
    "islamic art", "orientalis", "calligraph", "sharjah biennial",
    "art dubai", "islamic arts biennale", "mathaf", "barjeel", "ithra",
]
REGION_RE = re.compile("|".join(re.escape(t) for t in REGION_TERMS), re.I)

# Arabic block covers Arabic + Persian; also Hebrew and Cyrillic for safety.
NON_LATIN_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# Jetpack/WordPress append "The post <title> appeared first on <site>." to every
# summary. It is pure boilerplate and eats the useful part of the excerpt.
BOILERPLATE_RE = re.compile(
    r"\s*(?:The post\s+.*?\s+appeared first on\s+.*?\.?"
    r"|Continue reading\s+.*?(?:at|on)\s+.*?\.?"
    r"|(?:Read|The article)\s+.*?\s+(?:first )?appeared\s+.*?\.?)\s*$",
    re.I | re.S,
)

# Some feeds (a whole-magazine feed where only one desk is relevant) need a
# topical gate. Configured per feed via "filter_terms" in feeds_config.json.
ART_FILTER_PRESETS = {
    # Deliberately strict. Loose terms ("design", "culture", "collection",
    # "craft") let luxury-brand PR through on a whole-magazine feed, which is
    # exactly what this gate exists to keep out.
    "art": [
        "artist", "artists", "artwork", "artworks", "gallery", "galleries",
        "museum", "museums", "exhibition", "exhibitions", "biennale", "biennial",
        "sculpture", "sculptor", "painting", "paintings", "painter",
        "curator", "curators", "curated by", "curatorial",
        "auction", "art fair", "art week", "art basel", "art dubai",
        "contemporary art", "modern art", "fine art", "islamic art", "art world",
        "art collection", "art collector", "art scene", "art prize", "art award",
        "calligraphy", "retrospective", "photographer", "photography",
        "printmaking", "ceramics", "art history", "art historian", "atelier",
        "artistic director", "commissioned work", "public art", "art space",
    ],
}


def log(msg: str) -> None:
    print(msg, flush=True)


def clean_text(raw: str, limit: int = SUMMARY_CHARS) -> str:
    """Strip markup/entities from a feed summary and truncate on a word boundary."""
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    text = BOILERPLATE_RE.sub("", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"


def fetch_bytes(url: str) -> bytes:
    """Fetch a URL with a browser-ish UA. Raises on the final failed attempt.

    A plain feedparser.parse(url) sends a urllib UA that a lot of these hosts
    reject outright, so we do the HTTP ourselves and hand feedparser bytes.
    """
    last_err: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=REQUEST_HEADERS)
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
            last_err = exc
            if attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
    raise last_err  # type: ignore[misc]


def describe_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"connection failed: {exc.reason}"
    if isinstance(exc, socket.timeout):
        return f"timed out after {FETCH_TIMEOUT}s"
    return f"{type(exc).__name__}: {exc}"


def build_filter(feed_cfg: dict):
    """Return a compiled matcher for a feed's topical gate, or None."""
    terms = feed_cfg.get("filter_terms")
    if isinstance(terms, str):
        terms = ART_FILTER_PRESETS.get(terms)
    if not terms:
        return None
    return re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")", re.I
    )


def entry_datetime(entry) -> dt.datetime | None:
    """Best-effort UTC timestamp for a feed entry."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return dt.datetime.fromtimestamp(calendar.timegm(parsed), dt.timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
    return None


def build_item(entry, feed_cfg: dict) -> dict | None:
    link = (entry.get("link") or "").strip()
    title = clean_text(entry.get("title") or "", 240) or "(untitled)"
    if not link:
        return None

    summary = ""
    for key in ("summary", "description"):
        if entry.get(key):
            summary = clean_text(entry.get(key))
            break
    if not summary and entry.get("content"):
        try:
            summary = clean_text(entry["content"][0].get("value", ""))
        except (IndexError, AttributeError, TypeError):
            summary = ""

    when = entry_datetime(entry)
    blob = f"{title} {summary}"
    return {
        "title": title,
        "link": link,
        "summary": summary,
        "published": when.isoformat().replace("+00:00", "Z") if when else None,
        "published_ts": when.timestamp() if when else None,
        "source": feed_cfg["name"],
        "category": feed_cfg["category"],
        "mena": bool(REGION_RE.search(blob)),
        "needs_translation": bool(
            feed_cfg.get("lang") in ("fa", "ar") or NON_LATIN_RE.search(blob)
        ),
    }


def process_feed(feed_cfg: dict, cutoff: dt.datetime):
    """Return (items, error_or_None, stat, quiet_item_or_None) for one feed."""
    name, url = feed_cfg["name"], feed_cfg["url"]
    stat = {"source": name, "category": feed_cfg["category"], "fetched": 0, "kept": 0}

    try:
        raw = fetch_bytes(url)
    except Exception as exc:  # noqa: BLE001
        return [], {
            "source": name, "category": feed_cfg["category"], "url": url,
            "stage": "fetch", "error": describe_error(exc),
        }, stat, None

    try:
        parsed = feedparser.parse(raw)
    except Exception as exc:  # noqa: BLE001
        return [], {
            "source": name, "category": feed_cfg["category"], "url": url,
            "stage": "parse", "error": describe_error(exc),
        }, stat, None

    if not parsed.entries:
        detail = "feed returned zero entries"
        if getattr(parsed, "bozo", 0) and getattr(parsed, "bozo_exception", None):
            detail += f" ({type(parsed.bozo_exception).__name__}: {parsed.bozo_exception})"
        return [], {
            "source": name, "category": feed_cfg["category"], "url": url,
            "stage": "parse", "error": detail[:300],
        }, stat, None

    stat["fetched"] = len(parsed.entries)

    topical = build_filter(feed_cfg)
    dated, undated, filtered_out = [], [], 0
    for entry in parsed.entries:
        item = build_item(entry, feed_cfg)
        if item is None:
            continue
        if topical and not topical.search(f"{item['title']} {item['summary']}"):
            filtered_out += 1
            continue
        if item["published_ts"] is None:
            undated.append(item)
        elif item["published_ts"] >= cutoff.timestamp():
            dated.append(item)

    dated.sort(key=lambda i: i["published_ts"], reverse=True)
    items = dated[:MAX_ITEMS_PER_FEED]

    # Only fall back to undated items when the feed gave us nothing dated at
    # all, so a feed with real dates is never padded with dateless noise.
    if not items and undated:
        items = undated[:MAX_ITEMS_PER_FEED]

    stat["kept"] = len(items)
    if filtered_out:
        stat["filtered_out"] = filtered_out

    # A quiet feed still gets its single newest item preserved, so a source
    # that publishes every few months is visible (clearly dated and demoted)
    # instead of vanishing from the page entirely.
    quiet = None
    error = None
    if not items:
        # Re-scan only entries that passed the topical gate, so a feed emptied
        # by filtering is never described as merely "stale".
        newest = None
        for entry in parsed.entries:
            candidate = build_item(entry, feed_cfg)
            if candidate is None or candidate["published_ts"] is None:
                continue
            if topical and not topical.search(f"{candidate['title']} {candidate['summary']}"):
                continue
            if newest is None or candidate["published_ts"] > newest["published_ts"]:
                newest = candidate
        if newest:
            age = int((dt.datetime.now(dt.timezone.utc).timestamp()
                       - newest["published_ts"]) // 86400)
            newest["age_days"] = age
            quiet = newest
        error = {
            "source": name, "category": feed_cfg["category"], "url": url,
            "stage": "window",
            "error": (
                f"feed OK ({len(parsed.entries)} entries) but nothing "
                + ("on-topic " if filtered_out else "")
                + f"published in the last {WINDOW_DAYS} days"
                + (f"; newest is {quiet['age_days']} days old" if quiet
                   else f"; all {filtered_out} entries filtered out as off-topic"
                        if filtered_out else "")
            ),
            "severity": "info",
        }
    return items, error, stat, quiet


def write_step_summary(payload: dict) -> None:
    """Render the run's feed health into the GitHub Actions job summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    t = payload["totals"]
    errors = payload["errors"]
    fails = [e for e in errors if e.get("severity") != "info"]
    quiet = [e for e in errors if e.get("severity") == "info"]

    out = [
        "## Artistern Radar - feed health",
        "",
        f"**{t['items']} items** from **{t['feeds_with_items']}/{t['feeds_configured']}** feeds "
        f"| {t['feeds_failed']} failing | {t['feeds_quiet']} quiet "
        f"| {t['manual_sources']} manual sources",
        "",
    ]
    if fails:
        out += ["### Failing feeds - these need a fix", "",
                "| Source | Error | URL |", "|---|---|---|"]
        out += [f"| {e['source']} | {e['error'][:140]} | `{e['url']}` |" for e in fails]
        out.append("")
    else:
        out += ["All configured feeds responded.", ""]
    if quiet:
        out += ["<details><summary>Quiet feeds - reachable, nothing new in the window</summary>",
                "", "| Source | Detail |", "|---|---|"]
        out += [f"| {e['source']} | {e['error'][:140]} |" for e in quiet]
        out += ["", "</details>", ""]

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def main() -> int:
    global WINDOW_DAYS, MAX_ITEMS_PER_FEED

    with open(CONFIG_PATH, encoding="utf-8") as fh:
        config = json.load(fh)

    WINDOW_DAYS = int(config.get("window_days", WINDOW_DAYS))
    MAX_ITEMS_PER_FEED = int(config.get("max_items_per_feed", MAX_ITEMS_PER_FEED))

    feeds = config["feeds"]
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=WINDOW_DAYS)

    log(f"Artistern Radar - {len(feeds)} feeds, window {WINDOW_DAYS}d, "
        f"cap {MAX_ITEMS_PER_FEED}/feed")
    log("-" * 78)

    by_source: dict[str, list[dict]] = {}
    quiet_by_source: dict[str, dict] = {}
    errors: list[dict] = []
    stats: list[dict] = []

    for feed_cfg in feeds:
        items, error, stat, quiet = process_feed(feed_cfg, cutoff)
        stats.append(stat)
        if quiet:
            quiet_by_source[feed_cfg["name"]] = quiet
        if error:
            errors.append(error)
            log(f"  {'INFO' if error.get('severity') == 'info' else 'FAIL'}  "
                f"{feed_cfg['name']:<38} {error['error'][:80]}")
        if items:
            by_source[feed_cfg["name"]] = items
            note = f" ({stat['filtered_out']} off-topic dropped)" if stat.get("filtered_out") else ""
            log(f"  ok    {feed_cfg['name']:<38} {len(items)} item(s) "
                f"of {stat['fetched']}{note}")

    # Group by category, then by source, newest first within each source.
    categories = []
    for category in config["category_order"]:
        sources = []
        for feed_cfg in feeds:
            if feed_cfg["category"] != category:
                continue
            items = by_source.get(feed_cfg["name"])
            if not items:
                continue
            newest = max((i["published_ts"] or 0) for i in items)
            sources.append({
                "name": feed_cfg["name"],
                "site": feed_cfg.get("site") or feed_cfg["url"],
                "feed_url": feed_cfg["url"],
                "lang": feed_cfg.get("lang", "en"),
                "newest_ts": newest,
                "items": items,
            })
        sources.sort(key=lambda s: s["newest_ts"], reverse=True)

        quiet_sources = []
        for feed_cfg in feeds:
            if feed_cfg["category"] != category:
                continue
            quiet = quiet_by_source.get(feed_cfg["name"])
            if not quiet:
                continue
            quiet_sources.append({
                "name": feed_cfg["name"],
                "site": feed_cfg.get("site") or feed_cfg["url"],
                "lang": feed_cfg.get("lang", "en"),
                "age_days": quiet["age_days"],
                "item": quiet,
            })
        quiet_sources.sort(key=lambda s: s["age_days"])

        manual = [m for m in config.get("manual_sources", []) if m["category"] == category]
        if sources or manual or quiet_sources:
            categories.append({
                "name": category,
                "item_count": sum(len(s["items"]) for s in sources),
                "sources": sources,
                "quiet_sources": quiet_sources,
                "manual_sources": manual,
            })

    total_items = sum(c["item_count"] for c in categories)
    payload = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "window_days": WINDOW_DAYS,
        "max_items_per_feed": MAX_ITEMS_PER_FEED,
        "totals": {
            "feeds_configured": len(feeds),
            "feeds_with_items": len(by_source),
            "feeds_failed": len([e for e in errors if e.get("severity") != "info"]),
            "feeds_quiet": len([e for e in errors if e.get("severity") == "info"]),
            "manual_sources": len(config.get("manual_sources", [])),
            "items": total_items,
        },
        "categories": categories,
        "errors": errors,
        "stats": stats,
        "social": config.get("social", {}),
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    write_step_summary(payload)

    t = payload["totals"]
    log("-" * 78)
    log(f"{total_items} items from {t['feeds_with_items']}/{t['feeds_configured']} feeds "
        f"| {t['feeds_failed']} failed, {t['feeds_quiet']} quiet "
        f"| {t['manual_sources']} manual sources")
    log(f"wrote {OUTPUT_PATH}")
    # Always exit 0: a broken feed is data for the status log, not a build failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
