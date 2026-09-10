#!/usr/bin/env python3
"""
Refreshes Yahoo player news in news.json (yahooNews only -- nbcNews and
roster-stats.json are owned by other jobs and are never touched here).

Run this from anywhere; paths resolve relative to the repo root:

    python3 scripts/refresh_yahoo_news.py

Each run:
  1. Syncs news.json's player list against roster-stats.json (adds any
     rostered player missing from news.json as a stub with empty news
     arrays; never modifies or removes existing entries). roster-stats.json
     spans multiple teams -- both its top-level `players` array and each
     team's own `players` array (when present, vs. a `playersRef` pointer)
     are unioned by id.
  2. Fetches fresh headlines for only the BATCH_SIZE players whose
     yahooNews is most stale (ranked by max fetchedAt, ascending -- a
     player with no yahooNews items ranks first). This runs twice daily,
     so BATCH_SIZE needs to comfortably cover the full roster at least
     once a day; bump it if the roster grows past what two runs can cover.
  3. Purges stale/overflow yahooNews items for EVERY player in news.json,
     not just the batch -- this runs unconditionally on every run so that
     items don't linger past their 2-day freshness window just because
     their player wasn't re-fetched that run.

Freshness is judged by each item's ESTIMATED PUBLISH TIME, not by
fetchedAt: fetchedAt is set once, when an item is first captured, and is
never touched again even if the same href is seen on a later run (an old
article re-appearing high on the page must not reset its clock). The
estimated publish time is instead derived as
fetchedAt_at_first_capture - offset_parsed_from_the_when_field_at_that_same_capture_time,
so a persistently-listed old article still ages out on schedule.
"""
import calendar
import json
import re
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
NEWS_PATH = REPO_ROOT / "news.json"
ROSTER_PATH = REPO_ROOT / "roster-stats.json"
BATCH_SIZE = 30
TWO_DAYS_MS = 2 * 24 * 60 * 60 * 1000
DEFAULT_IMG = "https://s.yimg.com/dh/ap/default/140828/silhouette@2x.png"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

HEADERS = {"User-Agent": UA}


def now_ms():
    return int(time.time() * 1000)


def unescape(s):
    if s is None:
        return s
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("\\/", "/")
    s = s.replace('\\"', '"')
    s = s.replace("\\n", " ")
    s = s.replace("\\\\", "\\")
    return s


