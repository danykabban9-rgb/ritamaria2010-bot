import os
import time
import threading
import logging
import requests
import hashlib

from flask import Flask, request
from datetime import datetime
import pytz


# ============================================================
# RENDER VARIABLES
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


# ============================================================
# GLOBAL STATE
# ============================================================

LAST_AUTO_SETUP_ID = None
LAST_AUTO_SIGNAL = None
LAST_AUTO_ALERT_TIME = 0

LAST_MANUAL_SETUP_ID = None


# ============================================================
# SETTINGS
# ============================================================

MIN_SCORE = 70
STRONG_SCORE = 80

# Don't send another automatic signal immediately
ALERT_COOLDOWN = 300

# Only alert during these Beirut hours
TRADING_WINDOWS = [
    (9, 12),
    (14, 19)
]


# ============================================================
# TELEGRAM
# ============================================================

def send_message(chat_id, text):

    try:

        if not TELEGRAM_TOKEN:
            logging.error("TELEGRAM_TOKEN missing")
            return False

        if not chat_id:
            logging.error("Telegram chat ID missing")
            return False

        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/sendMessage"
        )

        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=15
        )

        if response.status_code != 200:
            logging.error(
                f"Telegram error: {response.text}"
            )
            return False

        return True

    except Exception as e:

        logging.error(
            f"Telegram send error: {e}"
        )

        return False


# ============================================================
# TRADING WINDOW
# ============================================================

def is_trading_window():

    now = datetime.now(BEIRUT_TZ)
    hour = now.hour

    for start, end in TRADING_WINDOWS:

        if start <= hour < end:
            return True

    return False


# ============================================================
# XAU DATA
# ============================================================

def get_xau_data(interval, outputsize=150):

    try:

        if not TWELVE_DATA_KEY:
            logging.error("TWELVE_DATA_KEY missing")
            return None

        url = (
            "https://api.twelvedata.com/time_series"
            f"?symbol=XAU/USD"
            f"&interval={interval}"
            f"&apikey={TWELVE_DATA_KEY}"
            f"&outputsize={outputsize}"
            f"&order=ASC"
        )

        response = requests.get(
            url,
            timeout=15
        )

        data = response.json()

        if "values" not in data:

            logging.error(
                f"Twelve Data error: {data}"
            )

            return None

        candles = []

        for x in data["values"]:

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

        if len(candles) < 30:
            return None

        return candles

    except Exception as e:

        logging.error(
            f"Market data error: {e}"
        )

        return None


# ============================================================
# CANDLE ANALYSIS
# ============================================================

def candle_parts(c):

    body = abs(
        c["close"] - c["open"]
    )

    upper = (
        c["high"]
        - max(c["open"], c["close"])
    )

    lower = (
        min(c["open"], c["close"])
        - c["low"]
    )

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
        lower > body * 1.2
        and lower > upper
        and c["close"] > c["low"] + total * 0.55
    )


def bearish_rejection(c):

    body, upper, lower, total = candle_parts(c)

    if total <= 0:
        return False

    return (
        upper > body * 1.2
        and upper > lower
        and c["close"] < c["high"] - total * 0.55
    )


def bullish_engulfing(prev, current):

    return (
        bearish(prev)
        and bullish(current)
        and current["close"] >= prev["open"]
        and current["open"] <= prev["close"]
        and abs(current["close"] - current["open"])
        >= abs(prev["close"] - prev["open"]) * 0.9
    )


def bearish_engulfing(prev, current):

    return (
        bullish(prev)
        and bearish(current)
        and current["close"] <= prev["open"]
        and current["open"] >= prev["close"]
        and abs(current["close"] - current["open"])
        >= abs(prev["close"] - prev["open"]) * 0.9
    )


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges.append(tr)

    return (
        sum(true_ranges[-period:])
        / period
    )


# ============================================================
# SWING STRUCTURE
# ============================================================

def get_swings(candles, lookback=20):

    if len(candles) < lookback + 2:
        return None, None

    section = candles[-lookback:]

    high = max(
        c["high"] for c in section
    )

    low = min(
        c["low"] for c in section
    )

    return high, low


def market_structure(candles):

    if len(candles) < 30:
        return "NEUTRAL"

    recent = candles[-10:]
    previous = candles[-20:-10]

    recent_high = max(
        c["high"] for c in recent
    )

    previous_high = max(
        c["high"] for c in previous
    )

    recent_low = min(
        c["low"] for c in recent
    )

    previous_low = min(
        c["low"] for c in previous
    )

    if (
        recent_high > previous_high
        and recent_low > previous_low
    ):
        return "BULLISH"

    if (
        recent_high < previous_high
        and recent_low < previous_low
    ):
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def bullish_sweep_recent(candles, lookback=12):

    if len(candles) < lookback + 2:
        return False, None

    # Look at the last 3 closed candles.
    # A sweep doesn't have to happen on the latest candle.
    for i in range(
        len(candles) - 3,
        len(candles)
    ):

        current = candles[i]

        previous = candles[
            max(0, i - lookback):i
        ]

        if not previous:
            continue

        old_low = min(
            c["low"] for c in previous
        )

        if (
            current["low"] < old_low
            and current["close"] > old_low
        ):
            return True, current

    return False, None


def bearish_sweep_recent(candles, lookback=12):

    if len(candles) < lookback + 2:
        return False, None

    for i in range(
        len(candles) - 3,
        len(candles)
    ):

        current = candles[i]

        previous = candles[
            max(0, i - lookback):i
        ]

        if not previous:
            continue

        old_high = max(
            c["high"] for c in previous
        )

        if (
            current["high"] > old_high
            and current["close"] < old_high
        ):
            return True, current

    return False, None


# ============================================================
# RECENT BOS
# ============================================================

def bullish_bos_recent(candles, lookback=10):

    if len(candles) < lookback + 2:
        return False

    for i in range(
        len(candles) - 3,
        len(candles)
    ):

        current = candles[i]

        previous = candles[
            max(0, i - lookback):i
        ]

        if not previous:
            continue

        old_high = max(
            c["high"] for c in previous
        )

        if current["close"] > old_high:
            return True

    return False


def bearish_bos_recent(candles, lookback=10):

    if len(candles) < lookback + 2:
        return False

    for i in range(
        len(candles) - 3,
        len(candles)
    ):

        current = candles[i]

        previous = candles[
            max(0, i - lookback):i
        ]

        if not previous:
            continue

        old_low = min(
            c["low"] for c in previous
        )

        if current["close"] < old_low:
            return True

    return False


# ============================================================
# DISPLACEMENT
# ============================================================

def bullish_displacement_recent(candles):

    if len(candles) < 7:
        return False

    for current in candles[-3:]:

        body, upper, lower, total = candle_parts(
            current
        )

        if total <= 0:
            continue

        previous_bodies = []

        for c in candles[-8:-3
