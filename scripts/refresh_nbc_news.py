#!/usr/bin/env python3
"""
Refreshes news.json's nbcNews field for the players whose NBC Sports news is
most stale, and syncs news.json's player list against roster-stats.json.
yahooNews is owned by a separate routine and is never touched here.

Run from the repo root:

    python3 scripts/refresh_nbc_news.py

Two things happen every run, but at different scopes:

1. Roster sync (all players): any id present in roster-stats.json (its
   top-level `players` array, plus each team's own `players` array when it
   has one -- a team with `playersRef` just points at the top-level array
   and is not double-counted) but missing from news.json gets a stub entry
   added ({id, name, img, nbcUrl, yahooNews: [], nbcNews: []}). Existing
   entries are never modified by this step.

2. Fetch new items (batch-scoped): only the BATCH_SIZE players whose
   nbcNews is most stale (lowest max fetchedAt; never-fetched players sort
   first) get their NBC Sports page fetched and parsed for new headlines.
   This is the expensive, rate-limit-sensitive part, so it's capped per
   run. With the roster at ~58 players and this routine run twice daily, a
   batch of 40 covers the full roster at least once per day -- if the
   roster grows enough that stops being true, the summary says so.

3. Prune + cap (ALL players, every run): independent of the batch above,
   every player's nbcNews is pruned of items whose own `date` field is
   more than STALE_DAYS before today, then capped at MAX_ITEMS most-recent.
   This runs globally so a player's stale headlines don't linger just
   because that player hasn't come up in the fetch-batch rotation yet.

fetchedAt is set ONCE, when an item is first captured, and is never
touched again -- staleness is judged solely by each item's own `date`
field (parsed as a calendar date), never by fetchedAt.
"""
import json
import re
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
NEWS_PATH = REPO_ROOT / "news.json"
ROSTER_PATH = REPO_ROOT / "roster-stats.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsRefreshBot/1.0)"}
TIMEOUT = 20
BATCH_SIZE = 40
STALE_DAYS = 2
MAX_ITEMS = 3
DEFAULT_IMG = "https://s.yimg.com/dh/ap/default/140828/silhouette@2x.png"

NEWS_LINK_RE = re.compile(
    r'PlayerNewsCard-content">\s*<a class="Link" href="(https://www\.nbcsports\.com/fantasy/baseball/player-news/(\d{4}-\d{2}-\d{2})/[^"]+)">([^<]+)</a>'
)
YIMG_RE = re.compile(
    r'https://s\.yimg\.com/xe/i/us/sp/v/mlb_cutout/players_l/[^"\'\s]+\.png'
)

now = None  # (epoch_ms, date) for the whole run, set once in main()


def now_ms():
    return now[0]


def today_date():
    return now[1]


def strip_accents(s):
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def norm_name(s):
    return strip_accents(s).lower()


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def find_img(player_id):
    try:
        html = fetch(f"https://sports.yahoo.com/mlb/players/{player_id}/")
    except Exception:
        return DEFAULT_IMG
    m = YIMG_RE.search(html)
    return m.group(0) if m else DEFAULT_IMG


def find_nbc_url(player_name):
    q = urllib.parse.quote(player_name)
    try:
        html = fetch(f"https://www.nbcsports.com/search?q={q}")
    except Exception:
        return None

    candidates = []
    for m in re.finditer(
        r'href="(https://www\.nbcsports\.com/(?:mlb/[a-z0-9-]+/[0-9]+|[a-z0-9-]+/[0-9a-f-]{20,}|mlb/[a-z0-9-]+/[0-9a-f-]{20,}))"',
        html,
    ):
        url = m.group(1)
        if url not in candidates:
            candidates.append(url)

    last_name = player_name.strip().split()[-1]
    target_full = norm_name(player_name)
    target_last = norm_name(last_name)

    for url in candidates:
        try:
            page = fetch(url)
        except Exception:
            continue
        title_m = re.search(r"<title>([^<]*)</title>", page)
        title = title_m.group(1) if title_m else ""
        title_norm = norm_name(title)
        if (target_full in title_norm or target_last in title_norm) and "PlayerNewsCard" in page:
            return url
    return None


def build_roster_id_set(roster):
    ids = {}
    for p in roster.get("players", []):
        ids[p["id"]] = p["name"]
    for t in roster.get("teams", []):
        if "players" in t:
            for p in t["players"]:
                ids[p["id"]] = p["name"]
    return ids


def sync_roster(news, roster):
    roster_ids = build_roster_id_set(roster)
    existing_ids = {p["id"] for p in news["players"]}
    onboarded = []
    for pid, name in roster_ids.items():
        if pid in existing_ids:
            continue
        stub = {
            "id": pid,
            "name": name,
            "img": find_img(pid),
            "nbcUrl": find_nbc_url(name),
            "yahooNews": [],
            "nbcNews": [],
        }
        news["players"].append(stub)
        existing_ids.add(pid)
        onboarded.append({"id": pid, "name": name, "nbcUrl": stub["nbcUrl"]})
    return onboarded


def parse_nbc_news(html):
    return [
        {"t": title.strip(), "date": date_str, "href": full_url}
        for full_url, date_str, title in NEWS_LINK_RE.findall(html)
    ]


def is_stale(date_str, today):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (today - d).days > STALE_DAYS


def _sort_key(e):
    try:
        return datetime.strptime(e["date"], "%Y-%m-%d").date()
    except ValueError:
        return datetime.min.date()


