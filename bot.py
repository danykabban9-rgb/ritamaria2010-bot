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

SCAN_SECONDS = 60

# Active scalper thresholds
WATCH_SCORE = 50
ENTRY_SCORE = 60

LOT_SIZE = "0.01 ONLY"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

LAST_M1 = None
LAST_SIGNAL_ID = None
LAST_WATCH_ID = None

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message, chat_id=None):

    token = TELEGRAM_TOKEN
    target = chat_id or TELEGRAM_CHAT_ID

    if not token or not target:
        logging.error("Telegram configuration missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    try:

        r = requests.post(
            url,
            json={
                "chat_id": target,
                "text": message
            },
            timeout=15
        )

        if r.status_code != 200:

            logging.error(
                "Telegram error: %s",
                r.text
            )

            return False

        return True

    except Exception as e:

        logging.error(
            "Telegram exception: %s",
            e
        )

        return False


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(interval, outputsize=120):

    if not TWELVE_DATA_KEY:

        logging.error(
            "TWELVE_DATA_KEY missing."
        )

        return []

    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "timezone": "UTC",
        "order": "desc"
    }

    try:

        r = requests.get(
            url,
            params=params,
            timeout=20
        )

        r.raise_for_status()

        data = r.json()

        if data.get("status") == "error":

            logging.error(
                "Twelve Data %s: %s",
                interval,
                data.get("message")
            )

            return []

        values = data.get("values", [])

        if not isinstance(values, list):
            return []

        candles = []

        for x in values:

            try:

                candles.append({
                    "datetime": str(x["datetime"]),
                    "open": float(x["open"]),
                    "high": float(x["high"]),
                    "low": float(x["low"]),
                    "close": float(x["close"])
                })

            except (
                KeyError,
                TypeError,
                ValueError
            ):

                continue

        if len(candles) < 30:

            logging.error(
                "%s incomplete: %d candles",
                interval,
                len(candles)
            )

            return []

        # newest candle can be forming
        candles = candles[1:]

        # oldest -> newest
        candles.reverse()

        logging.info(
            "%s | %d closed | latest=%s | "
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

    except Exception as e:

        logging.error(
            "Data error %s: %s",
            interval,
            e
        )

        return []


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def body(c):
    return abs(c["close"] - c["open"])


def rng(c):
    return max(
        c["high"] - c["low"],
        0.00001
    )


def upper(c):
    return (
        c["high"]
        - max(c["open"], c["close"])
    )


def lower(c):
    return (
        min(c["open"], c["close"])
        - c["low"]
    )


def bull(c):
    return c["close"] > c["open"]


def bear(c):
    return c["close"] < c["open"]


# ============================================================
# ATR
# ============================================================

def atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        c = candles[i]
        p = candles[i - 1]

        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"])
        )

        trs.append(tr)

    return sum(
        trs[-period:]
    ) / period


# ============================================================
# STRUCTURE
# ============================================================

def structure(candles, lookback=20):

    if len(candles) < lookback:
        return "RANGE"

    x = candles[-lookback:]

    half = lookback // 2

    a = x[:half]
    b = x[half:]

    ah = max(c["high"] for c in a)
    al = min(c["low"] for c in a)

    bh = max(c["high"] for c in b)
    bl = min(c["low"] for c in b)

    if bh > ah and bl > al:
        return "BULLISH"

    if bh < ah and bl < al:
        return "BEARISH"

    return "RANGE"


# ============================================================
# LOCAL SWINGS
# ============================================================

def recent_high(candles, n=6):

    return max(
        c["high"]
        for c in candles[-n:]
    )


