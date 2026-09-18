import os
import time
import threading
import logging
import requests
import hashlib
from datetime import datetime

import pytz
from flask import Flask, request

# ============================================================
# CONFIG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

BEIRUT = pytz.timezone("Asia/Beirut")

# Scanner
SCAN_SECONDS = 60

# Prevent repeated alerts
last_alert_setup = None
last_alert_time = 0

# Minimum score for a trade
MIN_SCORE = 80


# ============================================================
# TELEGRAM
# ============================================================

def send_message(text):
    try:
        url = f"{TELEGRAM_URL}/sendMessage"

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text
        }

        r = requests.post(url, json=payload, timeout=15)

        if r.status_code != 200:
            logging.error("Telegram error: %s", r.text)
            return False

        return True

    except Exception as e:
        logging.error("Telegram send error: %s", e)
        return False


# ============================================================
# TRADING HOURS
# ============================================================

def is_trading_window():
    now = datetime.now(BEIRUT)
    hour = now.hour

    # London / New York active periods
    return (
        9 <= hour < 12
        or
        14 <= hour < 19
    )


# ============================================================
# TWELVE DATA
# ============================================================

def get_xau_data(interval, outputsize=150):

    try:

        url = "https://api.twelvedata.com/time_series"

        params = {
            "symbol": "XAU/USD",
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVE_DATA_KEY,
            "order": "ASC"
        }

        r = requests.get(
            url,
            params=params,
            timeout=20
        )

        data = r.json()

        if "values" not in data:

            logging.error(
                "Twelve Data error %s: %s",
                interval,
                data
            )

            return []

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

        return candles

    except Exception as e:

        logging.error(
            "Data error %s: %s",
            interval,
            e
        )

        return []


# ============================================================
# REMOVE CURRENT FORMING CANDLE
# ============================================================

def closed_candles(candles):

    if len(candles) < 3:
        return []

    # Twelve Data normally returns the latest candle last.
    # Remove it because it may still be forming.
    return candles[:-1]


# ============================================================
# BASIC CANDLE FUNCTIONS
# ============================================================

def candle_body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(c["high"] - c["low"], 0.00001)


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


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
# STRUCTURE
# ============================================================

def market_structure(candles, lookback=20):

    if len(candles) < lookback:
        return "NEUTRAL"

    recent = candles[-lookback:]

    highs = [x["high"] for x in recent]
    lows = [x["low"] for x in recent]

    current = recent[-1]["close"]

    highest = max(highs[:-3])
    lowest = min(lows[:-3])

    recent_high = max(highs[-5:])
    recent_low = min(lows[-5:])

    if current > highest and recent_low > lowest:
        return "BULLISH"

    if current < lowest and recent_high < highest:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# BOS
# ============================================================

def detect_bos(candles):

    if len(candles) < 10:
        return None

    previous = candles[-6:-1]
    current = candles[-1]

    previous_high = max(x["high"] for x in previous)
    previous_low = min(x["low"] for x in previous)

    if current["close"] > previous_high:
        return "BULLISH BOS"

    if current["close"] < previous_low:
        return "BEARISH BOS"

    return None


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(candles):

    if len(candles) < 10:
        return None

    current = candles[-1]
    previous = candles[-8:-1]

    old_high = max(x["high"] for x in previous)
    old_low = min(x["low"] for x in previous)

    # Sweep sell-side liquidity then recover
    if (
        current["low"] < old_low
        and current["close"] > old_low
    ):
        return "BULLISH LIQUIDITY SWEEP"

    # Sweep buy-side liquidity then reject
    if (
        current["high"] > old_high
        and current["close"] < old_high
    ):
        return "BEARISH LIQUIDITY SWEEP"

    return None


# ============================================================
# DISPLACEMENT
# ============================================================

def detect_displacement(candles):

    if len(candles) < 10:
        return None

    current = candles[-1]

    atr = calculate_atr(candles, 14)

    if atr is None:
        return None

    body = candle_body(current)

    if body < atr * 0.8:
        return None

    if bullish(current):
        return "BULLISH DISPLACEMENT"

    if bearish(current):
        return "BEARISH DISPLACEMENT"

    return None


# ============================================================
# CANDLE PATTERNS
# ============================================================

def bullish_pin_bar(c):

    body = candle_body(c)
    rng = candle_range(c)

    lw = lower_wick(c)
    uw = upper_wick(c)

    if (
        lw >= body * 2
        and
        lw >= uw * 1.5
        and
        c["close"] >= c["low"] + rng * 0.60
    ):
        return True

    return False


