import os
import time
import threading
import logging
import requests
from flask import Flask, request
from datetime import datetime
import pytz

# ============================================================
# EXISTING RENDER VARIABLES — DO NOT CHANGE
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

BEIRUT_TZ = pytz.timezone("Asia/Beirut")

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# Prevent duplicate alerts
LAST_SIGNAL_CANDLE = None
LAST_SIGNAL = None
LAST_ALERT_TIME = 0

# ============================================================
# TELEGRAM
# ============================================================

def send_message(chat_id, text):
    try:
        if not TELEGRAM_TOKEN or not chat_id:
            logging.error("Telegram variables missing")
            return False

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=10
        )

        if r.status_code != 200:
            logging.error(f"Telegram error: {r.text}")
            return False

        return True

    except Exception as e:
        logging.error(f"Telegram send error: {e}")
        return False


# ============================================================
# TRADING WINDOW
# ============================================================

def is_trading_window():
    now = datetime.now(BEIRUT_TZ)
    h = now.hour

    # Main London/NY overlap periods adapted to Beirut time.
    # Avoid overnight/low-liquidity conditions.
    if 9 <= h < 12:
        return True

    if 14 <= h < 19:
        return True

    return False


# ============================================================
# TWELVE DATA
# ============================================================

def get_xau_data(interval, outputsize=120):

    try:
        url = (
            "https://api.twelvedata.com/time_series"
            f"?symbol=XAU/USD"
            f"&interval={interval}"
            f"&apikey={TWELVE_DATA_KEY}"
            f"&outputsize={outputsize}"
            f"&order=ASC"
        )

        r = requests.get(url, timeout=15)
        data = r.json()

        if "values" not in data:
            logging.error(f"Twelve Data: {data}")
            return None

        values = data["values"]

        candles = []

        for x in values:
            try:
                candles.append({
                    "datetime": x["datetime"],
                    "open": float(x["open"]),
                    "high": float(x["high"]),
                    "low": float(x["low"]),
                    "close": float(x["close"])
                })
            except:
                continue

        return candles

    except Exception as e:
        logging.error(f"Market data error: {e}")
        return None


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def candle_parts(c):
    body = abs(c["close"] - c["open"])
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    total = c["high"] - c["low"]

    return body, upper, lower, total


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def bullish_rejection(c):
    body, upper, lower, total = candle_parts(c)

    if total <= 0:
        return False

    return (
        lower >= body * 1.5
        and lower > upper
        and c["close"] > c["low"] + total * 0.55
    )


def bearish_rejection(c):
    body, upper, lower, total = candle_parts(c)

    if total <= 0:
        return False

    return (
        upper >= body * 1.5
        and upper > lower
        and c["close"] < c["high"] - total * 0.55
    )


def bullish_engulfing(prev, c):

    return (
        bearish(prev)
        and bullish(c)
        and c["open"] <= prev["close"]
        and c["close"] >= prev["open"]
        and abs(c["close"] - c["open"])
        > abs(prev["close"] - prev["open"])
    )


def bearish_engulfing(prev, c):

    return (
        bullish(prev)
        and bearish(c)
        and c["open"] >= prev["close"]
        and c["close"] <= prev["open"]
        and abs(c["close"] - c["open"])
        > abs(prev["close"] - prev["open"])
    )


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


# ============================================================
# MARKET STRUCTURE
# ============================================================

def recent_high(candles, lookback=8):
    if len(candles) < lookback:
        return None

    return max(c["high"] for c in candles[-lookback:])


def recent_low(candles, lookback=8):
    if len(candles) < lookback:
        return None

    return min(c["low"] for c in candles[-lookback:])


