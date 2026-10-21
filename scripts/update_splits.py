#!/usr/bin/env python3
"""Best-effort parser for DraftKings' public, aggregated NBA moneyline splits."""
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

URL = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/?itm_content=NBA&tb_edate=n7days&tb_eg=42648&tb_page=1"
OUTPUT = Path("data/latest.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BetTrack/1.0; public sports analytics)"}
TEAMS = {"Hawks","Celtics","Nets","Hornets","Bulls","Cavaliers","Mavericks","Nuggets","Pistons","Warriors","Rockets","Pacers","Clippers","Lakers","Grizzlies","Heat","Bucks","Timberwolves","Pelicans","Knicks","Thunder","Magic","76ers","Suns","Trail Blazers","Kings","Spurs","Raptors","Jazz","Wizards"}
def parse(text):
    lines = [re.sub(r"\\s+"," ",s).strip() for s in text.splitlines()]
    lines = [s for s in lines if s]
    games=[]
    for i,line in enumerate(lines):
        if not re.search(r"\\b(?:@| at )\\b",line): continue
        if not any(t in line for t in TEAMS): continue
        segment=lines[i+1:i+45]
        try:
            m=segment.index("Moneyline")
        except ValueError: continue
        segment=segment[m+1:]
        rows=[]
        for j,s in enumerate(segment):
            if s in ("Spread","Total","Moneyline") and j>2: break
            if not any(t in s for t in TEAMS):continue
            if j+3>=len(segment):continue
            # DK table row order: team, American odds, handle %, bets %
            odds=segment[j+1].replace("−","-").replace("–","-").replace(",","")
            handle=segment[j+2].replace("%","")
            bets=segment[j+3].replace("%","")
            if not re.fullmatch(r"[+-]?\\d{2,4}",odds):continue
            if not(handle.isdigit() and bets.isdigit()):continue
            if not(0<=int(handle)<=100 and 0<=int(bets)<=100):continue
            rows.append({"team":s,"odds":int(odds),"moneyPct":int(handle),"betPct":int(bets)})
            if len(rows)==2:break
        if len(rows)!=2:continue
        if abs(rows[0]["betPct"]+rows[1]["betPct"]-100)>2:continue
        if abs(rows[0]["moneyPct"]+rows[1]["moneyPct"]-100)>2:continue
        game_id=re.sub(r"[^a-z0-9]+","-",line.lower()).strip("-")
        for r,opp in ((rows[0],rows[1]),(rows[1],rows[0])):
            games.append(dict(gameId=game_id,opponent=opp["team"],**r))
    return games

def main():
    response=requests.get(URL,headers=HEADERS,timeout=25)
    response.raise_for_status()
    soup=BeautifulSoup(response.text,"html.parser")
    for element in soup(["script","style","noscript"]):element.decompose()
    games=parse(soup.get_text("\\n"))
    if len(games)<2:
        raise RuntimeError("No complete NBA moneyline matchups parsed; retaining last known snapshot")
    now=datetime.now(timezone.utc)
    payload={"snapshotDate":now.strftime("%Y-%m-%d"),"retrievedAt":now.isoformat(),"status":"Public DraftKings snapshot","sourceUrl":URL,"note":"Aggregated all-jurisdiction public moneyline splits. Snapshot only; not necessarily the latest sportsbook screen. Values may change.","teams":games}
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(payload,indent=2)+"\\n")
    print("Updated",len(games),"teams")
if __name__=="__main__":
    try:main()
    except Exception as e:
        print("Fetch failed; no snapshot overwritten:",e,file=sys.stderr)
        sys.exit(1)
