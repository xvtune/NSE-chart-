"""
NSE Telegram Bot — answers on-demand questions
------------------------------------------------
Companion to nse_price_watch.py. That script pushes alerts to you.
This one listens for messages YOU send the bot and replies using
data already sitting in nse_watch.db (built up by the daily watcher).

Supported so far (keep it simple, expand later if useful):
  "volume KCB"   -> latest known volume, close, and date for KCB
  "price KCB"    -> latest known close for KCB
  "status" / "help" -> lists supported tickers and commands

It does NOT scrape live on every message — it answers from the most
recent data the daily watcher already collected, so it stays instant
and doesn't hammer afx.kwayisi.org. Run the daily watcher first so
there's data to answer from.

This is meant to run on a short interval (e.g. every 5 minutes) via
its own GitHub Actions workflow, NOT continuously — GitHub Actions
doesn't support always-on processes on the free tier, so short polling
runs are the practical way to get "chat back" behavior without paying
for a server.
"""

import os
import requests
import sqlite3

DB_PATH = "nse_watch.db"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

WATCHLIST = {"SCOM", "KCB", "EQTY", "KNRE", "CARB", "SASN"}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def get_offset(conn):
    cur = conn.execute("SELECT value FROM bot_state WHERE key='update_offset'")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def set_offset(conn, offset):
    conn.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES ('update_offset', ?)", (str(offset),))
    conn.commit()


def get_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result", [])


def reply(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)


def latest_row(conn, ticker):
    cur = conn.execute(
        "SELECT date, close, volume, change_pct FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 1",
        (ticker,),
    )
    return cur.fetchone()


def latest_alert(conn, ticker):
    cur = conn.execute(
        "SELECT date, close, volume, move_pct FROM alerts_sent WHERE ticker=? ORDER BY date DESC LIMIT 1",
        (ticker,),
    )
    return cur.fetchone()


def handle_command(conn, text):
    text = text.strip().lower()
    parts = text.split()

    if not parts:
        return None

    if parts[0] in ("help", "/help", "start", "/start", "status"):
        return ("I watch: " + ", ".join(sorted(WATCHLIST)) +
                "\nAsk me: 'volume TICKER', 'price TICKER', or 'alert TICKER'.")

    if parts[0] in ("volume", "price", "alert") and len(parts) >= 2:
        ticker = parts[1].upper()
        if ticker not in WATCHLIST:
            return f"I'm not watching {ticker}. Watching: {', '.join(sorted(WATCHLIST))}"

        if parts[0] == "alert":
            row = latest_alert(conn, ticker)
            if not row:
                return f"No alert has fired for {ticker} yet."
            date, close, volume, move_pct = row
            vol_str = f"{volume:,.0f}" if volume else "unknown"
            return f"{ticker} alert on {date}: moved {move_pct:+.2f}%, close {close}, volume {vol_str}"

        row = latest_row(conn, ticker)
        if not row:
            return f"No data logged for {ticker} yet — the daily watcher hasn't run for it."
        date, close, volume, change_pct = row
        if parts[0] == "volume":
            vol_str = f"{volume:,.0f}" if volume else "not available for that day"
            return f"{ticker} volume on {date}: {vol_str}"
        else:  # price
            return f"{ticker} close on {date}: {close}"

    return "Not sure what you mean. Try 'help' for what I can answer."


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("[error] TELEGRAM_BOT_TOKEN not set — nothing to do.")
        return

    conn = get_conn()
    offset = get_offset(conn)
    updates = get_updates(offset)

    if not updates:
        print("[info] no new messages.")
        conn.close()
        return

    for update in updates:
        offset = update["update_id"] + 1
        msg = update.get("message", {})
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")

        if not text or chat_id is None:
            continue

        # Only answer the configured chat, so randoms who somehow message
        # the bot can't query your data.
        if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
            print(f"[warn] ignoring message from unrecognized chat_id {chat_id}")
            continue

        answer = handle_command(conn, text)
        if answer:
            reply(chat_id, answer)
            print(f"[info] replied to '{text}' -> '{answer}'")

    set_offset(conn, offset)
    conn.close()


if __name__ == "__main__":
    main()