def market_structure(candles):

    if len(candles) < 20:
        return "NEUTRAL"

    # Divide recent candles into two groups
    old = candles[-16:-8]
    new = candles[-8:]

    old_high = max(c["high"] for c in old)
    old_low = min(c["low"] for c in old)

    new_high = max(c["high"] for c in new)
    new_low = min(c["low"] for c in new)

    if new_high > old_high and new_low > old_low:
        return "BULLISH"

    if new_high < old_high and new_low < old_low:
        return "BEARISH"

    # Break-of-structure confirmation
    last = candles[-1]

    if last["close"] > old_high:
        return "BULLISH"

    if last["close"] < old_low:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def bullish_liquidity_sweep(candles):

    if len(candles) < 12:
        return False

    c = candles[-1]

    previous_low = min(x["low"] for x in candles[-11:-1])

    return (
        c["low"] < previous_low
        and c["close"] > previous_low
    )


def bearish_liquidity_sweep(candles):

    if len(candles) < 12:
        return False

    c = candles[-1]

    previous_high = max(x["high"] for x in candles[-11:-1])

    return (
        c["high"] > previous_high
        and c["close"] < previous_high
    )


# ============================================================
# BREAK OF STRUCTURE
# ============================================================

def bullish_bos(candles):

    if len(candles) < 12:
        return False

    c = candles[-1]

    previous_high = max(x["high"] for x in candles[-11:-1])

    return c["close"] > previous_high


def bearish_bos(candles):

    if len(candles) < 12:
        return False

    c = candles[-1]

    previous_low = min(x["low"] for x in candles[-11:-1])

    return c["close"] < previous_low


# ============================================================
# DISPLACEMENT
# ============================================================

def bullish_displacement(candles):

    if len(candles) < 6:
        return False

    c = candles[-1]

    body, upper, lower, total = candle_parts(c)

    if total <= 0:
        return False

    previous_bodies = []

    for x in candles[-6:-1]:
        b, _, _, _ = candle_parts(x)
        previous_bodies.append(b)

    avg_body = sum(previous_bodies) / len(previous_bodies)

    return (
        bullish(c)
        and body > avg_body * 1.5
        and body / total > 0.55
    )


def bearish_displacement(candles):

    if len(candles) < 6:
        return False

    c = candles[-1]

    body, upper, lower, total = candle_parts(c)

    if total <= 0:
        return False

    previous_bodies = []

    for x in candles[-6:-1]:
        b, _, _, _ = candle_parts(x)
        previous_bodies.append(b)

    avg_body = sum(previous_bodies) / len(previous_bodies)

    return (
        bearish(c)
        and body > avg_body * 1.5
        and body / total > 0.55
    )


# ============================================================
# MAIN CANDLE STRUCTURE ENGINE
# ============================================================