def recent_low(candles, n=6):

    return min(
        c["low"]
        for c in candles[-n:]
    )


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def sweep(candles):

    if len(candles) < 10:
        return "NONE"

    c = candles[-1]

    reference = candles[-8:-1]

    high = max(
        x["high"]
        for x in reference
    )

    low = min(
        x["low"]
        for x in reference
    )

    # SELL-side liquidity taken,
    # then price closes back above it
    if (
        c["low"] < low
        and c["close"] > low
    ):
        return "BULLISH"

    # BUY-side liquidity taken,
    # then price closes back below it
    if (
        c["high"] > high
        and c["close"] < high
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# REJECTION
# ============================================================

def rejection(c):

    r = rng(c)
    b = body(c)

    if (
        lower(c) >= max(b * 1.4, r * 0.30)
        and c["close"] > c["low"] + r * 0.55
    ):
        return "BULLISH"

    if (
        upper(c) >= max(b * 1.4, r * 0.30)
        and c["close"] < c["high"] - r * 0.55
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# ENGULFING
# ============================================================

def engulfing(candles):

    if len(candles) < 2:
        return "NONE"

    p = candles[-2]
    c = candles[-1]

    if (
        bear(p)
        and bull(c)
        and c["open"] <= p["close"]
        and c["close"] >= p["open"]
    ):
        return "BULLISH"

    if (
        bull(p)
        and bear(c)
        and c["open"] >= p["close"]
        and c["close"] <= p["open"]
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# DISPLACEMENT
# ============================================================

def displacement(candles):

    if len(candles) < 12:
        return "NONE"

    c = candles[-1]

    avg = sum(
        rng(x)
        for x in candles[-11:-1]
    ) / 10

    cr = rng(c)

    if cr < avg * 1.20:
        return "NONE"

    if (
        bull(c)
        and body(c) >= cr * 0.60
    ):
        return "BULLISH"

    if (
        bear(c)
        and body(c) >= cr * 0.60
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# MICRO BOS
# ============================================================

def micro_bos(candles):

    if len(candles) < 8:
        return "NONE"

    c = candles[-1]

    previous = candles[-5:-1]

    h = max(
        x["high"]
        for x in previous
    )

    l = min(
        x["low"]
        for x in previous
    )

    if c["close"] > h:
        return "BULLISH"

    if c["close"] < l:
        return "BEARISH"

    return "NONE"


# ============================================================
# PULLBACK
# ============================================================

def pullback_signal(candles, direction):

    if len(candles) < 5:
        return False

    last = candles[-1]
    previous = candles[-4:-1]

    if direction == "BUY":

        had_bearish = any(
            bear(x)
            for x in previous
        )

        return (
            had_bearish
            and bull(last)
            and last["close"] > previous[-1]["close"]
        )

    if direction == "SELL":

        had_bullish = any(
            bull(x)
            for x in previous
        )

        return (
            had_bullish
            and bear(last)
            and last["close"] < previous[-1]["close"]
        )

    return False


# ============================================================
# BREAKOUT
# ============================================================

def breakout(candles):

    if len(candles) < 10:
        return "NONE"

    c = candles[-1]

    reference = candles[-8:-1]

    high = max(
        x["high"]
        for x in reference
    )

    low = min(
        x["low"]
        for x in reference
    )

    if (
        c["close"] > high
        and body(c) >= rng(c) * 0.55
    ):
        return "BULLISH"

    if (
        c["close"] < low
        and body(c) >= rng(c) * 0.55
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# SCORE ENGINE
# ============================================================

def analyze(m1, m5, m15):

    if (
        len(m1) < 30
        or len(m5) < 30
        or len(m15) < 30
    ):
        return None

    m5_structure = structure(m5)
    m15_structure = structure(m15)

    sw = sweep(m1)
    rej = rejection(m1[-1])
    eng = engulfing(m1)
    disp = displacement(m1)
    bos = micro_bos(m1)
    brk = breakout(m1)

    buy = 0
    sell = 0

    buy_reasons = []
    sell_reasons = []

    # --------------------------------------------------------
    # M5 CONTEXT
    # --------------------------------------------------------

    if m5_structure == "BULLISH":

        buy += 12
        buy_reasons.append("M5 bullish")

    elif m5_structure == "BEARISH":

        sell += 12
        sell_reasons.append("M5 bearish")

    # --------------------------------------------------------
    # M15 BACKGROUND ONLY
