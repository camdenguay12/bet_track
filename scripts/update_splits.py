#!/usr/bin/env python3
"""Fetch public DraftKings moneyline betting splits by league.

Never invent values or overwrite a previous verified snapshot when parsing fails.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
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


def parse(text):
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    output = []
    for i, heading in enumerate(lines):
        match = GAME.fullmatch(heading)
        if not match or i + 1 >= len(lines) or not DATE.fullmatch(lines[i + 1]):
            continue
        away, home = match.groups()
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
    params = {"itm_content": league, "tb_edate": "n7days",
              "tb_eg": league, "tb_emt": "Moneyline"}
    response = requests.get(BASE, params=params, headers=HEADERS, timeout=35)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    teams = parse(soup.get_text("\n"))
    if not teams:
        raise RuntimeError(f"No complete {league} moneyline games parsed; preserving prior snapshot")
    now = datetime.now(timezone.utc)
    payload = {
        "snapshotDate": now.strftime("%Y-%m-%d"),
        "retrievedAt": now.isoformat(),
        "status": "Verified public DraftKings snapshot",
        "sourceUrl": response.url,
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
