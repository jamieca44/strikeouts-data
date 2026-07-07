#!/usr/bin/env python3
"""
Regenerates data.json (the combined, consumer-facing file) by merging
roster-stats.json (owned/written by the Cowork roster-stats-refresh task)
and news.json (owned/written by the Claude Code news-refresh routine).

Run this from the repo root after either source file changes, before
committing:

    python3 scripts/build_data_json.py

It is intentionally a pure function of the two source files on disk plus
the previously-generated data.json is never read as an input -- data.json
is fully derived output and should not be hand-edited.

Player identity (name/team/pos) is taken from roster-stats.json, since the
Cowork task refreshes it daily and is the source of truth for who is
currently rostered. Players are only ever removed from data.json here
because they've dropped off roster-stats.json (the Cowork task already
prunes those there); news.json is not pruned by this script.

If a player exists in roster-stats.json but has no matching entry yet in
news.json (e.g. newly added to the roster, before the news job has caught
up), placeholder news fields are used and a note is printed.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROSTER_PATH = REPO_ROOT / "roster-stats.json"
NEWS_PATH = REPO_ROOT / "news.json"
OUTPUT_PATH = REPO_ROOT / "data.json"

NEWS_PLACEHOLDER = {
    "img": "https://s.yimg.com/dh/ap/default/140828/silhouette@2x.png",
    "nbcUrl": None,
    "yahooNews": [],
    "nbcNews": [],
}


def main():
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    news = json.loads(NEWS_PATH.read_text(encoding="utf-8"))

    news_by_id = {p["id"]: p for p in news["players"]}

    missing_news = []
    players = []
    for rp in roster["players"]:
        pid = rp["id"]
        np = news_by_id.get(pid)
        if np is None:
            missing_news.append((pid, rp["name"]))
            news_fields = dict(NEWS_PLACEHOLDER)
        else:
            news_fields = {
                "img": np.get("img", NEWS_PLACEHOLDER["img"]),
                "nbcUrl": np.get("nbcUrl"),
                "yahooNews": np.get("yahooNews", []),
                "nbcNews": np.get("nbcNews", []),
            }

        players.append({
            "name": rp["name"],
            "team": rp["team"],
            "pos": rp["pos"],
            "id": pid,
            "img": news_fields["img"],
            "ilStatus": rp["ilStatus"],
            "nbcUrl": news_fields["nbcUrl"],
            "yesterdayStats": rp["yesterdayStats"],
            "lastWeekStats": rp["lastWeekStats"],
            "lastMonthStats": rp["lastMonthStats"],
            "yahooNews": news_fields["yahooNews"],
            "nbcNews": news_fields["nbcNews"],
        })

    combined = {
        "schemaVersion": roster["schemaVersion"],
        "lastUpdatedNews": news["lastUpdatedNews"],
        "lastUpdatedRosterStats": roster["lastUpdatedRosterStats"],
        "players": players,
    }

    OUTPUT_PATH.write_text(
        json.dumps(combined, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Wrote {OUTPUT_PATH} with {len(players)} players.")
    if missing_news:
        print(
            f"Note: {len(missing_news)} player(s) on the roster have no "
            f"news.json entry yet (using placeholders): "
            + ", ".join(f"{name} ({pid})" for pid, name in missing_news)
        )


if __name__ == "__main__":
    sys.exit(main())
