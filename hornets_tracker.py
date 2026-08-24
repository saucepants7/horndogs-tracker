#!/usr/bin/env python3
"""Charlotte Hornets home-game price tracker.

Schedule + onsale info comes from Ticketmaster (reliable, all 42 home games).
Prices come from SeatGeek (Ticketmaster's free API doesn't expose them), matched
to each game by date. Every run records the lowest price into a SQLite history,
flags onsales and good deals, and rebuilds a sortable dashboard (index.html).

Standard library only. No pip installs.
"""
import html
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---- settings you can tweak -------------------------------------------------
HOME_CITY   = "charlotte"     # home games are played here
SG_SLUG     = "charlotte-hornets"
DEAL_RATIO  = 0.80            # alert when lowest <= 80% of its own average
MIN_HISTORY = 3              # need this many past points before deal alerts
DB_PATH   = os.path.join(os.path.dirname(__file__), "prices.db")
HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")
TM_API = "https://app.ticketmaster.com/discovery/v2/events.json"
SG_API = "https://api.seatgeek.com/2/events"

DIAG = {"raw": 0, "kept": 0, "sg_matched": 0, "sg_fetched": 0, "sg_note": "",
        "sg_home": 0, "sg_priced": 0, "sg_sample": "", "sg_stats": ""}


# ---- SeatGeek prices (matched to games by date) -----------------------------
def fetch_seatgeek_prices():
    """Return {YYYY-MM-DD: (lowest, highest)} for Hornets HOME games."""
    mock = os.environ.get("MOCK_SG")
    prices = {}
    counters = {"home": 0, "priced": 0}

    def absorb(events):
        for ev in events:
            v = ev.get("venue") or {}
            city = v.get("city") or ""
            date = (ev.get("datetime_local") or "")[:10]
            if not DIAG["sg_sample"] and (city or date):
                DIAG["sg_sample"] = f"city='{city}' date={date}"
            if HOME_CITY not in city.lower():      # home games only
                continue
            counters["home"] += 1
            stats = ev.get("stats") or {}
            if not DIAG["sg_stats"]:
                DIAG["sg_stats"] = json.dumps(stats)[:180]
            lp = stats.get("lowest_price")
            if date:
                prices[date] = (lp, stats.get("highest_price"))
                if lp is not None:
                    counters["priced"] += 1

    if mock:
        absorb(json.load(open(mock)))
        DIAG["sg_fetched"] = counters["home"]
        DIAG["sg_home"] = counters["home"]
        DIAG["sg_priced"] = counters["priced"]
        return prices

    cid = os.environ.get("SEATGEEK_CLIENT_ID")
    if not cid:
        DIAG["sg_note"] = "no client id set"
        return prices

    fetched = [0]

    def query(extra):
        page = 1
        while page <= 10:
            q = {"client_id": cid, "per_page": "100", "page": str(page), **extra}
            url = SG_API + "?" + urllib.parse.urlencode(q)
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                DIAG["sg_note"] = f"HTTP {e.code}"
                return
            except Exception as e:
                DIAG["sg_note"] = f"error: {e}"
                return
            events = data.get("events", [])
            fetched[0] += len(events)
            absorb(events)
            meta = data.get("meta", {})
            per = meta.get("per_page") or 100
            if not events or page * per >= (meta.get("total") or 0):
                break
            page += 1

    query({"performers.slug": SG_SLUG})
    if fetched[0] == 0 and not DIAG["sg_note"]:
        query({"q": "Charlotte Hornets"})       # fallback if the slug is wrong
    DIAG["sg_fetched"] = fetched[0]
    DIAG["sg_home"] = counters["home"]
    DIAG["sg_priced"] = counters["priced"]
    return prices


# ---- Ticketmaster schedule --------------------------------------------------
def is_home_game(ev):
    venues = ev.get("_embedded", {}).get("venues", [])
    if not venues:
        return False
    v = venues[0]
    city = (v.get("city", {}) or {}).get("name", "") or ""
    name = v.get("name", "") or ""
    return HOME_CITY in city.lower() or "spectrum" in name.lower()


