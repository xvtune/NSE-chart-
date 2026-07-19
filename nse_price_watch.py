"""
NSE Price Watch & Mover Alert
------------------------------
Scrapes per-ticker daily trading history from afx.kwayisi.org (a public,
free NSE data mirror — there is no official free NSE API), stores it
locally, and flags stocks that have moved >= MOVE_THRESHOLD_PCT
cumulatively over WINDOW_DAYS trading days. Optionally pushes an alert
to Telegram so you get it on your phone.

READ THIS BEFORE YOU RELY ON IT:
- This scrapes a third-party page, not an official NSE feed. If the
  site changes its layout, the parser can silently find nothing —
  that's why every run prints a per-ticker status line, not just
  alerts. Watch the console output, don't just wait for silence.
- Free/public data can lag the live tape by minutes to a day. Before
  you execute a real trade off an alert from this, confirm the price
  on your broker platform or the NSE daily bulletin.
- NSE trades roughly 09:30-15:00 EAT. Run this via cron AFTER close
  (e.g. 16:00 EAT) so the day's close is already posted. A single
  daily run is enough to build the rolling history — see the cron
  line at the bottom.
- I cannot run this against the live site from where I generate code —
  my sandbox's network is locked to package registries only. I tested
  the parsing/scoring logic below against a saved copy of the real
  page's table structure. Run it for real on your own machine.
"""

import io
import requests
import pandas as pd
import sqlite3
import time
from datetime import datetime

# ---------------- CONFIG ----------------
# ticker -> URL slug on afx.kwayisi.org/nse/<slug>.html
# Verify each slug actually resolves before trusting it — I've confirmed
# kcb and eqty; check the others on the site once before relying on them.
WATCHLIST = {
    "SCOM": "scom",
    "KCB": "kcb",
    "EQTY": "eqty",
    "KNRE": "knre",
    "CARB": "carb",
    "SASN": "sasn",
}
BASE_URL = "https://afx.kwayisi.org/nse/{slug}.html"
DB_PATH = "nse_watch.db"
WINDOW_DAYS = 2            # cumulative move measured over this many trading days
MOVE_THRESHOLD_PCT = 5.0   # alert if the |move| is at least this big
REQUEST_DELAY_SEC = 1.5    # politeness delay between tickers

TELEGRAM_BOT_TOKEN = ""    # fill in for phone alerts — setup notes at bottom
TELEGRAM_CHAT_ID = ""

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-research-script/1.0)"}
# -----------------------------------------


