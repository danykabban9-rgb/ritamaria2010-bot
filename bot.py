import os
import time
import threading
import logging
import requests
from flask import Flask, request
from datetime import datetime
import pytz

# --- CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")
BEIRUT_TZ = pytz.timezone("Asia/Beirut")

app = Flask(__name__)
LAST_ALERT_TIME = 0
logging.basicConfig(level=logging.INFO)

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logging.error(f"telegram {e}")

def is_trading_window():
    now = datetime.now(BEIRUT_TZ)
    hour = now.hour
    # 10-12 and 14-18 Beirut
    if (10 <= hour < 12) or (14 <= hour < 18):
        return True, f"Window {hour}:00 OK"
    return False, f"Outside window {hour}:00"

def is_news_block():
    # add your red news logic here if you have it
    return False, ""

def get_xau_data(interval):
    try:
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={interval}&apikey={TWELVE_DATA_KEY}&outputsize=100"
        r = requests.get(url, timeout=10).json()
        return r.get("values")
    except:
        return None

def analyze_v2(m5, m15):
    # --- YOUR V2 LOGIC HERE ---
    # This is placeholder scoring, replace with your Pin Bar + LH + trend + ATR logic
    # return format: (signal, entry, sl, tp1, reason, tp2, score)
    # Example for testing:
    import random
    score = random.randint(30, 85)
    if score >= 70:
        price = float(m5[0]["close"])
        signal = "SELL" if float(m5[0]["close"]) < float(m5[1]["close"]) else "BUY"
        sl = price + 5 if signal == "SELL" else price - 5
        tp1 = price - 8 if signal == "SELL" else price + 8
        tp2 = price - 15 if signal == "SELL" else price + 15
        reason = f"Pin Bar + LH + Trend Align - Score {score}/100"
        return signal, price, sl, tp1, reason, tp2, score
    else:
        return "NO_TRADE", 0, 0, 0, f"⚪ NO TRADE Score {score}/100 - Insufficient", 0, score

# --- AUTO SCANNER STRONG ONLY ---
def auto_scanner():
    global LAST_ALERT_TIME
    logging.info("ritamariagold V2 STRONG ONLY running - auto scanner ON")
    while True:
        try:
            time.sleep(300) # check every 5 min
            can, _ = is_trading_window()
            if not can:
                continue

            blocked, _ = is_news_block()
            if blocked:
                continue

            m5 = get_xau_data("5min")
            m15 = get_xau_data("15min")
            if not m5 or not m15:
                continue

            res = analyze_v2(m5, m15)

            if res[0] in ["BUY", "SELL"]:
                signal, entry, sl, tp1, reason, tp2, score = res
                if score < 70:
                    continue
                if time.time() - LAST_ALERT_TIME < 3600: # 1 hour cooldown
                    continue

                LAST_ALERT_TIME = time.time()
                text = (
                    f"🔔🔔🔔 *STRONG SIGNAL {score}/100*\n"
                    f"*{signal} XAU/USD*\n"
                    f"Entry: ${entry:.2f}\n"
                    f"SL: ${sl:.2f}\n"
                    f"TP1: ${tp1:.2f} -> CLOSE 50% + BE\n"
                    f"TP2: ${tp2:.2f}\n\n"
                    f"_{reason}_\n\n"
                    f"Lot: 0.01 ONLY"
                )
                send_message(TELEGRAM_CHAT_ID,
