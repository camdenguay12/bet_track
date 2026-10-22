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
        try:
            start = datetime.strptime(lines[i + 1], "%m/%d, %I:%M %p").replace(
                year=datetime.now(ZoneInfo("America/New_York")).year,
                tzinfo=ZoneInfo("America/New_York"))
        except ValueError:
            continue
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
            output.append({"gameId": game_id, "opponent": opponent["team"], "startTime": start.isoformat(), **r})
    return output



def fetch_schedule(session, league, today):
    """ESPN public scoreboard schedule; no betting percentages inferred."""
    from datetime import timedelta
    dates = [today, today + timedelta(days=1)] if league != "NFL" else [
        today + timedelta(days=offset) for offset in range(-today.weekday(), 8-today.weekday())
    ]
    games = {}
    sport = {"NBA": "basketball/nba", "NFL": "football/nfl", "MLB": "baseball/mlb"}[league]
    for date in dates:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/scoreboard"
        response = session.get(url, params={"dates": date.strftime("%Y%m%d"), "limit": 1000}, timeout=30)
        response.raise_for_status()
        for event in response.json().get("events", []):
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            sides = competitions[0].get("competitors", [])
            if len(sides) != 2:
                continue
            away = next((x for x in sides if x.get("homeAway") == "away"), None)
            home = next((x for x in sides if x.get("homeAway") == "home"), None)
            if not away or not home:
                continue
            start = event.get("date")
            if not start:
                continue
            et_date = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date()
            if et_date not in dates:
                continue
            away_name = away.get("team", {}).get("displayName", "")
            home_name = home.get("team", {}).get("displayName", "")
            if not away_name or not home_name:
                continue
            key = str(event.get("id"))
            games[key] = {"gameId": key, "startTime": start, "away": away_name, "home": home_name,
                          "status": event.get("status", {}).get("type", {}).get("name", "STATUS_SCHEDULED")}
    return games


def attach_splits(schedule, parsed):
    """Match DraftKings abbreviated teams to scheduled games by nickname and date."""
    def aliases(x):
        # DraftKings uses abbreviations (e.g. "CLE Cavaliers") while ESPN
        # supplies city + nickname (e.g. "Cleveland Cavaliers").
        nicknames = ("trail blazers", "76ers", "cavaliers", "thunder",
                     "timberwolves", "pelicans", "grizzlies", "mavericks",
                     "clippers", "lakers", "warriors", "wizards", "raptors",
                     "celtics", "knicks", "bucks", "heat", "magic", "hawks",
                     "nets", "bulls", "jazz", "pacers", "rockets", "suns",
                     "nuggets", "spurs", "kings", "hornets", "pistons",
                     "patriots", "bears", "steelers", "saints", "packers",
                     "lions", "rams", "raiders", "chiefs", "seahawks",
                     "cowboys", "eagles", "49ers", "commanders", "jets",
                     "giants", "bills", "dolphins", "ravens", "bengals",
                     "browns", "texans", "colts", "jaguars", "titans",
                     "broncos", "chargers", "cardinals", "falcons",
                     "panthers", "buccaneers", "vikings")
        normalized = x.lower().replace("portland trail blazers", "por trail blazers")
        return next((re.sub(r"[^a-z0-9]", "", n) for n in nicknames
                     if normalized.endswith(n)), re.sub(r"[^a-z0-9]", "", normalized))
    indexed = {}
    for row in parsed:
        indexed[(aliases(row["team"]), aliases(row["opponent"]), row["startTime"][:10])] = row
    output = []
    for game in schedule.values():
        for team, opponent in ((game["away"], game["home"]), (game["home"], game["away"])):
            split = indexed.get((aliases(team), aliases(opponent), datetime.fromisoformat(game["startTime"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date().isoformat()))
            output.append({"gameId": game["gameId"], "team": team, "opponent": opponent,
                           "startTime": game["startTime"], "gameStatus": game["status"],
                           "odds": split["odds"] if split else None,
                           "betPct": split["betPct"] if split else None,
                           "moneyPct": split["moneyPct"] if split else None})
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
        parsed = parse(page_text, target_date=None)
        page_counts.append(len(parsed))
        print(f"{league}: page {page}, {len(parsed)} team rows, {len(expected_games)} dated NBA matchups listed")
        for row in parsed:
            teams_by_game.setdefault(row["gameId"], {})[row["team"]] = row
        urls.append(response.url)
        # The DK table supports tb_page even when it has no Next anchor.
    else:
        raise RuntimeError("20 distinct pages encountered; refusing possibly incomplete snapshot")
    teams = [row for game in teams_by_game.values() if len(game) == 2
             for row in game.values()]
    schedule = fetch_schedule(session, league, today)
    if not schedule:
        raise RuntimeError(f"No {league} scheduled games returned; preserving previous data")
    teams = attach_splits(schedule, teams)
    # Preserve previously verified market figures for the same matchup and date
    # when a new scrape cannot retrieve them. Never carry figures to another day.
    previous_path = Path("data") / f"{league.lower()}.json"
    if league == "NBA" and not previous_path.exists():
        previous_path = Path("data/latest.json")
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text()).get("teams", [])
            key = lambda row: (row.get("team"), row.get("opponent"),
                               (row.get("startTime") or "")[:10])
            old = {key(row): row for row in previous if row.get("betPct") is not None
                   and row.get("moneyPct") is not None and row.get("odds") is not None}
            for row in teams:
                prior = old.get(key(row))
                if prior and row["betPct"] is None:
                    for field in ("odds", "betPct", "moneyPct"):
                        row[field] = prior[field]
                    row["splitsRetained"] = True
        except (ValueError, OSError, TypeError):
            pass
    print(f"{league}: {len(schedule)} scheduled matchups, {sum(x['betPct'] is not None for x in teams)} teams with verified splits")
    now = datetime.now(timezone.utc)
    payload = {
        "snapshotDate": today.isoformat(),
        "retrievedAt": now.isoformat(),
        "status": "Schedule verified; betting splits shown only when matched",
        "sourceUrl": urls[0],
        "note": "Schedule from ESPN; betting percentages and odds from DraftKings where available. Missing splits are not estimated.",
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