def parse_when_offset_ms(when):
    """Parse strings like '59m ago', '2h ago', '1d ago' into an offset in ms.
    Returns None if unparseable."""
    if not when or not isinstance(when, str):
        return None
    m = re.match(r"^\s*(\d+)\s*([mhd])\s*ago\s*$", when.strip(), re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "m":
        return n * 60 * 1000
    if unit == "h":
        return n * 60 * 60 * 1000
    if unit == "d":
        return n * 24 * 60 * 60 * 1000
    return None


def format_when(delta_ms):
    """Format a millisecond delta into Yahoo-style relative time text."""
    if delta_ms < 0:
        delta_ms = 0
    minutes = delta_ms // (60 * 1000)
    hours = delta_ms // (60 * 60 * 1000)
    days = delta_ms // (24 * 60 * 60 * 1000)
    if hours < 1:
        return f"{max(1, int(minutes))}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(days)}d ago"


def estimated_published_at(fetched_at, when):
    """Estimate publish time from a persisted fetchedAt + when pair.
    Returns (estimate_ms_or_None, was_parseable)."""
    offset = parse_when_offset_ms(when)
    if offset is None:
        return None, False
    return fetched_at - offset, True


def fetch_player_news_html(player_id):
    url = f"https://sports.yahoo.com/mlb/players/{player_id}/news/"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_news_items(html, capture_time_ms):
    """Parse CarmotData blobs out of the page, return list of dicts with
    t, s, by, href, published_at_ms (or None)."""
    items = []
    seen_hrefs = set()
    parts = html.split('\\"CarmotData\\"')
    for chunk in parts[1:]:
        url_m = re.search(
            r'\\"clickThroughUrl\\":\{\\"__typename\\":\\"CarmotUrlInfo\\",\\"url\\":\\"([^\\]*)\\"',
            chunk,
        )
        if not url_m:
            continue
        href = unescape(url_m.group(1))
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        title_m = re.search(r'\\"title\\":\\"((?:[^\\]|\\\\.)*?)\\"', chunk)
        if not title_m:
            continue
        title = unescape(title_m.group(1))

        pub_m = re.search(r'\\"pubDate\\":\\"([^\\]*)\\"', chunk)
        published_at_ms = None
        if pub_m:
            try:
                ts = pub_m.group(1)
                # Format: 2026-07-11T22:32:02.000Z (always UTC)
                struct = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                published_at_ms = calendar.timegm(struct) * 1000
            except Exception:
                published_at_ms = None

        summary_m = re.search(r'\\"summary\\":\\"((?:[^\\]|\\\\.)*?)\\"', chunk)
        summary = unescape(summary_m.group(1)) if summary_m else None

        author_m = re.search(
            r'\\"author\\":\{\\"__typename\\":\\"CarmotAuthor\\",\\"displayName\\":\\"((?:[^\\]|\\\\.)*?)\\"',
            chunk,
        )
        author = unescape(author_m.group(1)) if author_m else None

        items.append(
            {
                "t": title,
                "s": summary,
                "by": author,
                "href": href,
                "published_at_ms": published_at_ms,
            }
        )
    return items


def fetch_player_image(player_id):
    url = f"https://sports.yahoo.com/mlb/players/{player_id}/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        m = re.search(
            r"s\.yimg\.com/xe/i/us/sp/v/mlb_cutout/players_l/[a-zA-Z0-9./_-]*\.png",
            resp.text,
        )
        if m:
            return "https://" + m.group(0)
    except Exception:
        pass
    return DEFAULT_IMG


def build_roster_id_set(roster):
    ids = {}
    for p in roster.get("players", []):
        ids[p["id"]] = p
    for t in roster.get("teams", []):
        if "players" in t:
            for p in t["players"]:
                ids[p["id"]] = p
    return ids


def purge_player_news(player, capture_time_ms, warnings):
    """Drop items older than 2 days (by estimated publish time) and cap
    the array at the 3 most recent. Mutates player in place. Returns
    (dropped_stale_count, capped_count)."""
    pname = player.get("name")
    pid = player.get("id")
    existing = player.get("yahooNews") or []

    kept = []
    dropped_stale = 0
    for item in existing:
        fa = item.get("fetchedAt")
        when = item.get("when")
        if fa is None:
            # No fetchedAt at all — cannot evaluate age; keep conservatively.
            kept.append((item, None))
            continue
        est_pub, parseable = estimated_published_at(fa, when)
        if not parseable:
            warnings.append(
                f"{pname} ({pid}): unparseable when='{when}' on existing item, kept conservatively"
            )
            kept.append((item, None))
            continue
        if capture_time_ms - est_pub > TWO_DAYS_MS:
            dropped_stale += 1
            continue
        kept.append((item, est_pub))

    # --- CAP at 3 items, keep most recent estimatedPublishedAt ---
    before_cap = len(kept)
    # items with unknown est_pub (None) sort last (treated as oldest/unknown)
    kept.sort(key=lambda x: (x[1] is None, -(x[1] or 0)))
    capped_count = max(0, before_cap - 3)
    final_items = [x[0] for x in kept[:3]]

    if final_items != existing:
        player["yahooNews"] = final_items

    return dropped_stale, capped_count