def bearish_pin_bar(c):

    body = candle_body(c)
    rng = candle_range(c)

    lw = lower_wick(c)
    uw = upper_wick(c)

    if (
        uw >= body * 2
        and
        uw >= lw * 1.5
        and
        c["close"] <= c["low"] + rng * 0.40
    ):
        return True

    return False


def hammer(c):

    body = candle_body(c)
    rng = candle_range(c)

    lw = lower_wick(c)
    uw = upper_wick(c)

    if (
        lw >= body * 2
        and
        uw <= max(body * 0.8, rng * 0.08)
        and
        c["close"] > c["open"]
        and
        c["close"] >= c["low"] + rng * 0.60
    ):
        return True

    return False


def shooting_star(c):

    body = candle_body(c)
    rng = candle_range(c)

    lw = lower_wick(c)
    uw = upper_wick(c)

    if (
        uw >= body * 2
        and
        lw <= max(body * 0.8, rng * 0.08)
        and
        c["close"] < c["open"]
        and
        c["close"] <= c["low"] + rng * 0.40
    ):
        return True

    return False


# ============================================================
# ENGULFING
# ============================================================

def bullish_engulfing(candles):

    if len(candles) < 2:
        return False

    prev = candles[-2]
    cur = candles[-1]

    if not bearish(prev):
        return False

    if not bullish(cur):
        return False

    return (
        cur["open"] <= prev["close"]
        and
        cur["close"] >= prev["open"]
        and
        candle_body(cur) >= candle_body(prev) * 0.9
    )


def bearish_engulfing(candles):

    if len(candles) < 2:
        return False

    prev = candles[-2]
    cur = candles[-1]

    if not bullish(prev):
        return False

    if not bearish(cur):
        return False

    return (
        cur["open"] >= prev["close"]
        and
        cur["close"] <= prev["open"]
        and
        candle_body(cur) >= candle_body(prev) * 0.9
    )


# ============================================================
# MORNING STAR
# ============================================================

def morning_star(candles):

    if len(candles) < 3:
        return False

    a = candles[-3]
    b = candles[-2]
    c = candles[-1]

    body_a = candle_body(a)
    body_b = candle_body(b)

    if not bearish(a):
        return False

    if body_a < candle_range(a) * 0.45:
        return False

    if body_b > body_a * 0.55:
        return False

    if not bullish(c):
        return False

    midpoint_a = (a["open"] + a["close"]) / 2

    return c["close"] > midpoint_a


def evening_star(candles):

    if len(candles) < 3:
        return False

    a = candles[-3]
    b = candles[-2]
    c = candles[-1]

    body_a = candle_body(a)
    body_b = candle_body(b)

    if not bullish(a):
        return False

    if body_a < candle_range(a) * 0.45:
        return False

    if body_b > body_a * 0.55:
        return False

    if not bearish(c):
        return False

    midpoint_a = (a["open"] + a["close"]) / 2

    return c["close"] < midpoint_a


# ============================================================
# TWEEZER
# ============================================================

def tweezer_bottom(candles):

    if len(candles) < 2:
        return False

    a = candles[-2]
    b = candles[-1]

    atr = calculate_atr(candles, 14)

    if atr is None:
        atr = candle_range(a)

    same_low = abs(a["low"] - b["low"]) <= atr * 0.15

    return (
        same_low
        and
        bearish(a)
        and
        bullish(b)
    )


def tweezer_top(candles):

    if len(candles) < 2:
        return False

    a = candles[-2]
    b = candles[-1]

    atr = calculate_atr(candles, 14)

    if atr is None:
        atr = candle_range(a)

    same_high = abs(a["high"] - b["high"]) <= atr * 0.15

    return (
        same_high
        and
        bullish(a)
        and
        bearish(b)
    )


# ============================================================
# INSIDE BAR
# ============================================================

def inside_bar(candles):

    if len(candles) < 2:
        return None

    mother = candles[-2]
    child = candles[-1]

    if (
        child["high"] < mother["high"]
        and
        child["low"] > mother["low"]
    ):
        return "INSIDE BAR"

    return None


# ============================================================
# CANDLE PATTERN ANALYZER
# ============================================================

