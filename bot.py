import os
import time
import threading
import logging
import hashlib
import requests

from datetime import datetime, timezone
from flask import Flask, request, jsonify


# ============================================================
# CONFIG
# ============================================================

SYMBOL = "XAU/USD"

SCAN_SECONDS = 60

MIN_SCORE = 80
LOT_SIZE = "0.01 ONLY"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

LAST_AUTO_SETUP_ID = None
LAST_MANUAL_SETUP_ID = None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram variables are missing.")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code != 200:
            logging.error(
                "Telegram error: %s",
                response.text
            )
            return False

        return True

    except Exception as e:
        logging.error(
            "Telegram connection error: %s",
            e
        )
        return False


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(interval, outputsize=100):
    if not TWELVE_DATA_KEY:
        logging.error("TWELVE_DATA_KEY is missing.")
        return []

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "format": "JSON"
    }

    try:
        response = requests.get(
            TWELVE_DATA_URL,
            params=params,
            timeout=20
        )

        if response.status_code != 200:
            logging.error(
                "Twelve Data HTTP error: %s",
                response.status_code
            )
            return []

        data = response.json()

        if "values" not in data:
            logging.error(
                "Twelve Data response: %s",
                data
            )
            return []

        candles = []

        for item in reversed(data["values"]):
            try:
                candles.append({
                    "time": item["datetime"],
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"])
                })
            except (KeyError, ValueError):
                continue

        return candles

    except Exception as e:
        logging.error(
            "Twelve Data connection error: %s",
            e
        )
        return []


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr1 = current["high"] - current["low"]

        tr2 = abs(
            current["high"] - previous["close"]
        )

        tr3 = abs(
            current["low"] - previous["close"]
        )

        true_range = max(
            tr1,
            tr2,
            tr3
        )

        true_ranges.append(true_range)

    if len(true_ranges) < period:
        return None

    recent = true_ranges[-period:]

    return sum(recent) / len(recent)


# ============================================================
# CANDLE ANALYSIS
# ============================================================

def bullish_engulfing(previous, current):
    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(previous, current):
    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def bullish_rejection(candle):
    body = abs(
        candle["close"] - candle["open"]
    )

    lower_wick = (
        min(candle["open"], candle["close"])
        - candle["low"]
    )

    upper_wick = (
        candle["high"]
        - max(candle["open"], candle["close"])
    )

    if body == 0:
        body = 0.00001

    return (
        lower_wick > body * 1.5
        and lower_wick > upper_wick
        and candle["close"] > candle["open"]
    )