def fetch_home_games():
    """Home games from Ticketmaster, priced from SeatGeek.

    Test offline with MOCK_EVENTS (TM events) and MOCK_SG (SeatGeek events).
    """
    mock = os.environ.get("MOCK_EVENTS")
    if mock:
        with open(mock) as f:
            raw = json.load(f)
    else:
        key = os.environ.get("TM_API_KEY")
        if not key:
            raise RuntimeError("TM_API_KEY not set")
        raw = []
        page = 0
        while page < 10:
            q = {"apikey": key, "keyword": "Charlotte Hornets",
                 "classificationName": "Sports", "countryCode": "US",
                 "size": "100", "sort": "date,asc", "page": str(page)}
            url = TM_API + "?" + urllib.parse.urlencode(q)
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.load(r)
            batch = data.get("_embedded", {}).get("events", [])
            raw += batch
            total_pages = data.get("page", {}).get("totalPages", 1)
            page += 1
            if not batch or page >= total_pages:
                break

    DIAG["raw"] = len(raw)
    sg_prices = fetch_seatgeek_prices()

    games = []
    for ev in raw:
        if "charlotte hornets" not in (ev.get("name") or "").lower():
            continue
        if not is_home_game(ev):
            continue
        date = ev.get("dates", {}).get("start", {}).get("localDate")
        low, high = sg_prices.get(date, (None, None))
        games.append({
            "event_id": ev["id"],
            "name": ev.get("name"),
            "game_date": date,
            "venue": (ev.get("_embedded", {}).get("venues", [{}])[0].get("name")),
            "url": ev.get("url"),
            "status": ev.get("dates", {}).get("status", {}).get("code"),
            "onsale": ev.get("sales", {}).get("public", {}).get("startDateTime"),
            "lowest": low,
            "highest": high,
        })
    DIAG["kept"] = len(games)
    DIAG["sg_matched"] = sum(1 for g in games if g["lowest"] is not None)
    return games


