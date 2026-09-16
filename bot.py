from flask import Flask, request
import requests
import os
import numpy as np
import logging

app = Flask(__name__)

# IMPORTANT:
# Put TELEGRAM_TOKEN and TWELVE_DATA_KEY in Render -> Environment.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

if not TELEGRAM_TOKEN:
    logging.warning("TELEGRAM_TOKEN is not set.")
if not TWELVE_DATA_KEY:
    logging.warning("TWELVE_DATA_KEY is not set.")

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ---------------- INDICATORS ----------------

def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    highs = np.array([float(c["high"]) for c in candles])
    lows = np.array([float(c["low"]) for c in candles])
    closes = np.array([float(c["close"]) for c in candles])

    trs = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
    )

    return float(trs[-period:].mean())


def detect_candlestick_patterns(candles):
    if len(candles) < 2:
        return None

    # Twelve Data normally returns newest candle first.
    last = candles[0]
    prev = candles[1]

    o1, c1 = float(prev["open"]), float(prev["close"])
    o2, c2 = float(last["open"]), float(last["close"])
    h2, l2 = float(last["high"]), float(last["low"])

    # Bullish engulfing
    if c2 > o2 and c1 < o1 and c2 > o1 and o2 < c1:
        return "Bullish Engulfing"

    # Bearish engulfing
    if c2 < o2 and c1 > o1 and o2 > c1 and c2 < o1:
        return "Bearish Engulfing"

    candle_range = h2 - l2
    if candle_range <= 0:
        return None

    # Doji
    if abs(c2 - o2) <= candle_range * 0.10:
        return "Doji"

    body = abs(c2 - o2)
    upper_wick = h2 - max(o2, c2)
    lower_wick = min(o2, c2) - l2

    # Avoid treating a zero-body candle as a pin bar.
    if body > 0:
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


# ---------------- SIGNAL ENGINE ----------------

def analyze_signal(m5_data, m15_data):
    if not m5_data or not m15_data:
        return "NO TRADE", None, None, None, "No XAU/USD data"

    pattern = detect_candlestick_patterns(m5_data)
    structure = detect_market_structure(m15_data)
    atr = calc_atr(m5_data, 14)

    try:
        price = float(m5_data[0]["close"])
    except (KeyError, ValueError, TypeError):
        return "NO TRADE", None, None, None, "Invalid price data"

    if not pattern or not structure or not atr or atr <= 0:
        return "NO TRADE", None, None, None, "Insufficient confluence"

    if pattern in ["Bullish Engulfing", "Pin Bar (Bullish)"] and structure == "HL":
        entry = price
        sl = entry - atr
        tp = entry + 2 * atr
        return "BUY", entry, sl, tp, f"{pattern} + {structure}"

    if pattern in ["Bearish Engulfing", "Pin Bar (Bearish)"] and structure == "LH":
        entry = price
        sl = entry + atr
        tp = entry - 2 * atr
        return "SELL", entry, sl, tp, f"{pattern} + {structure}"

    return "NO TRADE", None, None, None, "Pattern/Structure mismatch"


# ---------------- TELEGRAM ----------------

def send_message(chat_id, text):
    if not TELEGRAM_TOKEN:
        logging.error("Cannot send Telegram message: TELEGRAM_TOKEN is missing.")
        return False

    try:
        response = requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            },
            timeout=15
        )

        logging.info("Telegram sendMessage: %s", response.text)
        return response.ok

    except requests.RequestException as e:
        logging.exception("Telegram sendMessage error: %s", e)
        return False


def get_bot_info():
    if not TELEGRAM_TOKEN:
        return None

    try:
        response = requests.get(
            f"{BASE_URL}/getMe",
            timeout=15
        )
        logging.info("Telegram getMe: %s", response.text)
        return response.json()
    except requests.RequestException as e:
        logging.exception("Telegram getMe error: %s", e)
        return None


def set_webhook():
    if not TELEGRAM_TOKEN:
        logging.error("Webhook NOT set: TELEGRAM_TOKEN is missing.")
        return

    # Render automatically provides this variable for deployed services.
    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if not render_url:
        logging.error("Webhook NOT set: RENDER_EXTERNAL_URL is missing.")
        return

    webhook_url = render_url.rstrip("/") + "/telegram"

    try:
        response = requests.post(
            f"{BASE_URL}/setWebhook",
            json={"url": webhook_url},
            timeout=15
        )

        logging.info("Webhook URL: %s", webhook_url)
        logging.info("Telegram setWebhook response: %s", response.text)

    except requests.RequestException as e:
        logging.exception("setWebhook error: %s", e)


# ---------------- ROUTES ----------------

@app.route("/telegram", methods=["POST"])
def handle_update():
    data = request.get_json(silent=True) or {}

    message = data.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    logging.info("Telegram message received: %s", text)

    if text == "/start":
        send_message(
            chat_id,
            "🥇 Gold Scalper Pro\n\n"
            "Candlestick + Market Structure + ATR\n\n"
            "Commands:\n"
            "/signal - Get current XAU/USD signal\n"
            "/status - Check bot status"
        )

    elif text == "/status":
        send_message(
            chat_id,
            "✅ Bot is online.\n"
            "Webhook is active.\n"
            "Use /signal for XAU/USD analysis."
        )

    elif text == "/signal":
        m5_candles = get_xau_data("5min")
        m15_candles = get_xau_data("15min")

        signal, entry, sl, tp, reason = analyze_signal(
            m5_candles,
            m15_candles
        )

        if signal in ["BUY", "SELL"]:
            emoji = "🟢" if signal == "BUY" else "🔴"

            reply = (
                f"{emoji} *{signal}*\n"
                f"Entry: `${entry:.2f}`\n"
                f"SL: `${sl:.2f}`\n"
                f"TP: `${tp:.2f}`\n\n"
                f"Reason: {reason}"
            )
        else:
            reply = (
                "⚪ *NO TRADE*\n\n"
                f"_{reason}_"
            )

        send_message(chat_id, reply)

    else:
        send_message(
            chat_id,
            "Use /start, /status, or /signal."
        )

    return {"ok": True}


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


@app.route("/", methods=["GET"])
def index():
    return "Gold Telegram Bot is running."


# ---------------- TWELVE DATA ----------------

def get_xau_data(interval, limit=100):
    if not TWELVE_DATA_KEY:
        logging.error("TWELVE_DATA_KEY is missing.")
        return []

    url = "https://api.twelvedata.com/time_series"

    try:
        response = requests.get(
            url,
            params={
                "symbol": "XAU/USD",
                "interval": interval,
                "outputsize": limit,
                "apikey": TWELVE_DATA_KEY
            },
            timeout=20
        )

        response.raise_for_status()
        data = response.json()

        if "values" not in data:
            logging.error("Twelve Data error: %s", data)
            return []

        return data["values"]

    except (requests.RequestException, ValueError) as e:
        logging.exception("Twelve Data error: %s", e)
        return []


# ---------------- START ----------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )

    print("Starting Gold Telegram Bot...")

    bot_info = get_bot_info()
    if bot_info and bot_info.get("ok"):
        print("Telegram connection: OK")
        print("Bot username:", bot_info["result"].get("username"))
    else:
        print("Telegram connection: FAILED")

    set_webhook()

    port = int(os.getenv("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=port
    )