def analyze_pro(m1, m5, m15):

    if (
        not m1
        or not m5
        or not m15
        or len(m1) < 30
        or len(m5) < 30
        or len(m15) < 30
    ):
        return None

    # Use the latest CLOSED candle, not a constantly moving candle.
    m1c = m1[:-1]
    m5c = m5[:-1]
    m15c = m15[:-1]

    price = m1c[-1]["close"]

    # --------------------------------------------------------
    # MARKET STRUCTURE
    # --------------------------------------------------------

    trend15 = market_structure(m15c)
    trend5 = market_structure(m5c)

    # --------------------------------------------------------
    # M5 STRUCTURE
    # --------------------------------------------------------

    buy_sweep = bullish_liquidity_sweep(m5c)
    sell_sweep = bearish_liquidity_sweep(m5c)

    buy_bos = bullish_bos(m5c)
    sell_bos = bearish_bos(m5c)

    # --------------------------------------------------------
    # M1 CONFIRMATION
    # --------------------------------------------------------

    m1_last = m1c[-1]
    m1_prev = m1c[-2]

    buy_rejection = bullish_rejection(m1_last)
    sell_rejection = bearish_rejection(m1_last)

    buy_engulf = bullish_engulfing(m1_prev, m1_last)
    sell_engulf = bearish_engulfing(m1_prev, m1_last)

    buy_disp = bullish_displacement(m1c)
    sell_disp = bearish_displacement(m1c)

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr = calculate_atr(m5c, 14)

    if atr is None or atr <= 0:
        return None

    # Avoid abnormal volatility
    last_range = m5c[-1]["high"] - m5c[-1]["low"]

    if last_range > atr * 2.8:
        return None

    # ========================================================
    # BUY SCORE
    # ========================================================

    buy_score = 0
    buy_reasons = []

    if trend15 == "BULLISH":
        buy_score += 20
        buy_reasons.append("M15 bullish structure")

    if trend5 == "BULLISH":
        buy_score += 15
        buy_reasons.append("M5 bullish structure")

    if buy_sweep:
        buy_score += 20
        buy_reasons.append("M5 sell-side liquidity sweep")

    if buy_bos:
        buy_score += 15
        buy_reasons.append("M5 bullish BOS")

    if buy_rejection:
        buy_score += 10
        buy_reasons.append("M1 bullish rejection")

    if buy_engulf:
        buy_score += 10
        buy_reasons.append("M1 bullish engulfing")

    if buy_disp:
        buy_score += 10
        buy_reasons.append("M1 bullish displacement")

    # ========================================================
    # SELL SCORE
    # ========================================================

    sell_score = 0
    sell_reasons = []

    if trend15 == "BEARISH":
        sell_score += 20
        sell_reasons.append("M15 bearish structure")

    if trend5 == "BEARISH":
        sell_score += 15
        sell_reasons.append("M5 bearish structure")

    if sell_sweep:
        sell_score += 20
        sell_reasons.append("M5 buy-side liquidity sweep")

    if sell_bos:
        sell_score += 15
        sell_reasons.append("M5 bearish BOS")

    if sell_rejection:
        sell_score += 10
        sell_reasons.append("M1 bearish rejection")

    if sell_engulf:
        sell_score += 10
        sell_reasons.append("M1 bearish engulfing")

    if sell_disp:
        sell_score += 10
        sell_reasons.append("M1 bearish displacement")

    # ========================================================
    # DECIDE
    # ========================================================

    if buy_score < 80 and sell_score < 80:
        return {
            "signal": "NO_TRADE",
            "score": max(buy_score, sell_score),
            "price": price,
            "reason": (
                f"M15={trend15} | M5={trend5} | "
                f"BUY={buy_score} SELL={sell_score}"
            )
        }

    if buy_score >= 80 and buy_score > sell_score:

        entry = price

        # Structure/ATR based SL
        structure_low = recent_low(m5c, 8)

        sl_distance = max(
            entry - structure_low + 0.30,
            atr * 0.9
        )

        sl = entry - sl_distance

        tp1 = entry + sl_distance * 1.5
        tp2 = entry + sl_distance * 2.3

        return {
            "signal": "BUY",
            "score": buy_score,
            "price": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "atr": atr,
            "trend15": trend15,
            "trend5": trend5,
            "reasons": buy_reasons
        }

    if sell_score >= 80 and sell_score > buy_score:

        entry = price

        structure_high = recent_high(m5c, 8)

        sl_distance = max(
            structure_high - entry + 0.30,
            atr * 0.9
        )

        sl = entry + sl_distance

        tp1 = entry - sl_distance * 1.5
        tp2 = entry - sl_distance * 2.3

        return {
            "signal": "SELL",
            "score": sell_score,
            "price": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "atr": atr,
            "trend15": trend15,
            "trend5": trend5,
            "reasons": sell_reasons
        }

    return {
        "signal": "NO_TRADE",
        "score": max(buy_score, sell_score),
        "price": price,
        "reason": "Conflicting BUY/SELL structure"
    }


# ============================================================
# FORMAT TELEGRAM SIGNAL
# ============================================================

def format_signal(r):

    sig = r["signal"]

    if sig == "BUY":
        emoji = "🟢"
    else:
        emoji = "🔴"

    reasons = "\n".join(
        f"✓ {x}" for x in r["reasons"]
    )

    return (
        f"{emoji} XAUUSD PRO SCALP {sig}\n"
        f"🔥 CONFLUENCE: {r['score']}/100\n\n"
        f"Entry: ${r['price']:.2f}\n"
        f"SL:    ${r['sl']:.2f}\n"
        f"TP1:   ${r['tp1']:.2f}\n"
        f"TP2:   ${r['tp2']:.2f}\n\n"
        f"M15: {r['trend15']}\n"
        f"M5:  {r['trend5']}\n\n"
        f"{reasons}\n\n"
        f"⚠️ LOT: 0.01 ONLY\n"
        f"⚠️ Signal is candle-structure based."
    )


