"""
NSE Price Watch & Mover Alert
------------------------------
Scrapes per-ticker daily trading history (Date, Close, Volume, Change%)
from afx.kwayisi.org (a public, free NSE data mirror — there is no
official free NSE API), stores it locally, and flags stocks that have
moved >= MOVE_THRESHOLD_PCT cumulatively over WINDOW_DAYS trading days.
Pushes an alert to Telegram, and remembers each alert's details (close,
volume, move%) so the companion telegram_bot.py can answer follow-up
questions like "volume KCB" on demand.

READ THIS BEFORE YOU RELY ON IT:
- This scrapes a third-party page, not an official NSE feed. If the
  site changes its layout, the parser can silently find nothing —
  that's why every run prints a per-ticker status line, not just
  alerts. Watch the console output, don't just wait for silence.
- Free/public data can lag the live tape by minutes to a day. Before
  you execute a real trade off an alert from this, confirm the price
  on your broker platform or the NSE daily bulletin.
- NSE trades roughly 09:30-15:00 EAT. Run this via cron/Actions AFTER
  close (e.g. 16:00 EAT) so the day's close is already posted.
"""

import io
import os
import requests
import pandas as pd
import sqlite3
import time
from datetime import datetime

# ---------------- CONFIG ----------------
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
WINDOW_DAYS = 2
MOVE_THRESHOLD_PCT = 5.0
REQUEST_DELAY_SEC = 1.5

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-research-script/1.0)"}
# -----------------------------------------


def init_db(path=None):
    if path is None:
        path = DB_PATH
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT, date TEXT, close REAL, volume REAL, change_pct REAL,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            ticker TEXT, date TEXT, close REAL, volume REAL, move_pct REAL,
            PRIMARY KEY (ticker, date)
        )
    """)
    # shared key/value table used by telegram_bot.py to remember its
    # Telegram getUpdates offset between runs
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT
        )
    """)
    conn.commit()
    return conn


def fetch_history_html(slug):
    url = BASE_URL.format(slug=slug)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def find_history_table(html_text):
    tables = pd.read_html(io.StringIO(html_text))
    for t in tables:
        cols = [str(c).strip().lower() for c in t.columns]
        if any("date" in c for c in cols) and any("close" in c for c in cols):
            t.columns = [str(c).strip() for c in t.columns]
            return t
    return None


def normalize(df):
    colmap = {}
    for c in df.columns:
        lc = str(c).lower().replace(" ", "")
        if "date" in lc:
            colmap[c] = "Date"
        elif lc == "close":
            colmap[c] = "Close"
        elif "vol" in lc:
            colmap[c] = "Volume"
        elif "change%" in lc or "%chg" in lc or lc == "change":
            colmap.setdefault(c, "ChangePct" if "%" in lc else "ChangePts")
    df = df.rename(columns=colmap)
    keep = [c for c in ["Date", "Close", "Volume", "ChangePct"] if c in df.columns]
    return df[keep].copy()


def parse_pct(val):
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


def parse_num(val):
    """'1,234,500' / NaN / '' -> float or None"""
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if s == "" or s.lower() == "nan":
        return None
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
            "INSERT OR IGNORE INTO prices (ticker, date, close, volume, change_pct) VALUES (?,?,?,?,?)",
            (ticker, str(row.get("Date")), float(row["Close"]),
             parse_num(row.get("Volume")), parse_pct(row.get("ChangePct"))),
        )
    conn.commit()

    hist = pd.read_sql(
        "SELECT date, close, volume FROM prices WHERE ticker=? ORDER BY date DESC LIMIT ?",
        conn, params=(ticker, WINDOW_DAYS + 1),
    )

    if len(hist) < WINDOW_DAYS + 1:
        return {"ticker": ticker, "move_pct": None, "latest_close": hist.iloc[0]["close"],
                 "latest_volume": hist.iloc[0]["volume"], "date": hist.iloc[0]["date"],
                 "insufficient_history": True}

    latest_close = hist.iloc[0]["close"]
    latest_volume = hist.iloc[0]["volume"]
    base_close = hist.iloc[-1]["close"]
    if base_close == 0:
        return None

    move_pct = (latest_close - base_close) / base_close * 100
    return {"ticker": ticker, "move_pct": move_pct, "latest_close": latest_close,
             "latest_volume": latest_volume, "date": hist.iloc[0]["date"],
             "insufficient_history": False}


def already_alerted(conn, ticker, date):
    cur = conn.execute("SELECT 1 FROM alerts_sent WHERE ticker=? AND date=?", (ticker, date))
    return cur.fetchone() is not None


def mark_alerted(conn, ticker, date, close, volume, move_pct):
    conn.execute(
        "INSERT OR IGNORE INTO alerts_sent (ticker, date, close, volume, move_pct) VALUES (?,?,?,?,?)",
        (ticker, date, close, volume, move_pct),
    )
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
                vol_str = f", volume {result['latest_volume']:,.0f}" if result['latest_volume'] else ""
                print(f"{ticker}: {result['move_pct']:+.2f}% over last {WINDOW_DAYS} trading days "
                      f"(close {result['latest_close']} on {result['date']}{vol_str})")
                if abs(result["move_pct"]) >= MOVE_THRESHOLD_PCT and not already_alerted(conn, ticker, result["date"]):
                    movers.append(result)
                    mark_alerted(conn, ticker, result["date"], result["latest_close"],
                                 result["latest_volume"], result["move_pct"])

        except Exception as e:
            print(f"[error] {ticker}: {e}")

        time.sleep(REQUEST_DELAY_SEC)

    if movers:
        lines = [f"NSE MOVER ALERT ({datetime.now().date()})"]
        for m in movers:
            direction = "up" if m["move_pct"] > 0 else "down"
            vol_str = f", vol {m['latest_volume']:,.0f}" if m['latest_volume'] else ""
            lines.append(f"{m['ticker']} is {direction} {abs(m['move_pct']):.1f}% "
                         f"over {WINDOW_DAYS} trading days — close {m['latest_close']}{vol_str}")
        lines.append("\nAsk me e.g. \"volume KCB\" for details on any ticker.")
        message = "\n".join(lines)
        print("\n" + message)
        send_telegram(message)
    else:
        print(f"\nNo tickers crossed the +/-{MOVE_THRESHOLD_PCT:.0f}% / {WINDOW_DAYS}-day threshold today.")

    conn.close()


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------
# SETUP NOTES — see README section in telegram_bot.py for the Q&A half
# ---------------------------------------------------------------
# 1. Local install (only needed if also running this on your own machine):
#      pip install requests pandas lxml
#
# 2. Telegram — free, ~5 min:
#      a. In Telegram, message @BotFather -> /newbot -> follow prompts -> copy the token
#      b. Message your new bot anything once (so it can reply to you)
#      c. Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser,
#         find your numeric "chat":{"id": ...} and copy it
#      d. Add both as GitHub repo secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#
# 3. This repo's .github/workflows/nse-watch.yml runs this file once daily
#    after NSE close. .github/workflows/telegram-bot.yml runs telegram_bot.py
#    every few minutes to answer questions you send the bot.