def main():
    with open(NEWS_PATH, encoding="utf-8") as f:
        news = json.load(f)
    with open(ROSTER_PATH, encoding="utf-8") as f:
        roster = json.load(f)

    warnings = []
    onboarded = []

    # --- ROSTER SYNC ---
    roster_players = build_roster_id_set(roster)
    existing_ids = {p["id"] for p in news["players"]}
    missing_ids = [pid for pid in roster_players if pid not in existing_ids]

    for pid in missing_ids:
        rp = roster_players[pid]
        img = fetch_player_image(pid)
        stub = {
            "id": pid,
            "name": rp.get("name"),
            "img": img,
            "nbcUrl": None,
            "yahooNews": [],
            "nbcNews": [],
        }
        news["players"].append(stub)
        onboarded.append(rp.get("name"))

    total_players = len(news["players"])
    changed = bool(onboarded)

    # --- BATCH SELECTION ---
    def last_refreshed(p):
        yn = p.get("yahooNews") or []
        vals = [item.get("fetchedAt") for item in yn if item.get("fetchedAt")]
        return max(vals) if vals else 0

    ranked = sorted(news["players"], key=last_refreshed)
    batch = ranked[:BATCH_SIZE]

    summary_lines = []

    # --- FETCH: only the batch of stalest players gets new headlines pulled ---
    for player in batch:
        pid = player["id"]
        pname = player["name"]
        capture_time_ms = now_ms()
        existing_hrefs = {item["href"] for item in (player.get("yahooNews") or [])}

        added_count = 0
        added_ages = []
        skipped_stale_new = 0

        try:
            time.sleep(0.3)
            html = fetch_player_news_html(pid)
            parsed_items = parse_news_items(html, capture_time_ms)

            new_entries = []
            for item in parsed_items:
                if item["href"] in existing_hrefs:
                    continue  # already known; never touch fetchedAt
                published_at_ms = item["published_at_ms"]
                if published_at_ms is not None:
                    age_ms = capture_time_ms - published_at_ms
                    when_str = format_when(age_ms)
                    est_pub = published_at_ms
                else:
                    # No pubDate found; treat as brand new (age 0) — conservative,
                    # cannot determine offset another way from this scrape.
                    when_str = "0m ago"
                    est_pub = capture_time_ms

                if capture_time_ms - est_pub > TWO_DAYS_MS:
                    skipped_stale_new += 1
                    continue

                entry = {
                    "t": item["t"],
                    "s": item["s"] if item["s"] else "",
                    "by": item["by"] if item["by"] else "",
                    "when": when_str,
                    "href": item["href"],
                    "fetchedAt": capture_time_ms,
                }
                new_entries.append(entry)
                added_count += 1
                added_ages.append(when_str)

            if new_entries:
                player["yahooNews"] = new_entries + (player.get("yahooNews") or [])
                changed = True

        except Exception as e:
            warnings.append(f"{pname} ({pid}): fetch/parse failed: {e}")

        if added_count or skipped_stale_new:
            summary_lines.append(
                f"{pname}: +{added_count} new ({', '.join(added_ages) if added_ages else '-'}), "
                f"skipped_stale_new={skipped_stale_new}"
            )

    # --- PURGE: stale/overflow pruning runs for EVERY player, every run ---
    purge_capture_time_ms = now_ms()
    purge_lines = []
    for player in news["players"]:
        dropped_stale, capped_count = purge_player_news(player, purge_capture_time_ms, warnings)
        if dropped_stale or capped_count:
            changed = True
            purge_lines.append(
                f"{player['name']}: dropped_stale={dropped_stale}, capped={capped_count}"
            )

    if changed:
        ms = now_ms()
        news["lastUpdatedNews"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}Z"
        with open(NEWS_PATH, "w", encoding="utf-8") as f:
            json.dump(news, f, indent=2, ensure_ascii=False)
            f.write("\n")

    print("=== SUMMARY ===")
    print(f"Total players in news.json: {total_players}")
    print(f"Players onboarded via roster sync: {len(onboarded)}" + (f" -> {', '.join(onboarded)}" if onboarded else ""))
    print(f"Players processed this batch (fetch): {len(batch)}")
    print(f"Players purged (prune/cap, all players every run): {len(purge_lines)} of {total_players}")
    print(f"File changed: {changed}")
    if summary_lines:
        print("Fetch changes (batch only):")
        for line in summary_lines:
            print(f"  - {line}")
    else:
        print("No fetch changes (no new items added in this batch).")
    if purge_lines:
        print("Purge changes (all players):")
        for line in purge_lines:
            print(f"  - {line}")
    else:
        print("No purge changes (nothing stale or over the 3-item cap).")
    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