# ---- database ---------------------------------------------------------------
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS games (
        event_id TEXT PRIMARY KEY, name TEXT, game_date TEXT,
        venue TEXT, url TEXT, status TEXT, onsale TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS price_history (
        event_id TEXT, checked_at TEXT, lowest REAL, highest REAL,
        PRIMARY KEY (event_id, checked_at))""")
    return db


def prior_state(db, event_id):
    row = db.execute("SELECT status, onsale FROM games WHERE event_id=?",
                     (event_id,)).fetchone()
    lows = [r[0] for r in db.execute(
        "SELECT lowest FROM price_history WHERE event_id=? AND lowest IS NOT NULL "
        "ORDER BY checked_at", (event_id,)).fetchall()]
    return row, lows


def record(db, g, now):
    db.execute("""INSERT INTO games (event_id,name,game_date,venue,url,status,onsale)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(event_id) DO UPDATE SET
        name=excluded.name, game_date=excluded.game_date, venue=excluded.venue,
        url=excluded.url, status=excluded.status, onsale=excluded.onsale""",
        (g["event_id"], g["name"], g["game_date"], g["venue"], g["url"],
         g["status"], g["onsale"]))
    db.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?,?)",
               (g["event_id"], now, g["lowest"], g["highest"]))


# ---- alerts -----------------------------------------------------------------
def alerts_for(g, prior_row, prior_lows):
    out = []
    if prior_row is None:
        out.append(f"NEW home game listed: {g['name']} ({g['game_date']})"
                   + (f" - lowest ${g['lowest']:.0f}" if g['lowest'] else ""))
        return out
    old_status, old_onsale = prior_row
    if old_status != "onsale" and g["status"] == "onsale":
        out.append(f"ON SALE now: {g['name']} ({g['game_date']}) {g['url']}")
    if not old_onsale and g["onsale"]:
        out.append(f"Onsale date set - {g['name']}: {g['onsale']}")
    if g["lowest"] is not None and len(prior_lows) >= MIN_HISTORY:
        avg = sum(prior_lows) / len(prior_lows)
        if g["lowest"] <= DEAL_RATIO * avg:
            out.append(f"GOOD DEAL - {g['name']}: ${g['lowest']:.0f} "
                       f"(avg ~${avg:.0f}) {g['url']}")
    return out


def notify(subject, body):
    print(subject + "\n" + body)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}",
                                     data=body.encode(), headers={"Title": subject})
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print("ntfy failed:", e)


# ---- dashboard --------------------------------------------------------------
def sparkline(lows):
    pts = [x for x in lows if x is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    w, h = 120, 28
    step = w / (len(pts) - 1)
    coords = " ".join(
        f"{i*step:.1f},{h - (v-lo)/rng*(h-4) - 2:.1f}" for i, v in enumerate(pts))
    return (f'<svg width="{w}" height="{h}">'
            f'<polyline fill="none" stroke="#1d8cf8" stroke-width="2" '
            f'points="{coords}"/></svg>')


def build_dashboard(db):
    rows = db.execute("SELECT event_id,name,game_date,url FROM games "
                      "ORDER BY game_date").fetchall()
    trs = []
    for eid, name, date, url in rows:
        hist = [r[0] for r in db.execute(
            "SELECT lowest FROM price_history WHERE event_id=? ORDER BY checked_at",
            (eid,)).fetchall()]
        cur = next((x for x in reversed(hist) if x is not None), None)
        ever = min([x for x in hist if x is not None], default=None)
        deal = cur is not None and ever is not None and cur <= ever * 1.001
        cur_s = f"${cur:.0f}" if cur is not None else "-"
        ever_s = f"${ever:.0f}" if ever is not None else "-"
        cls = ' class="deal"' if deal else ""
        trs.append(
            f"<tr{cls}><td><a href='{html.escape(url or '#')}'>"
            f"{html.escape(name or '')}</a></td>"
            f"<td>{html.escape(date or '')}</td>"
            f"<td data-v='{cur if cur is not None else 1e9}'>{cur_s}</td>"
            f"<td>{ever_s}</td><td>{sparkline(hist)}</td></tr>")
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sub = (f"Lowest SeatGeek price per game. Green = at its lowest ever. "
           f"Prices matched for {DIAG['sg_matched']} of {DIAG['kept']} games. "
           f"Updated {updated}.")
    if DIAG["sg_matched"] == 0:
        sub += (f" [SeatGeek debug: fetched {DIAG['sg_fetched']}, "
                f"home {DIAG['sg_home']}, priced {DIAG['sg_priced']}, "
                f"sample {DIAG['sg_sample'] or 'none'}"
                + (f", {DIAG['sg_note']}" if DIAG['sg_note'] else "")
                + (f", stats={DIAG['sg_stats']}" if DIAG['sg_stats'] else "")
                + ".]")
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hornets Ticket Tracker</title><style>
body{{font-family:system-ui,sans-serif;margin:1rem;color:#111}}
h1{{font-size:1.2rem}} .sub{{color:#666;font-size:.8rem;margin-bottom:1rem}}
table{{border-collapse:collapse;width:100%}}
th,td{{padding:.5rem .6rem;border-bottom:1px solid #eee;text-align:left;font-size:.9rem}}
th{{cursor:pointer;background:#fafafa}} tr.deal td{{background:#eafbea}}
a{{color:#1d8cf8;text-decoration:none}}</style></head><body>
<h1>Charlotte Hornets - home game prices</h1>
<div class="sub">{sub}</div>
<table id="t"><thead><tr>
<th onclick="s(0)">Game</th><th onclick="s(1)">Date</th>
<th onclick="s(2,1)">Lowest now</th><th onclick="s(3)">Lowest ever</th>
<th>Trend</th></tr></thead><tbody>
{''.join(trs) or '<tr><td colspan=5>No games recorded yet.</td></tr>'}
</tbody></table>
<script>
function s(c,num){{const tb=document.querySelector('#t tbody');
[...tb.rows].sort((a,b)=>{{let x=a.cells[c],y=b.cells[c];
x=num?+x.dataset.v:x.innerText;y=num?+y.dataset.v:y.innerText;
return x>y?1:x<y?-1:0}}).forEach(r=>tb.appendChild(r))}}
</script></body></html>"""
    with open(HTML_PATH, "w") as f:
        f.write(doc)


# ---- main -------------------------------------------------------------------
def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = get_db()
    all_alerts = []
    for g in fetch_home_games():
        prior_row, prior_lows = prior_state(db, g["event_id"])
        all_alerts += alerts_for(g, prior_row, prior_lows)
        record(db, g, now)
    db.commit()
    build_dashboard(db)
    db.close()
    print(f"TM kept {DIAG['kept']} home games; "
          f"SeatGeek priced {DIAG['sg_matched']}.")
    if all_alerts:
        notify(f"Hornets: {len(all_alerts)} update(s)", "\n\n".join(all_alerts))
    else:
        print("No new alerts.")


if __name__ == "__main__":
    main()
