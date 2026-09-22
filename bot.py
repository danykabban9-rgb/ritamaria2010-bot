import os
import time
import threading
import logging
import hashlib
import requests

from flask import Flask, jsonify


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

TWELVE_URL = "https://api.twelvedata.com/time_series"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# GLOBAL STATE
# ============================================================

LAST_AUTO_SETUP_ID = None
LAST_MANUAL_SETUP_ID = None
LAST_UPDATE_ID = 0

START_TIME = time.time()
LAST_HEARTBEAT = 0

# Prevent overlapping market analysis
ANALYSIS_LOCK = threading.Lock()

# Prevent simultaneous Twelve Data requests
TWELVE_REQUEST_LOCK = threading.Lock()


# ============================================================
# MARKET DATA CACHE
# ============================================================

DATA_CACHE = {
    "1min": {
        "candles": [],
        "time": 0
    },
    "5min": {
        "candles": [],
        "time": 0
    },
    "15min": {
        "candles": [],
        "time": 0
    }
}

CACHE_LOCK = threading.Lock()

CACHE_TTL = {
    "1min": 55,
    "5min": 240,
    "15min": 840
}


# ============================================================
# TWELVE DATA BACKOFF
# ============================================================

TWELVE_BACKOFF_UNTIL = 0
TWELVE_BACKOFF_SECONDS = 90


# ============================================================
# BASIC CONFIG
# ============================================================

def config_ok():

    return bool(
        TELEGRAM_TOKEN
        and TELEGRAM_CHAT_ID
        and TWELVE_DATA_KEY
    )


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_api(method, data=None, timeout=10):

    if not TELEGRAM_TOKEN:
        return None

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/{method}"
    )

    try:

        response = requests.post(
            url,
            data=data or {},
            timeout=timeout
        )

        if not response.ok:

            logging.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response