# ============================================================
# SCANNER
# ============================================================

def run_scan():

    global LAST_SIGNAL_CANDLE
    global LAST_SIGNAL
    global LAST_ALERT_TIME

    m1 = get_xau_data("1min", 120)
    m5 = get_xau_data("5min", 120)
    m15 = get_xau_data("15min", 120)

    if not m1 or not m5 or not m15:
        logging.warning("Not enough market data")
        return

    result = analyze_pro(m1, m5, m15)

    if not result:
        return

    logging.info(
        f"Analysis: {result['signal']} "
        f"{result.get('score', 0)}/100"
    )

    if result["signal"] not in ["BUY", "SELL"]:
        return

    score = result["score"]

    if score < 80:
        return

    # Candle ID prevents repeated alert on same setup
    candle_id = m1[-2]["datetime"]

    # Don't send identical signal repeatedly
    if candle_id == LAST_SIGNAL_CANDLE and result["signal"] == LAST_SIGNAL:
        return

    # Minimum 10-minute cooldown
    if time.time() - LAST_ALERT_TIME < 600:
        return

    text = format_signal(result)

    if send_message(TELEGRAM_CHAT_ID, text):
        LAST_SIGNAL_CANDLE = candle_id
        LAST_SIGNAL = result["signal"]
        LAST_ALERT_TIME = time.time()

        logging.info(
            f"ALERT SENT: {result['signal']} "
            f"{result['score']}/100"
        )


def auto_scanner():

    logging.info(
        "CANDLE STRUCTURE PRO SCALPER V1 STARTED"
    )

    # Give Flask/Render a moment to start
    time.sleep(10)

    while True:

        try:

            if is_trading_window():
                run_scan()
            else:
                logging.info("Outside trading window")

            # Scan every minute
            time.sleep(60)

        except Exception as e:

            logging.error(
                f"Scanner error: {e}"
            )

            time.sleep(30)


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return (
        "Candle Structure Pro Scalper V1 "
        "running"
    )


@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    try:

        data = request.get_json()

        if not data:
            return "ok"

        if "message" not in data:
            return "ok"

        message = data["message"]

        if "text" not in message:
            return "ok"

        text = message["text"].strip().lower()

        chat_id = message["chat"]["id"]

        # Manual signal command
        if text.startswith("/signal"):

            m1 = get_xau_data("1min", 120)
            m5 = get_xau_data("5min", 120)
            m15 = get_xau_data("15min", 120)

            result = analyze_pro(
                m1,
                m5,
                m15
            )

            if not result:

                send_message(
                    chat_id,
                    "⚠️ DATA ERROR — TRY AGAIN"
                )

                return "ok"

            if result["signal"] in ["BUY", "SELL"]:

                send_message(
                    chat_id,
                    format_signal(result)
                )

            else:

                send_message(
                    chat_id,
                    (
                        "⚪ NO TRADE\n\n"
                        f"Current structure:\n"
                        f"{result['reason']}\n\n"
                        "Waiting for stronger confluence."
                    )
                )

        elif text == "/start":

            send_message(
                chat_id,
                (
                    "🤖 Candle Structure Pro Scalper V1\n\n"
                    "Commands:\n"
                    "/signal — analyze XAUUSD now\n\n"
                    "Automatic scanner is ON."
                )
            )

        return "ok"

    except Exception as e:

        logging.error(
            f"Webhook error: {e}"
        )

        return "ok"


# ============================================================
# START SCANNER
# ============================================================

threading.Thread(
    target=auto_scanner,
    daemon=True
).start()


# ============================================================
# START FLASK
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