def init_db(path=None):
    if path is None:
        path = DB_PATH  # read the current module-level value, not one captured at def-time
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT, date TEXT, close REAL, change_pct REAL,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            ticker TEXT, date TEXT, PRIMARY KEY (ticker, date)
        )
    """)
    conn.commit()
    return conn


def fetch_history_html(slug):
    """Real network fetch — used when actually running against the live site."""
    url = BASE_URL.format(slug=slug)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def find_history_table(html_text):
    """Pull the trading-history table (Date/Close/Change%) out of the page.
    Returns a DataFrame or None if nothing matching was found — meaning the
    site layout probably changed and the parser needs a look."""
    tables = pd.read_html(io.StringIO(html_text))
    for t in tables:
        cols = [str(c).strip().lower() for c in t.columns]
        if any("date" in c for c in cols) and any("close" in c for c in cols):
            t.columns = [str(c).strip() for c in t.columns]
            return t
    return None


def normalize(df):
    """Map whatever the real column names are onto Date/Close/ChangePct."""
    colmap = {}
    for c in df.columns:
        lc = str(c).lower().replace(" ", "")
        if "date" in lc:
            colmap[c] = "Date"
        elif lc == "close":
            colmap[c] = "Close"
        elif "change%" in lc or "%chg" in lc or lc == "change":
            # prefer an explicit % column over a raw point-change column
            colmap.setdefault(c, "ChangePct" if "%" in lc else "ChangePts")
    df = df.rename(columns=colmap)
    keep = [c for c in ["Date", "Close", "ChangePct"] if c in df.columns]
    return df[keep].copy()


def parse_pct(val):
    """'+0.37%' / '-1.2%' / NaN / '' -> float or None"""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() == "nan":
        return None
    s = s.replace("%", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def store_and_check(conn, ticker, df):
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Close"])
    if df.empty:
        return None

    for _, row in df.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO prices (ticker, date, close, change_pct) VALUES (?,?,?,?)",
            (ticker, str(row.get("Date")), float(row["Close"]), parse_pct(row.get("ChangePct"))),
        )
    conn.commit()

    hist = pd.read_sql(
        "SELECT date, close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT ?",
        conn, params=(ticker, WINDOW_DAYS + 1),
    )
    if len(hist) < WINDOW_DAYS + 1:
        return {"ticker": ticker, "move_pct": None, "latest_close": hist.iloc[0]["close"],
                "date": hist.iloc[0]["date"], "insufficient_history": True}

    latest_close = hist.iloc[0]["close"]
    base_close = hist.iloc[-1]["close"]
    if base_close == 0:
        return None
    move_pct = (latest_close - base_close) / base_close * 100
    return {"ticker": ticker, "move_pct": move_pct, "latest_close": latest_close,
             "date": hist.iloc[0]["date"], "insufficient_history": False}


def already_alerted(conn, ticker, date):
    cur = conn.execute("SELECT 1 FROM alerts_sent WHERE ticker=? AND date=?", (ticker, date))
    return cur.fetchone() is not None


def mark_alerted(conn, ticker, date):
    conn.execute("INSERT OR IGNORE INTO alerts_sent (ticker, date) VALUES (?,?)", (ticker, date))
    conn.commit()


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[info] Telegram not configured — printing alert only. See setup notes at bottom of file.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except requests.RequestException as e:
        print(f"[warn] Telegram send failed: {e}")


def process_ticker(conn, ticker, html_text):
    """Runs the full pipeline for one ticker given its page HTML.
    Split out from main() so it can be unit-tested with saved HTML."""
    raw = find_history_table(html_text)
    if raw is None:
        print(f"[warn] {ticker}: no matching table found — site layout may have changed.")
        return None
    df = normalize(raw)
    if "Close" not in df.columns:
        print(f"[warn] {ticker}: no Close column after mapping — skipping.")
        return None
    return store_and_check(conn, ticker, df)


def main():
    conn = init_db()
    movers = []

    for ticker, slug in WATCHLIST.items():
        try:
            html_text = fetch_history_html(slug)
            result = process_ticker(conn, ticker, html_text)
            if result is None:
                pass
            elif result.get("insufficient_history"):
                print(f"{ticker}: logged close {result['latest_close']} on {result['date']} — "
                      f"need {WINDOW_DAYS + 1} days of history before a move can be computed. Run daily to build it up.")
            else:
                print(f"{ticker}: {result['move_pct']:+.2f}% over last {WINDOW_DAYS} trading days "
                      f"(close {result['latest_close']} on {result['date']})")
                if abs(result["move_pct"]) >= MOVE_THRESHOLD_PCT and not already_alerted(conn, ticker, result["date"]):
                    movers.append(result)
                    mark_alerted(conn, ticker, result["date"])
        except Exception as e:
            print(f"[error] {ticker}: {e}")
        time.sleep(REQUEST_DELAY_SEC)

    if movers:
        lines = [f"NSE MOVER ALERT ({datetime.now().date()})"]
        for m in movers:
            direction = "up" if m["move_pct"] > 0 else "down"
            lines.append(f"{m['ticker']} is {direction} {abs(m['move_pct']):.1f}% "
                          f"over {WINDOW_DAYS} trading days — close {m['latest_close']}")
        message = "\n".join(lines)
        print("\n" + message)
        send_telegram(message)
    else:
        print(f"\nNo tickers crossed the +/-{MOVE_THRESHOLD_PCT:.0f}% / {WINDOW_DAYS}-day threshold today.")

    conn.close()


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------
# SETUP NOTES
# ---------------------------------------------------------------
# 1. Install dependencies once:
#      pip install requests pandas lxml
#
# 2. (Optional but recommended) Phone notifications via Telegram — free, ~5 min:
#      a. In Telegram, message @BotFather -> /newbot -> follow prompts -> copy the token it gives you
#      b. Message your new bot anything once (so it can reply to you)
#      c. Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser,
#         find your numeric "chat":{"id": ...} and copy it
#      d. Paste both into TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID above
#
# 3. Verify each ticker's slug actually resolves — open
#    https://afx.kwayisi.org/nse/<slug>.html for each one in WATCHLIST
#    and confirm it's the right company before trusting alerts on it.
#
# 4. Schedule it to run once daily after NSE close (Linux/Mac cron, run `crontab -e`):
#      0 16 * * 1-5 /usr/bin/python3 /full/path/to/nse_price_watch.py >> /full/path/to/nse_watch.log 2>&1
#    (Windows: use Task Scheduler, daily trigger ~16:00 EAT, weekdays only)
