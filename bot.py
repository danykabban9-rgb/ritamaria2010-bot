import os
import time
import threading
import logging
import hashlib
import requests

from flask import Flask, request
from datetime import datetime
import pytz


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

SYMBOL = "XAU/USD"

BEIRUT_TZ = pytz.timezone("Asia/Beirut")

# Your preferred scanning windows
SESSION_1_START = 9
SESSION_1_END = 12

SESSION_2_START = 14
SESSION_2_END = 19

SCAN_SECONDS = 60

# Minimum setup quality
MIN_SCORE = 78

# Keep small-account signals conservative
LOT_SIZE = "0.01 ONLY"

# Prevent repeated signals
LAST_SIGNAL_HASH = None
LAST_SIGNAL_TIME = None


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram environment variables missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        r = requests.post(url, json=payload, timeout=15)

        if r.status_code != 200:
            logging.error("Telegram error: %s", r.text)
            return False

        return True

    except Exception as e:
        logging.error("Telegram exception: %s", e)
        return False


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(interval, outputsize=150):

    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "timezone": "Asia/Beirut",
        "order": "desc"
    }

    try:

        r = requests.get(url, params=params, timeout=20)

        data = r.json()

        if data.get("status") == "error":
            logging.error(
                "Twelve Data %s error: %s",
                interval,
                data.get("message")
            )
            return []

        values = data.get("values", [])

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

            except Exception:
                continue

        # API gives newest first.
        # We want oldest -> newest.
        candles.reverse()

        return candles

    except Exception as e:

        logging.error(
            "Twelve Data exception %s: %s",
            interval,
            e
        )

        return []


# ============================================================
# BASIC FUNCTIONS
# ============================================================

def candle_body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(c["high"] - c["low"], 0.00001)


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def is_bullish(c):
    return c["close"] > c["open"]


def is_bearish(c):
    return c["close"] < c["open"]


def average_range(candles, period=14):

    if len(candles) < period:
        return None

    recent = candles[-period:]

    return sum(candle_range(x) for x in recent) / period


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_direction(candles):

    if len(candles) < 20:
        return "NEUTRAL"

    recent = candles[-20:]

    highs = [x["high"] for x in recent]
    lows = [x["low"] for x in recent]

    first_high = max(highs[:10])
    second_high = max(highs[10:])

    first_low = min(lows[:10])
    second_low = min(lows[10:])

    if second_high > first_high and second_low > first_low:
        return "BULLISH"

    if second_high < first_high and second_low < first_low:
        return "BEARISH"

    return "RANGE"


# ============================================================
# BOS
# ============================================================