def analyze_candle_patterns(candles):

    patterns = []
    bullish_points = 0
    bearish_points = 0

    if len(candles) < 3:
        return patterns, 0, 0

    current = candles[-1]

    # --------------------------------------------------------
    # BULLISH
    # --------------------------------------------------------

    if hammer(current):
        patterns.append("HAMMER")
        bullish_points += 3

    if bullish_pin_bar(current):
        patterns.append("BULLISH PIN BAR")
        bullish_points += 3

    if bullish_engulfing(candles):
        patterns.append("BULLISH ENGULFING")
        bullish_points += 5

    if morning_star(candles):
        patterns.append("MORNING STAR")
        bullish_points += 5

    if tweezer_bottom(candles):
        patterns.append("TWEEZER BOTTOM")
        bullish_points += 4

    # --------------------------------------------------------
    # BEARISH
    # --------------------------------------------------------

    if shooting_star(current):
        patterns.append("SHOOTING STAR")
        bearish_points += 3

    if bearish_pin_bar(current):
        patterns.append("BEARISH PIN BAR")
        bearish_points += 3

    if bearish_engulfing(candles):
        patterns.append("BEARISH ENGULFING")
        bearish_points += 5

    if evening_star(candles):
        patterns.append("EVENING STAR")
        bearish_points += 5

    if tweezer_top(candles):
        patterns.append("TWEEZER TOP")
        bearish_points += 4

    # --------------------------------------------------------
    # INSIDE BAR
    # --------------------------------------------------------

    ib = inside_bar(candles)

    if ib:
        patterns.append(ib)

        # Inside bar alone is neutral.
        # Direction comes from structure/BOS.

    return patterns, bullish_points, bearish_points


# ============================================================
# SCORE CANDLE PATTERNS
# ============================================================

def pattern_direction(patterns, bullish_points, bearish_points):

    if bullish_points > bearish_points:
        return "BUY"

    if bearish_points > bullish_points:
        return "SELL"

    return "NEUTRAL"


# ============================================================
# SETUP ID
# ============================================================

def create_setup_id(
    m5,
    signal,
    m5_patterns,
    m1_patterns,
    sweep,
    bos
):

    if not m5:
        return None

    last = m5[-1]

    raw = "|".join([
        str(last["datetime"]),
        str(round(last["open"], 2)),
        str(round(last["high"], 2)),
        str(round(last["low"], 2)),
        str(round(last["close"], 2)),
        str(signal),
        ",".join(m5_patterns),
        ",".join(m1_patterns),
        str(sweep),
        str(bos)
    ])

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()[:16]


# ============================================================
# MAIN ANALYSIS
# ============================================================

