#!/usr/bin/env python3
"""Fetch public DraftKings moneyline betting splits by league.

Never invent values or overwrite a previous verified snapshot when parsing fails.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BetTrack/1.0)"}
LEAGUES = ("NBA", "NFL", "MLB")
GAME = re.compile(r"^(.+?)\s+@\s+(.+?)$")
ODDS = re.compile(r"^[+\-−–]\d{2,4}$")
PERCENT = re.compile(r"^(\d{1,3})%$")
DATE = re.compile(r"^\d{1,2}/\d{1,2},\s+\d{1,2}:\d{2}\s*(?:AM|PM)$", re.I)


def parse(text, target_date=None):
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    output = []
    for i, heading in enumerate(lines):
        match = GAME.fullmatch(heading)
        if not match or i + 1 >= len(lines) or not DATE.fullmatch(lines[i + 1]):
            continue
        away, home = match.groups()
        if target_date is not None:
            md = re.match(r"(\d{1,2})/(\d{1,2})", lines[i + 1])
            if not md or (int(md.group(1)), int(md.group(2))) != (target_date.month, target_date.day):
                continue
        # Limit scanning to this game's own section.
        end = next((j for j in range(i + 2, len(lines))
                    if GAME.fullmatch(lines[j]) and j + 1 < len(lines)
                    and DATE.fullmatch(lines[j + 1])), len(lines))
        block = lines[i + 2:end]
        try:
            pos = block.index("Moneyline")
        except ValueError:
            continue
        block = block[pos + 1:]
        try:
            pos = block.index("% Bets")
        except ValueError:
            continue
        block = block[pos + 1:]
        rows = []
        for j in range(len(block) - 3):
            if block[j] not in (home, away):
                continue
            odd = block[j + 1].replace("−", "-").replace("–", "-")
            handle = PERCENT.fullmatch(block[j + 2])
            bets = PERCENT.fullmatch(block[j + 3])
            if not ODDS.fullmatch(odd) or not handle or not bets:
                continue
            money_pct, bet_pct = int(handle.group(1)), int(bets.group(1))
            if not (0 <= money_pct <= 100 and 0 <= bet_pct <= 100):
                continue
            rows.append({"team": block[j], "odds": int(odd),
                         "moneyPct": money_pct, "betPct": bet_pct})
            if len(rows) == 2:
                break
        if len(rows) != 2 or {r["team"] for r in rows} != {home, away}:
            continue
        if abs(sum(r["betPct"] for r in rows) - 100) > 2:
            continue
        if abs(sum(r["moneyPct"] for r in rows) - 100) > 2:
            continue
        game_id = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
        for r, opponent in ((rows[0], rows[1]), (rows[1], rows[0])):
            output.append({"gameId": game_id, "opponent": opponent["team"], **r})
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", choices=LEAGUES, required=True)
    args = parser.parse_args()
    league = args.league
    today = datetime.now(ZoneInfo("America/New_York")).date()
    # NBA: DraftKings NBA event group 42648. Its moneyline table is paginated.
    # Request every page, then merge only today's complete games.
    group_ids = {"NBA": "42648", "NFL": "88808", "MLB": "84240"}
    base_params = {"itm_content": group_ids[league], "tb_edate": "n7days",
                   "tb_eg": group_ids[league], "tb_emt": "Moneyline"}
    teams_by_game = {}
    urls = []
    expected_games = set()
    session = requests.Session()
    seen_pages = set()
    page_counts = []
    for page in range(1, 21):
        params = {**base_params, "tb_page": page}
        response = session.get(BASE, params=params, headers=HEADERS, timeout=35)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        page_text = soup.get_text("\n")
        fingerprint = " ".join(page_text.split())
        if fingerprint in seen_pages:
            print(f"{league}: page {page} repeats a previous page; stopping pagination")
            break
        seen_pages.add(fingerprint)
        if league == "NBA":
            normalized = [re.sub(r"\s+", " ", v).strip() for v in page_text.splitlines()]
            normalized = [v for v in normalized if v]
            for i, line in enumerate(normalized[:-1]):
                if GAME.fullmatch(line) and DATE.fullmatch(normalized[i + 1]):
                    md = re.match(r"(\d{1,2})/(\d{1,2})", normalized[i + 1])
                    if md and (int(md.group(1)), int(md.group(2))) == (today.month, today.day):
                        expected_games.add(re.sub(r"[^a-z0-9]+", "-", line.lower()).strip("-"))
        parsed = parse(page_text, target_date=today if league == "NBA" else None)
        page_counts.append(len(parsed))
        print(f"{league}: page {page}, {len(parsed)} team rows, {len(expected_games)} dated NBA matchups listed")
        for row in parsed:
            teams_by_game.setdefault(row["gameId"], {})[row["team"]] = row
        urls.append(response.url)
        # A page is final only when there is no explicit Next pagination link.
        next_links = [a for a in soup.select("a[href]")
                      if a.get_text(" ", strip=True).lower() in ("next", "next »", "›")]
        if not next_links:
            break
    else:
        raise RuntimeError("20 distinct pages encountered; refusing possibly incomplete snapshot")
    teams = [row for game in teams_by_game.values() if len(game) == 2
             for row in game.values()]
    if not teams:
        raise RuntimeError(f"No complete {league} moneyline games parsed; preserving prior snapshot")
    if league == "NBA":
        if len(teams_by_game) < 2:
            raise RuntimeError(
                f"Only {len(teams_by_game)} NBA game(s) retrieved; "
                "refusing to replace daily snapshot with a partial slate"
            )
        # Cross-check count against the source's own matchup headings for today.
        # Never silently publish a truncated partial slate.
        if not expected_games or len(teams) % 2 or set(teams_by_game) != expected_games:
            raise RuntimeError(f"Incomplete NBA slate: {len(teams_by_game)} parsed of {len(expected_games)} listed games")
    now = datetime.now(timezone.utc)
    payload = {
        "snapshotDate": today.isoformat(),
        "retrievedAt": now.isoformat(),
        "status": "Verified public DraftKings snapshot",
        "sourceUrl": urls[0],
        "note": "DraftKings all-jurisdiction public splits; odds and percentages may change. Snapshot only.",
        "teams": teams,
    }
    path = Path("data") / f"{league.lower()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    # Retain legacy NBA URL for existing bookmarks/consumers.
    if league == "NBA":
        Path("data/latest.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Updated {league}: {len(teams)} teams")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Snapshot not changed: {exc}", file=sys.stderr)
        sys.exit(1)
