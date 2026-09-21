import os
import time
import threading
import logging
import requests
from flask import Flask, request
import pytz

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

BEIRUT_TZ = pytz.timezone("Asia/Beirut")
app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MIN_SCORE = 70
STRONG_SCORE = 80
ALERT_COOLDOWN = 300  # 5 minutes
LAST_AUTO_ALERT_TIME = 0

# ============================================================
# TELEGRAM
# ============================================================

def send_message(chat_id, text):
    try:
        if not TELEGRAM_TOKEN or not chat_id:
            logging.error("Telegram config missing")
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
        if response.status_code != 200:
            logging.error(f"Telegram error: {response.text}")
            return False
        return True
    except Exception as e:
        logging.error(f"Telegram send error: {e}")
        return False

# ============================================================
# SIGNAL GENERATION
# ============================================================

def generate_signal(candles):
    global LAST_AUTO_ALERT_TIME
    now = time.time()

    # Cooldown check
    if now - LAST_AUTO_ALERT_TIME < ALERT_COOLDOWN:
        logging.info("Cooldown active, skipping signal")
        return

    score_buy, score_sell = 0, 0
    msg_buy, msg_sell = "", ""

    atr = calculate_atr(candles)
    last_close = candles[-1]["close"]
    swing_high, swing_low = get_swings(candles)

    # BUY conditions
    if bullish_engulfing(candles[-2], candles[-1]):
        score_buy += 30
        msg_buy += "📈 Bullish Engulfing\n"
    if bullish_bos_recent(candles):
        score_buy += 25
        msg_buy += "📈 Bullish BOS\n"
    sweep, _ = bullish_sweep_recent(candles)
    if sweep:
        score_buy += 20
        msg_buy += "📈 Bullish Sweep\n"

    # SELL conditions
    if bearish_engulfing(candles[-2], candles[-1]):
        score_sell += 30
        msg_sell += "📉 Bearish Engulfing\n"
    if bearish_bos_recent(candles):
        score_sell += 25
        msg_sell += "📉 Bearish BOS\n"
    sweep, _ = bearish_sweep_recent(candles)
    if sweep:
        score_sell += 20
        msg_sell += "📉 Bearish Sweep\n"

    # BUY signal setup
    if score_buy >= STRONG_SCORE or score_buy >= MIN_SCORE:
        sl = swing_low if swing_low else last_close - atr
        tp = last_close + atr * 2
        risk = abs(last_close - sl)
        reward = abs(tp - last_close)
        rrr = round(reward / risk, 2) if risk > 0 else None

        signal_text = f"""
        ✅ BUY SIGNAL ✅
        Score: {score_buy}
        {msg_buy}
        Entry: {last_close}
        SL: {sl}
        TP: {tp}
        RRR: {rrr}:1
        """
        send_message(TELEGRAM_CHAT_ID, signal_text)
        LAST_AUTO_ALERT_TIME = now

    # SELL signal setup
    if score_sell >= STRONG_SCORE or score_sell >= MIN_SCORE:
        sl = swing_high if swing_high else last_close + atr
        tp = last_close - atr * 2
        risk = abs(sl - last_close)
        reward = abs(last_close - tp)
        rrr = round(reward / risk, 2) if risk > 0 else None

        signal_text = f"""
        ✅ SELL SIGNAL ✅
        Score: {score_sell}
        {msg_sell}
        Entry: {last_close}
        SL: {sl}
        TP: {tp}
        RRR: {rrr}:1
        """
        send_message(TELEGRAM_CHAT_ID, signal_text)
        LAST_AUTO_ALERT_TIME = now

# ============================================================
# MANUAL SIGNAL REPLY
# ============================================================

@app.route("/signal", methods=["POST"])
def manual_signal():
    data = request.json
    text = data.get("text", "")
    if text:
        send_message(TELEGRAM_CHAT_ID, f"📢 Manual Signal: {text}")
        return {"status": "ok"}
    return {"status": "error", "message": "No text provided"}

# ============================================================
# MAIN LOOP
# ============================================================

def run_bot():
    while True:
        candles = get_xau_data("5min")
        if candles:
            generate_signal(candles)
        time.sleep(60)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # ✅ Correct binding for Render
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