def analyze_pro():

    m1_raw = get_xau_data("1min", 200)
    m5_raw = get_xau_data("5min", 150)
    m15_raw = get_xau_data("15min", 100)

    if (
        len(m1_raw) < 30
        or
        len(m5_raw) < 30
        or
        len(m15_raw) < 30
    ):
        return {
            "valid": False,
            "reason": "Not enough market data"
        }

    # Only closed candles
    m1 = closed_candles(m1_raw)
    m5 = closed_candles(m5_raw)
    m15 = closed_candles(m15_raw)

    # --------------------------------------------------------
    # PRICE
    # --------------------------------------------------------

    price = m1[-1]["close"]

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    structure15 = market_structure(m15)
    structure5 = market_structure(m5)

    # --------------------------------------------------------
    # BOS
    # --------------------------------------------------------

    bos5 = detect_bos(m5)

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    sweep5 = detect_liquidity_sweep(m5)

    # --------------------------------------------------------
    # DISPLACEMENT
    # --------------------------------------------------------

    displacement5 = detect_displacement(m5)

    displacement1 = detect_displacement(m1)

    # --------------------------------------------------------
    # CANDLE PATTERNS
    # --------------------------------------------------------

    m5_patterns, m5_bull_points, m5_bear_points = \
        analyze_candle_patterns(m5)

    m1_patterns, m1_bull_points, m1_bear_points = \
        analyze_candle_patterns(m1)

    m5_pattern_direction = pattern_direction(
        m5_patterns,
        m5_bull_points,
        m5_bear_points
    )

    m1_pattern_direction = pattern_direction(
        m1_patterns,
        m1_bull_points,
        m1_bear_points
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    buy_score = 0
    sell_score = 0

    reasons_buy = []
    reasons_sell = []

    # ========================================================
    # M15 STRUCTURE - 15 POINTS
    # ========================================================

    if structure15 == "BULLISH":
        buy_score += 15
        reasons_buy.append("M15 bullish structure")

    elif structure15 == "BEARISH":
        sell_score += 15
        reasons_sell.append("M15 bearish structure")

    # ========================================================
    # M5 STRUCTURE - 15 POINTS
    # ========================================================

    if structure5 == "BULLISH":
        buy_score += 15
        reasons_buy.append("M5 bullish structure")

    elif structure5 == "BEARISH":
        sell_score += 15
        reasons_sell.append("M5 bearish structure")

    # ========================================================
    # LIQUIDITY SWEEP - 20 POINTS
    # ========================================================

    if sweep5 == "BULLISH LIQUIDITY SWEEP":
        buy_score += 20
        reasons_buy.append("M5 bullish liquidity sweep")

    elif sweep5 == "BEARISH LIQUIDITY SWEEP":
        sell_score += 20
        reasons_sell.append("M5 bearish liquidity sweep")

    # ========================================================
    # M5 CANDLE PATTERN - MAX 15
    # ========================================================

    if m5_bull_points > m5_bear_points:

        points = min(15, m5_bull_points)

        buy_score += points

        if m5_patterns:
            reasons_buy.append(
                "M5: " + ", ".join(m5_patterns)
            )

    elif m5_bear_points > m5_bull_points:

        points = min(15, m5_bear_points)

        sell_score += points

        if m5_patterns:
            reasons_sell.append(
                "M5: " + ", ".join(m5_patterns)
            )

    # ========================================================
    # M1 CANDLE PATTERN - MAX 15
    # ========================================================

    if m1_bull_points > m1_bear_points:

        points = min(15, m1_bull_points)

        buy_score += points

        if m1_patterns:
            reasons_buy.append(
                "M1: " + ", ".join(m1_patterns)
            )

    elif m1_bear_points > m1_bull_points:

        points = min(15, m1_bear_points)

        sell_score += points

        if m1_patterns:
            reasons_sell.append(
                "M1: " + ", ".join(m1_patterns)
            )

    # ========================================================
    # BOS - 10 POINTS
    # ========================================================

    if bos5 == "BULLISH BOS":

        buy_score += 10
        reasons_buy.append("M5 bullish BOS")

    elif bos5 == "BEARISH BOS":

        sell_score += 10
        reasons_sell.append("M5 bearish BOS")

    # ========================================================
    # DISPLACEMENT - 5 POINTS
    # ========================================================

    if displacement5 == "BULLISH DISPLACEMENT":

        buy_score += 5
        reasons_buy.append("M5 bullish displacement")

    elif displacement5 == "BEARISH DISPLACEMENT":

        sell_score += 5
        reasons_sell.append("M5 bearish displacement")

    # M1 displacement is used as confirmation,
    # but does not add another score to avoid double counting.

    # ========================================================
    # DETERMINE SIGNAL
    # ========================================================

    signal = "NO TRADE"
    score = max(buy_score, sell_score)
    reasons = []

    if buy_score >= MIN_SCORE and buy_score > sell_score:

        signal = "BUY"
        score = buy_score
        reasons = reasons_buy

    elif sell_score >= MIN_SCORE and sell_score > buy_score:

        signal = "SELL"
        score = sell_score
        reasons = reasons_sell

    # ========================================================
    # ATR / SL / TP
    # ========================================================

    atr = calculate_atr(m5, 14)

    if atr is None:

        return {
            "valid": False,
            "reason": "ATR unavailable"
        }

    # ========================================================
    # RISK MODEL
    # ========================================================

    if signal == "BUY":

        # Use recent M5 swing low
        swing_low = min(
            x["low"] for x in m5[-8:]
        )

        sl = swing_low - atr * 0.15

        risk = price - sl

        if risk <= 0:
            risk = atr * 1.2
            sl = price - risk

        tp1 = price + risk * 1.5
        tp2 = price + risk * 2.3

    elif signal == "SELL":

        swing_high = max(
            x["high"] for x in m5[-8:]
        )

        sl = swing_high + atr * 0.15

        risk = sl - price

        if risk <= 0:
            risk = atr * 1.2
            sl = price + risk

        tp1 = price - risk * 1.5
        tp2 = price - risk * 2.3

    else:

        sl = None
        tp1 = None
        tp2 = None
        risk = None

    # ========================================================
    # SETUP ID
    # ========================================================

    setup_id = create_setup_id(
        m5,
        signal,
        m5_patterns,
        m1_patterns,
        sweep5,
        bos5
    )

    return {
        "valid": True,

        "signal": signal,
        "score": score,

        "price": price,

        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,

        "risk": risk,

        "atr": atr,

        "structure15": structure15,
        "structure5": structure5,

        "bos": bos5,
        "sweep": sweep5,

        "displacement5": displacement5,
        "displacement1": displacement1,

        "m5_patterns": m5_patterns,
        "m1_patterns": m1_patterns,

        "m5_pattern_direction": m5_pattern_direction,
        "m1_pattern_direction": m1_pattern_direction,

        "reasons": reasons,

        "setup_id": setup_id,

        "time": m1[-1]["datetime"]
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(a, same_setup=False):

    if not a["valid"]:

        return (
            "⚠️ DATA ERROR\n\n"
            f"{a['reason']}"
        )

    signal = a["signal"]

    if signal == "BUY":
        emoji = "🟢"
    elif signal == "SELL":
        emoji = "🔴"
    else:
        emoji = "⚪"

    text
