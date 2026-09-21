import os
import time
import threading
import logging
import hashlib
import requests

from flask import Flask, request


# ============================================================
# CONFIG
# ============================================================

SYMBOL = "XAU/USD"

# Continuous scanning
SCAN_SECONDS = 60

# Signal threshold
MIN_SCORE = 78

# Developing setup threshold
WATCH_SCORE = 55

# Small account protection
LOT_SIZE = "0.01 ONLY"

# Duplicate signal protection
LAST_SIGNAL_HASH = None
LAST_SIGNAL_TIME = None

# Latest candles
LAST_M1_CANDLE = None
LAST_M5_CANDLE = None
LAST_M15_CANDLE = None


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# ENVIRONMENT
# ============================================================

def get_env():
    return (
        os.getenv("TELEGRAM_TOKEN"),
        os.getenv("TELEGRAM_CHAT_ID"),
        os.getenv("TWELVE_DATA_KEY")
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message, chat_id=None):

    telegram_token, default_chat_id, _ = get_env()

    if not telegram_token:
        logging.error("TELEGRAM_TOKEN is missing.")
        return False

    target_chat_id = (
        chat_id
        if chat_id is not None
        else default_chat_id
    )

    if not target_chat_id:
        logging.error("Telegram chat ID missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{telegram_token}/sendMessage"
    )

    payload = {
        "chat_id": target_chat_id,
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
                "Telegram error %s: %s",
                response.status_code,
                response.text
            )

            return False

        logging.info("Telegram message sent.")

        return True

    except Exception as e:

        logging.exception(
            "Telegram exception: %s",
            e
        )

        return False


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(interval, outputsize=160):

    _, _, twelve_data_key = get_env()

    if not twelve_data_key:

        logging.error(
            "TWELVE_DATA_KEY is missing."
        )

        return []

    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": twelve_data_key,
        "timezone": "UTC",
        "order": "desc"
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        if data.get("status") == "error":

            logging.error(
                "Twelve Data %s error: %s",
                interval,
                data.get("message")
            )

            return []

        values = data.get("values", [])

        if not isinstance(values, list):

            logging.error(
                "Invalid Twelve Data values: %s",
                interval
            )

            return []

        candles = []

        for item in values:

            try:

                candle = {
                    "datetime": str(item["datetime"]),
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"])
                }

                if candle["high"] < candle["low"]:
                    continue

                candles.append(candle)

            except (
                KeyError,
                TypeError,
                ValueError
            ):
                continue

        if len(candles) < 30:

            logging.error(
                "%s: only %d valid candles",
                interval,
                len(candles)
            )

            return []

        # Twelve Data normally returns newest first.
        # First candle may still be forming.
        # Remove it and use closed candles only.

        if len(candles) > 1:
            candles = candles[1:]

        candles.reverse()

        logging.info(
            "%s | %d closed candles | latest=%s | "
            "O=%.2f H=%.2f L=%.2f C=%.2f",
            interval,
            len(candles),
            candles[-1]["datetime"],
            candles[-1]["open"],
            candles[-1]["high"],
            candles[-1]["low"],
            candles[-1]["close"]
        )

        return candles

    except requests.RequestException as e:

        logging.error(
            "Twelve Data network error %s: %s",
            interval,
            e
        )

        return []

    except Exception as e:

        logging.exception(
            "Twelve Data exception %s: %s",
            interval,
            e
        )

        return []


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def candle_body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(
        c["high"] - c["low"],
        0.00001
    )


def upper_wick(c):
    return (
        c["high"]
        - max(c["open"], c["close"])
    )


def lower_wick(c):
    return (
        min(c["open"], c["close"])
        - c["low"]
    )


def is_bullish(c):
    return c["close"] > c["open"]


def is_bearish(c):
    return c["close"] < c["open"]


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
            abs(
                current["high"]
                - previous["close"]
            ),
            abs(
                current["low"]
                - previous["close"]
            )
        )

        trs.append(tr)

    return sum(trs[-period:]) / period


# ============================================================
# STRUCTURE
# ============================================================

def structure_direction(candles, lookback=20):

    if len(candles) < lookback:
        return "NEUTRAL"

    recent = candles[-lookback:]

    first = recent[:10]
    second = recent[10:]

    first_high = max(
        c["high"] for c in first
    )

    second_high = max(
        c["high"] for c in second
    )

    first_low = min(
        c["low"] for c in first
    )

    second_low = min(
        c["low"] for c in second
    )

    if (
        second_high > first_high
        and second_low > first_low
    ):
        return "BULLISH"

    if (
        second_high < first_high
        and second_low < first_low
    ):
        return "BEARISH"

    return "RANGE"


# ============================================================
# BOS
# ============================================================

def detect_bos(candles):

    if len(candles) < 12:
        return "NONE"

    current = candles[-1]

    reference = candles[-8:-1
