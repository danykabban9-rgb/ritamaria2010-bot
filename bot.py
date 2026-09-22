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
        current =