def detect_bos(candles):

    if len(candles) < 12:
        return "NONE"

    prev = candles[-7:-2]
    current = candles[-1]

    previous_high = max(x["high"] for x in prev)
    previous_low = min(x["low"] for x in prev)

    if current["close"] > previous_high:
        return "BULLISH"

    if current["close"] < previous_low:
        return "BEARISH"

    return "NONE"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def liquidity_sweep(candles):

    if len(candles) < 12:
        return "NONE"

    current = candles[-1]

    previous = candles[-7:-1]

    previous_high = max(x["high"] for x in previous)
    previous_low = min(x["low"] for x in previous)

    # Bullish sweep:
    # price takes previous low then closes back above it
    if (
        current["low"] < previous_low
        and current["close"] > previous_low
    ):
        return "BULLISH"

    # Bearish sweep:
    # price takes previous high then closes back below it
    if (
        current["high"] > previous_high
        and current["close"] < previous_high
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# REJECTION CANDLE
# ============================================================

def rejection_signal(c):

    rng = candle_range(c)
    body = candle_body(c)

    if rng <= 0:
        return "NONE"

    lw = lower_wick(c)
    uw = upper_wick(c)

    # Bullish rejection
    if (
        lw >= body * 1.5
        and lw >= uw * 1.5
        and c["close"] > c["open"]
    ):
        return "BULLISH"

    # Bearish rejection
    if (
        uw >= body * 1.5
        and uw >= lw * 1.5
        and c["close"] < c["open"]
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# ENGULFING
# ============================================================

def engulfing_signal(candles):

    if len(candles) < 2:
        return "NONE"

    previous = candles[-2]
    current = candles[-1]

    # Bullish engulfing
    if (
        is_bearish(previous)
        and is_bullish(current)
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    ):
        return "BULLISH"

    # Bearish engulfing
    if (
        is_bullish(previous)
        and is_bearish(current)
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# DISPLACEMENT
# ============================================================

def displacement(candles):

    if len(candles) < 16:
        return "NONE"

    current = candles[-1]

    avg = average_range(candles[:-1], 14)

    if not avg:
        return "NONE"

    rng = candle_range(current)

    if rng < avg * 1.5:
        return "NONE"

    if is_bullish(current):
        return "BULLISH"

    if is_bearish(current):
        return "BEARISH"

    return "NONE"


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


# ============================================================
# SETUP ANALYSIS
# ============================================================

def analyze_market(m15, m5, m1):

    if len(m15) < 30 or len(m5) < 30 or len(m1) < 30:
        return None

    m15_direction = structure_direction(m15)
    m5_direction = structure_direction(m5)

    m5_bos = detect_bos(m5)

    m1_sweep = liquidity_sweep(m1)
    m1_rejection = rejection_signal(m1)
    m1_engulfing = engulfing_signal(m1)
    m1_displacement = displacement(m1)

    atr = calculate_atr(m1)

    if not atr:
        return None

    score_buy = 0
    score_sell = 0

    # --------------------------------------------------------
    # M15 STRUCTURE
    # --------------------------------------------------------

    if m15_direction == "BULLISH":
        score_buy += 20

    if m15_direction == "BEARISH":
        score_sell += 20

    # --------------------------------------------------------
    # M5 STRUCTURE
    # --------------------------------------------------------

    if m5_direction == "BULLISH":
        score_buy += 15

    if m5_direction == "BEARISH":
        score_sell += 15

    # --------------------------------------------------------
    # M5 BOS
    # --------------------------------------------------------

    if m5_bos == "BULLISH":
        score_buy += 20

    if m5_bos == "BEARISH":
        score_sell += 20

    # --------------------------------------------------------
    # M1 LIQUIDITY
    # --------------------------------------------------------

    if m1_sweep == "BULLISH":
        score_buy += 20

    if m1_sweep == "BEARISH":
        score_sell += 20

    # --------------------------------------------------------
    # M1 CANDLE CONFIRMATION
    # --------------------------------------------------------

    if m1_rejection == "BULLISH":
        score_buy += 10

    if m1_rejection == "BEARISH":
        score_sell += 10

    if m1_engulfing == "BULLISH":
        score_buy += 10

    if m1_engulfing == "BEARISH":
        score_sell += 10

    if m1_displacement == "BULLISH":
        score_buy += 10

    if m1_displacement == "BEARISH":
        score_sell += 10

    # --------------------------------------------------------
    # DETERMINE DIRECTION
    # --------------------------------------------------------

    if score_buy >= MIN_SCORE and score_buy > score_sell:

        direction = "BUY"
        score = score_buy

    elif score_sell >= MIN_SCORE and score_sell > score_buy:

        direction = "SELL"
        score = score_sell

    else:

        return {
            "direction": "NONE",
            "score": max(score_buy, score_sell),
            "m15": m15_direction,
            "m5": m5_direction,
            "bos": m5_bos,
            "sweep": m1_sweep,
            "rejection": m1_rejection,
            "engulfing": m1_engulfing,
            "displacement": m1_displacement,
            "atr": atr
        }

    # --------------------------------------------------------
    # ENTRY / SL
    # --------------------------------------------------------

    current = m1[-1]

    entry = current["close"]

    if direction == "BUY":

        structure_low = min(
            x["low"] for x in m1[-8:]
        )

        sl = structure_low - atr * 0.25

        risk = entry - sl

        if risk <= 0:
            return None

        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.2

    else:

        structure_high = max(
            x["high"] for x in m1[-8:]
        )

        sl = structure_high + atr * 0.25

        risk = sl - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.2

    return {
        "direction": direction,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "atr": atr,
        "m15": m15_direction,
        "m5": m5_direction,
        "bos": m5_bos,
        "sweep": m1_sweep,
        "rejection": m1_rejection,
        "engulfing": m1_engulfing,
        "displacement": m1_displacement
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(s):

    emoji = "🟢" if s["direction"] == "BUY" else "🔴"

    risk = abs(s["entry"] - s["sl"])

    reward = abs(s["tp2"] - s["entry"])

    rr = reward / risk if risk > 0 else 0

    return f"""
{emoji} XAUUSD {s["direction"]} SIGNAL

ENTRY: {s["entry"]:.2f}
SL:    {s["sl"]:.2f}
TP1:   {s["tp1"]:.2f}
TP2:   {s["tp2"]:.2f}

RR: 1:{rr:.2f}

SETUP SCORE: {s["score"]}/100

M15 STRUCTURE: {s["m15"]}
M5 STRUCTURE:  {s["m5"]}
M5 BOS:        {s["bos"]}

M1 LIQUIDITY:  {s["sweep"]}
M1 REJECTION:  {s["rejection"]}
M1 ENGULFING:  {s["engulfing"]}
M1 DISPLACE:   {s["displacement"]}

ATR: {s["atr"]:.2f}

LOT: {LOT_SIZE}

⚠️ SIGNAL ONLY — MANUAL EXECUTION
"""


# ============================================================
# NO TRADE MESSAGE
# ============================================================

def format_no_trade(s):

    return f"""
⚪ XAUUSD — NO TRADE

Setup not strong enough.

Score: {s.get("score", 0)}/100

M15: {s.get("m15", "N/A")}
M5:  {s.get("m5", "N/A")}
BOS: {s.get("bos", "N/A")}

M1 Sweep: {s.get("sweep", "N/A")}
M1 Rejection: {s.get("rejection", "N/A")}
M1 Engulfing: {s.get("engulfing", "N/A")}

Waiting for better candle structure.
"""


# ============================================================
# SIGNAL HASH
# ============================================================

def signal_hash(signal):

    raw = (
        f'{signal["direction"]}-'
        f'{signal["entry"]:.2f}-'
        f'{signal["sl"]:.2f}-'
        f'{signal["tp1"]:.2f}'
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()


# ============================================================
# SCAN
# ============================================================

def scan(send_no_trade=False):

    global LAST_SIGNAL_HASH
    global LAST_SIGNAL_TIME

    logging.info("Starting XAUUSD scan...")

    m15 = get_candles("15min", 150)
    m5 = get_candles("5min", 150)
    m1 = get_candles("1min", 150)

    if not m15 or not m5 or not m1:

        logging.error("Missing market data.")

        if send_no_trade:
            send_telegram(
                "⚠️ XAUUSD\n\n"
                "Unable to retrieve complete market data."
            )

        return

    result = analyze_market(m15, m5, m1)

    if not result:
        return

    if result["direction"] == "NONE":

        logging.info(
            "No setup. Score=%s",
            result["score"]
        )

        if send_no_trade:
            send_telegram(
                format_no_trade(result)
            )

        return

    h = signal_hash(result)

    # Don't repeat same setup
    if h == LAST_SIGNAL_HASH:

        logging.info(
            "Duplicate setup ignored."
        )

        return

    LAST_SIGNAL_HASH = h
    LAST_SIGNAL_TIME = datetime.now(BEIRUT_TZ)

    message = format_signal(result)

    logging.info(
        "SIGNAL: %s",
        result["direction"]
    )

    send_telegram(message)


# ============================================================
# SESSION FILTER
# ============================================================

def trading_session():

    now = datetime.now(BEIRUT_TZ)

    hour = now.hour + now.minute / 60

    session_1 = (
        SESSION_1_START <= hour < SESSION_1_END
    )

    session_2 = (
        SESSION_2_START <= hour < SESSION_2_END
    )

    return session_1 or session_2


# ============================================================
# BACKGROUND SCANNER
# ============================================================

def scanner_loop():

    logging.info(
        "Gold signal scanner started."
    )

    while True:

        try:

            if trading_session():

                scan()

            else:

                logging.info(
                    "Outside configured trading session."
                )

        except Exception as e:

            logging.exception(
                "Scanner error: %s",
                e
            )

        time.sleep(SCAN_SECONDS)


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return "XAUUSD Signal Bot is running.", 200


@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        message = data.get("message", {})

        text = message.get(
            "text",
            ""
        ).strip()

        chat_id = message.get(
            "chat",
            {}
        ).get(
            "id"
        )

        if not text:
            return "OK", 200

        if text.startswith("/start"):

            send_telegram(
                "🟡 XAUUSD GOLD SCALPING BOT\n\n"
                "Commands:\n"
                "/signal — scan now\n"
                "/status — bot status"
            )

        elif text.startswith("/signal"):

            send_telegram(
                "🔎 Scanning XAUUSD...\n"
                "M15 → M5 → M1"
            )

            scan(
                send_no_trade=True
            )

        elif text.startswith("/status"):

            now = datetime.now(
                BEIRUT_TZ
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            send_telegram(
                "🟢 BOT ONLINE\n\n"
                f"Time: {now}\n"
                f"Symbol: {SYMBOL}\n"
                f"Scan: every {SCAN_SECONDS}s\n"
                f"Minimum score: {MIN_SCORE}/100\n"
                f"Lot: {LOT_SIZE}"
            )

        return "OK", 200

    except Exception as e:

        logging.exception(
            "Webhook error: %s",
            e
        )

        return "OK", 200


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    # Start scanner in background
    thread = threading.Thread(
        target=scanner_loop,
        daemon=True
    )

    thread.start()

    port = int(
        os.getenv(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