def fetch_and_add_new_items(player, today, summary):
    """Batch-scoped: fetch the player's NBC page and add genuinely-new,
    non-stale items to the front of nbcNews. Never drops/caps existing
    items -- that's prune_and_cap_player's job, applied to every player."""
    nbc_url = player.get("nbcUrl")
    if not nbc_url:
        summary["warnings"].append(f"{player['name']} (id={player['id']}): nbcUrl is null, skipped")
        return False

    try:
        html = fetch(nbc_url)
    except Exception as e:
        summary["warnings"].append(f"{player['name']} (id={player['id']}): fetch failed: {e}")
        return False

    try:
        parsed = parse_nbc_news(html)
    except Exception as e:
        summary["warnings"].append(f"{player['name']} (id={player['id']}): parse failed: {e}")
        return False

    existing = player.get("nbcNews") or []
    existing_hrefs = {item["href"] for item in existing}

    new_entries = []
    seen_new_hrefs = set()
    for item in parsed:
        href = item["href"]
        if href in existing_hrefs or href in seen_new_hrefs:
            continue
        if is_stale(item["date"], today):
            summary["skipped_stale"].append(f"{player['name']}: '{item['t']}' ({item['date']})")
            continue
        new_entries.append({
            "t": item["t"],
            "date": item["date"],
            "href": href,
            "fetchedAt": now_ms(),
        })
        seen_new_hrefs.add(href)

    if not new_entries:
        return False

    player["nbcNews"] = new_entries + existing
    for e in new_entries:
        summary["added"].append(f"{player['name']}: '{e['t']}' ({e['date']})")
    return True


def prune_and_cap_player(player, today, summary):
    """Global, every run, every player: drop items more than STALE_DAYS
    stale (by their own `date`) and cap at MAX_ITEMS most-recent.
    Independent of batching and of whether this player was fetched this
    run -- otherwise a player's stale items only clear out on the run that
    happens to fetch them, which can lag a day or more behind."""
    arr = player.get("nbcNews") or []
    if not arr:
        return False

    before = len(arr)
    pruned = [e for e in arr if not is_stale(e["date"], today)]
    dropped_stale_count = before - len(pruned)

    pruned.sort(key=_sort_key, reverse=True)
    capped_count = max(0, len(pruned) - MAX_ITEMS)
    final = pruned[:MAX_ITEMS]

    changed = dropped_stale_count > 0 or capped_count > 0
    if changed:
        player["nbcNews"] = final
        if dropped_stale_count:
            summary["dropped_stale"].append(f"{player['name']}: {dropped_stale_count}")
        if capped_count:
            summary["capped"].append(f"{player['name']}: {capped_count}")

    return changed


def main():
    global now
    run_dt = datetime.now(timezone.utc)
    now = (int(run_dt.timestamp() * 1000), run_dt.date())

    news = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))

    summary = {
        "onboarded": [],
        "processed": [],
        "added": [],
        "skipped_stale": [],
        "dropped_stale": [],
        "capped": [],
        "warnings": [],
    }

    summary["onboarded"] = sync_roster(news, roster)
    any_changed = bool(summary["onboarded"])

    def last_refreshed(p):
        arr = p.get("nbcNews") or []
        ts = [item.get("fetchedAt", 0) for item in arr if item.get("fetchedAt") is not None]
        return max(ts) if ts else 0

    ranked = sorted(news["players"], key=last_refreshed)
    batch = ranked[:BATCH_SIZE]

    today = today_date()

    # 1. Fetch + add new items -- batch-scoped (only the BATCH_SIZE stalest players).
    for player in batch:
        summary["processed"].append(player["name"])
        if fetch_and_add_new_items(player, today, summary):
            any_changed = True

    # 2. Prune stale items + cap at MAX_ITEMS -- applies to EVERY player,
    #    every run, regardless of batching.
    for player in news["players"]:
        if prune_and_cap_player(player, today, summary):
            any_changed = True

    total_players = len(news["players"])
    if total_players > BATCH_SIZE * 2:
        summary["warnings"].append(
            f"roster now {total_players} players; batch size of {BATCH_SIZE} run "
            f"twice daily only covers {BATCH_SIZE * 2}/day -- batch size may need increasing"
        )

    if any_changed:
        news["lastUpdatedNews"] = run_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{run_dt.microsecond // 1000:03d}Z"
        NEWS_PATH.write_text(
            json.dumps(news, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    print("=== NBC News Refresh Summary ===")
    print(f"Total players in news.json: {total_players}")
    print(f"Players onboarded (roster sync): {len(summary['onboarded'])}")
    for o in summary["onboarded"]:
        print(f"  + {o['name']} (id={o['id']}) nbcUrl={o['nbcUrl']}")
    print(f"Batch size processed: {len(summary['processed'])}")
    print(f"New items added: {len(summary['added'])}")
    for a in summary["added"]:
        print(f"  + {a}")
    print(f"Items skipped as already-stale (not added): {len(summary['skipped_stale'])}")
    for s in summary["skipped_stale"]:
        print(f"  - {s}")
    print(f"Items dropped for staleness during prune, ALL {total_players} players (by player): {len(summary['dropped_stale'])}")
    for d in summary["dropped_stale"]:
        print(f"  - {d}")
    print(f"Items capped at {MAX_ITEMS} (by player): {len(summary['capped'])}")
    for c in summary["capped"]:
        print(f"  - {c}")
    print(f"File changed: {any_changed}")
    print(f"Warnings: {len(summary['warnings'])}")
    for w in summary["warnings"]:
        print(f"  ! {w}")


if __name__ == "__main__":
    main()