def bearish_rejection(candle):
    body = abs(
        candle["close"] - candle["open"]
    )

    upper_wick = (
        candle["high"]
        - max(candle["open"], candle["close"])
    )

    lower_wick = (
        min(candle["open"], candle["close"])
        - candle["low"]
    )

    if body == 0:
        body = 0.00001

    return (
        upper_wick > body * 1.5
        and upper_wick > lower_wick
        and candle["close"] < candle["open"]
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(candles):
    if len(candles) < 20:
        return "NEUTRAL"

    recent = candles[-20:]

    highs = [
        candle["high"]
        for candle in recent
    ]

    lows = [
        candle["low"]
        for candle in recent
    ]

    last_close = recent[-1]["close"]

    previous_high = max(highs[:-3])
    previous_low = min(lows[:-3])

    if last_close > previous_high:
        return "BULLISH"

    if last_close < previous_low:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(candles):
    if len(candles) < 10:
        return None

    previous = candles[-2]
    current = candles[-1]

    recent = candles[-10:-2]

    previous_high = max(
        candle["high"]
        for candle in recent
    )

    previous_low = min(
        candle["low"]
        for candle in recent
    )

    # Bearish liquidity sweep
    if (
        current["high"] > previous_high
        and current["close"] < previous_high
    ):
        return "BEARISH"

    # Bullish liquidity sweep
    if (
        current["low"] < previous_low
        and current["close"] > previous_low
    ):
        return "BULLISH"

    return None


# ============================================================
# SETUP ANALYZER
# ============================================================

def analyze_market():
    candles_m5 = get_candles("5min", 100)

    candles_m1 = get_candles("1min", 100)

    candles_m15 = get_candles("15min", 100)

    if (
        len(candles_m5) < 30
        or len(candles_m1) < 10
        or len(candles_m15) < 20
    ):
        logging.warning(
            "Not enough candle data."
        )
        return None

    m5_structure = market_structure(
        candles_m5
    )

    m15_structure = market_structure(
        candles_m15
    )

    sweep = detect_liquidity_sweep(
        candles_m5
    )

    previous_m1 = candles_m1[-2]
    current_m1 = candles_m1[-1]

    atr = calculate_atr(
        candles_m5,
        14
    )

    if atr is None or atr <= 0:
        return None

    price = current_m1["close"]

    buy_score = 0
    sell_score = 0

    reasons_buy = []
    reasons_sell = []

    # --------------------------------------------------------
    # M15 STRUCTURE
    # --------------------------------------------------------

    if m15_structure == "BULLISH":
        buy_score += 20
        reasons_buy.append("M15 bullish structure")

    elif m15_structure == "BEARISH":
        sell_score += 20
        reasons_sell.append("M15 bearish structure")

    # --------------------------------------------------------
    # M5 STRUCTURE
    # --------------------------------------------------------

    if m5_structure == "BULLISH":
        buy_score += 20
        reasons_buy.append("M5 bullish structure")

    elif m5_structure == "BEARISH":
        sell_score += 20
        reasons_sell.append("M5 bearish structure")

    # --------------------------------------------------------
    # LIQUIDITY SWEEP
    # --------------------------------------------------------

    if sweep == "BULLISH":
        buy_score += 20
        reasons_buy.append("Bullish liquidity sweep")

    elif sweep == "BEARISH":
        sell_score += 20
        reasons_sell.append("Bearish liquidity sweep")

    # --------------------------------------------------------
    # M1 CANDLE
    # --------------------------------------------------------

    if bullish_engulfing(
        previous_m1,
        current_m1
    ):
        buy_score += 20
        reasons_buy.append("M1 bullish engulfing")

    if bearish_engulfing(
        previous_m1,
        current_m1
    ):
        sell_score += 20
        reasons_sell.append("M1 bearish engulfing")

    if bullish_rejection(current_m1):
        buy_score += 15
        reasons_buy.append("M1 bullish rejection")

    if bearish_rejection(current_m1):
        sell_score += 15
        reasons_sell.append("M1 bearish rejection")

    # --------------------------------------------------------
    # CANDLE DISPLACEMENT
    # --------------------------------------------------------

    candle_range = (
        current_m1["high"]
        - current_m1["low"]
    )

    if candle_range > atr * 0.20:

        if current_m1["close"] > current_m1["open"]:
            buy_score += 10
            reasons_buy.append("Bullish displacement")

        elif current_m1["close"] < current_m1["open"]:
            sell_score += 10
            reasons_sell.append("Bearish displacement")

    # --------------------------------------------------------
    # SELECT DIRECTION
    # --------------------------------------------------------

    if (
        buy_score >= MIN_SCORE
        and buy_score > sell_score
    ):
        direction = "BUY"
        score = buy_score
        reasons = reasons_buy

    elif (
        sell_score >= MIN_SCORE
        and sell_score > buy_score
    ):
        direction = "SELL"
        score = sell_score
        reasons = reasons_sell

    else:
        return None

    # --------------------------------------------------------
    # ENTRY / SL / TP
    # --------------------------------------------------------

    if direction == "BUY":

        entry = price

        sl = entry - (
            atr * 0.80
        )

        tp1 = entry + (
            atr * 0.80
        )

        tp2 = entry + (
            atr * 1.50
        )

    else:

        entry = price

        sl = entry + (
            atr * 0.80
        )

        tp1 = entry - (
            atr * 0.80
        )

        tp2 = entry - (
            atr * 1.50
        )

    return {
        "direction": direction,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "atr": atr,
        "reasons": reasons,
        "candle_time": current_m1["time"]
    }


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def format_signal(setup):

    direction = setup["direction"]

    emoji = "🟢" if direction == "BUY" else "🔴"

    reasons = "\n".join(
        f"• {reason}"
        for reason in setup["reasons"]
    )

    message = (
        f"🚨 XAUUSD SCALP SIGNAL\n"
        f"\n"
        f"{emoji} {direction}\n"
        f"\n"
        f"ENTRY: {setup['entry']:.2f}\n"
        f"SL: {setup['sl']:.2f}\n"
        f"TP1: {setup['tp1']:.2f}\n"
        f"TP2: {setup['tp2']:.2f}\n"
        f"\n"
        f"SCORE: {setup['score']}/100+\n"
        f"LOT: {LOT_SIZE}\n"
        f"\n"
        f"WHY:\n"
        f"{reasons}\n"
        f"\n"
        f"⚠️ Manual execution only.\n"
        f"Wait for price confirmation."
    )

    return message


# ============================================================
# SETUP ID
# ============================================================

def setup_id(setup):
    raw = (
        f"{setup['direction']}-"
        f"{setup['candle_time']}-"
        f"{round(setup['entry'], 2)}"
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()


# ============================================================
# AUTOMATIC SCANNER
# ============================================================

def scanner():

    global LAST_AUTO_SETUP_ID

    logging.info(
        "Gold scanner started."
    )

    while True:

        try:

            setup = analyze_market()

            if setup:

                current_id = setup_id(
                    setup
                )

                if current_id != LAST_AUTO_SETUP_ID:

                    message = format_signal(
                        setup
                    )

                    if send_telegram(message):

                        LAST_AUTO_SETUP_ID = current_id

                        logging.info(
                            "Signal sent: %s",
                            setup["direction"]
                        )

                else:

                    logging.info(
                        "Duplicate setup ignored."
                    )

            else:

                logging.info(
                    "No high-quality setup."
                )

        except Exception as e:

            logging.exception(
                "Scanner error: %s",
                e
            )

        time.sleep(
            SCAN_SECONDS
        )


# ============================================================
# FLASK
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "online",
        "bot": "XAUUSD Candle Expert",
        "symbol": SYMBOL,
        "scan_seconds": SCAN_SECONDS,
        "minimum_score": MIN_SCORE
    })


