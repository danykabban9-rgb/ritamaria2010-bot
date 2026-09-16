from flask import Flask, request
import requests
import os
import numpy as np

app = Flask(__name__)

TELEGRAM_TOKEN = 8541182452:AAEChxaZ15aKXk5_ccZoZwEnhLUWY-ygA2I
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# --- Indicators & Patterns ---
def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    highs = np.array([float(c["high"]) for c in candles])
    lows = np.array([float(c["low"]) for c in candles])
    closes = np.array([float(c["close"]) for c in candles])
    trs = np.maximum(highs[1:] - lows[1:], 
                     np.abs(highs[1:] - closes[:-1]), 
                     np.abs(lows[1:] - closes[:-1]))
    return trs[-period:].mean()

def detect_candlestick_patterns(candles):
    if len(candles) < 2:
        return None
    last, prev = candles[0], candles[1]
    o1, c1 = float(prev["open"]), float(prev["close"])
    o2, c2 = float(last["open"]), float(last["close"])
    h2, l2 = float(last["high"]), float(last["low"])

    if c2 > o2 and c1 < o1 and c2 > o1 and o2 < c1:
        return "Bullish Engulfing"
    if c2 < o2 and c1 > o1 and o2 > c1 and c2 < o1:
        return "Bearish Engulfing"
    if abs(c2 - o2) <= (h2 - l2) * 0.1:
        return "Doji"
    body = abs(c2 - o2)
    upper_wick = h2 - max(o2, c2)
    lower_wick = min(o2, c2) - l2
    if upper_wick > body * 2:
        return "Pin Bar (Bearish)"
    if lower_wick > body * 2:
        return "Pin Bar (Bullish)"
    return None

def detect_market_structure(candles):
    if len(candles) < 5:
        return None
    lows = [float(c["low"]) for c in candles[:5]]
    highs = [float(c["high"]) for c in candles[:5]]
    if lows[0] < lows[1] < lows[2]:
        return "HL"
    if highs[0] > highs[1] > highs[2]:
        return "LH"
    return "CHOP"

# --- Signal Engine ---
def analyze_signal(m5_data, m15_data):
    pattern = detect_candlestick_patterns(m5_data)
    structure = detect_market_structure(m15_data)
    atr = calc_atr(m5_data, 14)
    price = float(m5_data[0]["close"])

    if not pattern or not structure or not atr:
        return "NO TRADE", None, None, None, "Insufficient confluence"

    if pattern in ["Bullish Engulfing", "Pin Bar (Bullish)"] and structure == "HL":
        entry = price
        sl = entry - atr
        tp = entry + 2 * atr
        return "BUY", entry, sl, tp, f"{pattern} + {structure}"
    elif pattern in ["Bearish Engulfing", "Pin Bar (Bearish)"] and structure == "LH":
        entry = price
        sl = entry + atr
        tp = entry - 2 * atr
        return "SELL", entry, sl, tp, f"{pattern} + {structure}"
    else:
        return "NO TRADE", None, None, None, "Pattern/Structure mismatch"

# --- Telegram Integration ---
def send_message(chat_id, text):
    requests.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    })

@app.route("/telegram", methods=["POST"])
def handle_update():
    data = request.get_json()
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")
    if not chat_id:
        return {"ok": True}

    if text == "/start":
        send_message(chat_id, "🥇 Gold Scalper Pro\nCandlestick + Structure + ATR\nSend /signal")
    elif text == "/signal":
        m5_candles = get_xau_data("5min")
        m15_candles = get_xau_data("15min")
        signal, entry, sl, tp, reason = analyze_signal(m5_candles, m15_candles)
        if signal in ["BUY", "SELL"]:
            reply = f"{'🟢' if signal=='BUY' else '🔴'} *{signal}*\nEntry: ${entry:.2f}\nSL: ${sl:.2f}\nTP: ${tp:.2f}\n\nPattern: {reason}"
        else:
            reply = f"⚪ NO TRADE\n_{reason}_"
        send_message(chat_id, reply)
    return {"ok": True}

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}

@app.route('/')
def index():
    return "Bot is running"

# --- Data Fetch ---
def get_xau_data(interval, limit=100):
    url = "https://api.twelvedata.com/time_series"
    response = requests.get(url, params={
        "symbol": "XAU/USD",
        "interval": interval,
        "outputsize": limit,
        "apikey": TWELVE_DATA_KEY
    })
    data = response.json()
    return data.get("values", []) if "values" in data else []

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