@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "status": "healthy"
    })


# ============================================================
# MANUAL SIGNAL
# ============================================================

@app.route("/signal", methods=["GET", "POST"])
def manual_signal():

    global LAST_MANUAL_SETUP_ID

    try:

        setup = analyze_market()

        if not setup:

            message = (
                "⏳ NO HIGH-QUALITY SETUP\n\n"
                "The candle structure does not "
                "currently meet the 80-point "
                "confirmation threshold.\n\n"
                "No trade."
            )

            send_telegram(message)

            return jsonify({
                "status": "no_setup"
            })

        current_id = setup_id(
            setup
        )

        message = format_signal(
            setup
        )

        if current_id == LAST_MANUAL_SETUP_ID:

            message = (
                "ℹ️ SAME SETUP\n\n"
                + message
            )

        if send_telegram(message):

            LAST_MANUAL_SETUP_ID = current_id

            return jsonify({
                "status": "signal_sent",
                "direction": setup["direction"],
                "score": setup["score"]
            })

        return jsonify({
            "status": "telegram_failed"
        }), 500

    except Exception as e:

        logging.exception(
            "Manual signal error: %s",
            e
        )

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        message = data.get(
            "message",
            {}
        )

        text_message = message.get(
            "text",
            ""
        )

        if text_message.startswith(
            "/signal"
        ):

            setup = analyze_market()

            if not setup:

                send_telegram(
                    "⏳ NO HIGH-QUALITY SETUP\n\n"
                    "No trade right now."
                )

            else:

                send_telegram(
                    format_signal(setup)
                )

        elif text_message.startswith(
            "/start"
        ):

            send_telegram(
                "🟢 XAUUSD Candle Expert ONLINE\n\n"
                "Commands:\n"
                "/signal - scan gold now\n"
                "/status - bot status"
            )

        elif text_message.startswith(
            "/status"
        ):

            send_telegram(
                "🟢 BOT ONLINE\n\n"
                f"Symbol: {SYMBOL}\n"
                f"Scan: every {SCAN_SECONDS}s\n"
                f"Minimum score: {MIN_SCORE}\n"
                f"Lot: {LOT_SIZE}"
            )

        return jsonify({
            "ok": True
        })

    except Exception as e:

        logging.exception(
            "Webhook error: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# START SCANNER
# ============================================================

def start_scanner():

    thread = threading.Thread(
        target=scanner,
        daemon=True
    )

    thread.start()


start_scanner()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
