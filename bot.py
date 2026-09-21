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

# Continuous scanning - NO TIME FILTER
SCAN_SECONDS = 60

# Strong setup threshold
MIN_SCORE = 78

# Small account
LOT_SIZE = "0.01 ONLY"

# Duplicate signal protection
LAST_SIGNAL_HASH = None
LAST_SIGNAL_TIME = None

# Remember latest market candles
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
        logging.error("No Telegram chat ID available.")
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

        logging.info("Telegram message sent successfully.")

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

        if not isinstance(data, dict):

            logging.error(
                "Invalid Twelve Data response: %s",
                interval
            )

            return []

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
                "Invalid values for %s",
                interval
            )

            return []

        candles = []

        for item in values:

            if not isinstance(item, dict):
                continue

            try:

                c = {
                    "datetime": str(item["datetime"]),
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"])
                }

                if c["high"] < c["low"]:
                    continue

                candles.append(c)

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

        # ----------------------------------------------------
        # Twelve Data normally returns newest first.
        #
        # We remove the first candle because it may still
        # be forming, then reverse into oldest -> newest.
        # ----------------------------------------------------

        closed = candles[1:]

        closed.reverse()

        logging.info(
            "%s | candles=%d | latest=%s | O=%.2f H=%.2f L=%.2f C=%.2f",
            interval,
            len(closed),
            closed[-1]["datetime"],
            closed[-1]["open"],
            closed[-1]["high"],
            closed[-1]["low"],
            closed[-1]["close"]
        )

        return closed

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
# CANDLE BASICS
# ============================================================

def candle_body(c):

    return abs(
        c["close"] - c["open"]
    )


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
# AVERAGE RANGE
# ============================================================

def average_range(candles, period=14):

    if len(candles) < period:
        return None

    recent = candles[-period:]

    return (
        sum(candle_range(c) for c in recent)
        / period
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_direction(candles):

    if len(candles) < 20:
        return "NEUTRAL"

    recent = candles[-20:]

    first = recent[:10]
    second = recent[10:]

    first_high = max(c["high"] for c in first)
    second_high = max(c["high"] for c in second)

    first_low = min(c["low"] for c in first)
    second_low = min(c["low"] for c in second)

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
# MORE DYNAMIC BOS
# ============================================================

def detect_bos(candles):

    if len(candles) < 15:
        return "NONE"

    current = candles[-1]

    reference = candles[-8:-1]

    previous_high = max(
        c["high"] for c in reference
    )

    previous_low = min(
        c["low"] for c in reference
    )

    # Require a real close beyond structure
    if current["close"] > previous_high:
        return "BULLISH"

    if current["close"] < previous_low:
        return "BEARISH"

    return "NONE"


# ============================================================
# RECENT LIQUIDITY SWEEP
# ============================================================

def liquidity_sweep(candles):

    if len(candles) < 15:
        return "NONE"

    # Check the last 3 closed candles.
    # This avoids depending only on candles[-1].

    for index in range(
        len(candles) - 3,
        len(candles)
    ):

        current = candles[index]

        start = max(0, index - 7)

        previous = candles[start:index]

        if len(previous) < 4:
            continue

        previous_high = max(
            c["high"] for c in previous
        )

        previous_low = min(
            c["low"] for c in previous
        )

        # Bullish sweep
        if (
            current["low"] < previous_low
            and current["close"] > previous_low
        ):

            return "BULLISH"

        # Bearish sweep
        if (
            current["high"] > previous_high
            and current["close"] < previous_high
        ):

            return "BEARISH"

    return "NONE"


# ============================================================
# REJECTION
# ============================================================

def rejection_signal(c):

    if not isinstance(c, dict):
        return "NONE"

    rng = candle_range(c)

    body = candle_body(c)

    if body <= rng * 0.05:
        return "NONE"

    lw = lower_wick(c)
    uw = upper_wick(c)

    if (
        lw >= body * 1.5
        and lw >= uw * 1.5
        and is_bullish(c)
    ):
        return "BULLISH"

    if (
        uw >= body * 1.5
        and uw >= lw * 1.5
        and is_bearish(c)
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# RECENT REJECTION
# ============================================================

def recent_rejection(candles):

    for c in candles[-3:]:

        result = rejection_signal(c)

        if result != "NONE":
            return result

    return "NONE"


# ============================================================
# ENGULFING
# ============================================================

def engulfing_signal(candles):

    if len(candles) < 2:
        return "NONE"

    previous = candles[-2]
    current = candles[-1]

    if (
        is_bearish(previous)
        and is_bullish(current)
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    ):

        return "BULLISH"

    if (
        is_bullish(previous)
        and is_bearish(current)
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    ):

        return "BEARISH"

    return "NONE"


# ============================================================
# CHECK RECENT ENGULFING
# ============================================================

def recent_engulfing(candles):

    if len(candles) < 4:
        return "NONE"

    for i in range(
        len(candles) - 3,
        len(candles)
    ):

        pair = candles[:i + 1]

        result = engulfing_signal(pair)

        if result != "NONE":
            return result

    return "NONE"


# ============================================================
# DISPLACEMENT
# ============================================================

def displacement(candles):

    if len(candles) < 16:
        return "NONE"

    current = candles[-1]

    avg = average_range(
        candles[:-1],
        14
    )

    if not avg:
        return "NONE"

    rng = candle_range(current)

    if rng < avg * 1.35:
        return "NONE"

    # Require meaningful body
    if candle_body(current) / rng < 0.60:
        return "NONE"

    if is_bullish(current):
        return "BULLISH"

    if is_bearish(current):
        return "BEARISH"

    return "NONE"


# ============================================================
# RECENT DISPLACEMENT
# ============================================================

def recent_displacement(candles):

    for i in range(
        max(16, len(candles) - 3),
        len(candles)
    ):

        subset = candles[:i + 1]

        result = displacement(subset)

        if result != "NONE":
            return result

    return "NONE"


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(
        1,
        len(candles)
    ):

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

        true_ranges.append(tr)

    return (
        sum(true_ranges[-period:])
        / period
    )


# ============================================================
# CANDLE QUALITY
# ============================================================

def directional_candle_quality(c):

    rng = candle_range(c)

    body = candle_body(c)

    ratio = body / rng

    if ratio >= 0.70:
        return 2

    if ratio >= 0.50:
        return 1

    return 0


# ============================================================
# MARKET ANALYSIS
# ============================================================

def analyze_market(
    m15,
    m5,
    m1
):

    if (
        len(m15) < 30
        or len(m5) < 30
        or len(m1) < 30
    ):
        return None

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    m15_direction = structure_direction(m15)

    m5_direction = structure_direction(m5)

    m5_bos = detect_bos(m5)

    # --------------------------------------------------------
    # M1
    # --------------------------------------------------------

    latest = m1[-1]

    sweep = liquidity_sweep(m1)

    rejection = recent_rejection(m1)

    engulfing = recent_engulfing(m1)

    displace = recent_displacement(m1)

    quality = directional_candle_quality(latest)

    atr = calculate_atr(m1)

    if not atr:
        return None

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    buy = 0
    sell = 0

    # M15
    if m15_direction == "BULLISH":
        buy += 20

    elif m15_direction == "BEARISH":
        sell += 20

    # M5
    if m5_direction == "BULLISH":
        buy += 15

    elif m5_direction == "BEARISH":
        sell += 15

    # BOS
    if m5_bos == "BULLISH":
        buy += 20

    elif m5_bos == "BEARISH":
        sell += 20

    # Sweep
    if sweep == "BULLISH":
        buy += 20

    elif sweep == "BEARISH":
        sell += 20

    # Rejection
    if rejection == "BULLISH":
        buy += 10

    elif rejection == "BEARISH":
        sell += 10

    # Engulfing
    if engulfing == "BULLISH":
        buy += 10

    elif engulfing == "BEARISH":
        sell += 10

    # Displacement
    if displace == "BULLISH":
        buy += 10

    elif displace == "BEARISH":
        sell += 10

    # Candle quality
    if quality:

        if is_bullish(latest):
            buy += quality

        elif is_bearish(latest):
            sell += quality

    # --------------------------------------------------------
    # DETERMINE DIRECTION
    # --------------------------------------------------------

    if (
        buy >= MIN_SCORE
        and buy > sell
    ):

        direction = "BUY"
        score = buy

    elif (
        sell >= MIN_SCORE
        and sell > buy
    ):

        direction = "SELL"
        score = sell

    else:

        return {
            "direction": "NONE",
            "score": max(buy, sell),
            "buy_score": buy,
            "sell_score": sell,

            "m15": m15_direction,
            "m5": m5_direction,
            "bos": m5_bos,

            "sweep": sweep,
            "rejection": rejection,
            "engulfing": engulfing,
            "displacement": displace,

            "quality": quality,
            "atr": atr,

            "candle_time": latest["datetime"],
            "open": latest["open"],
            "high": latest["high"],
            "low": latest["low"],
            "close": latest["close"]
        }

    # --------------------------------------------------------
    # ENTRY
    # --------------------------------------------------------

    entry = latest["close"]

    # --------------------------------------------------------
    # SL / TP
    # --------------------------------------------------------

    if direction == "BUY":

        structure_low = min(
            c["low"]
            for c in m1[-8:]
        )

        sl = (
            structure_low
            - atr * 0.25
        )

        risk = entry - sl

        if risk <= 0:
            return None

        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.2

    else:

        structure_high = max(
            c["high"]
            for c in m1[-8:]
        )

        sl = (
            structure_high
            + atr * 0.25
        )

        risk = sl - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.2

    return {
        "direction": direction,
        "score": score,

        "buy_score": buy,
        "sell_score": sell,

        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,

        "atr": atr,

        "m15": m15_direction,
        "m5": m5_direction,
        "bos": m5_bos,

        "sweep": sweep,
        "rejection": rejection,
        "engulfing": engulfing,
        "displacement": displace,

        "quality": quality,

        "candle_time": latest["datetime"],
        "open": latest["open"],
        "high": latest["high"],
        "low": latest["low"],
        "close": latest["close"]
    }


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(s):

    emoji = (
        "🟢"
        if s["direction"] == "BUY"
        else "🔴"
    )

    risk = abs(
        s["entry"] - s["sl"]
    )

    reward = abs(
        s["tp2"] - s["entry"]
    )

    rr = (
        reward / risk
        if risk > 0
        else 0
    )

    return (
        f"{emoji} XAUUSD "
        f"{s['direction']} SIGNAL\n\n"

        f"ENTRY: {s['entry']:.2f}\n"
        f"SL:    {s['sl']:.2f}\n"
        f"TP1:   {s['tp1']:.2f}\n"
        f"TP2:   {s['tp2']:.2f}\n\n"

        f"RR: 1:{rr:.2f}\n"
        f"SCORE: {s['score']}/100\n\n"

        f"M15: {s['m15']}\n"
        f"M5:  {s['m5']}\n"
        f"BOS: {s['bos']}\n\n"

        f"M1 SWEEP:      {s['sweep']}\n"
        f"M1 REJECTION:  {s['rejection']}\n"
        f"M1 ENGULFING:  {s['engulfing']}\n"
        f"M1 DISPLACE:   {s['displacement']}\n"
        f"M1 QUALITY:    {s['quality']}/2\n\n"

        f"ATR: {s['atr']:.2f}\n"

        f"CANDLE: {s['candle_time']}\n\n"

        f"LOT: {LOT_SIZE}\n\n"

        "⚠️ SIGNAL ONLY — MANUAL EXECUTION"
    )


# ============================================================
# NO TRADE FORMAT
# ============================================================

def format_no_trade(s):

    return (
        "⚪ XAUUSD — NO TRADE\n\n"

        "Candle structure is not "
        "strong enough yet.\n\n"

        f"SCORE: {s.get('score', 0)}/100\n"
        f"BUY SCORE: {s.get('buy_score', 0)}\n"
        f"SELL SCORE: {s.get('sell_score', 0)}\n\n"

        f"M15: {s.get('m15', 'N/A')}\n"
        f"M5:  {s.get('m5', 'N/A')}\n"
        f"BOS: {s.get('bos', 'N/A')}\n\n"

        f"M1 Sweep:       {s.get('sweep', 'N/A')}\n"
        f"M1 Rejection:   {s.get('rejection', 'N/A')}\n"
        f"M1 Engulfing:   {s.get('engulfing', 'N/A')}\n"
        f"M1 Displacement:{s.get('displacement', 'N/A')}\n\n"

        f"M1 CANDLE: "
        f"{s.get('candle_time', 'N/A')}\n"

        f"O: {s.get('open', 0):.2f}  "
        f"H: {s.get('high', 0):.2f}  "
        f"L: {s.get('low', 0):.2f}  "
        f"C: {s.get('close', 0):.2f}\n\n"

        "Waiting for a stronger "
        "candle/structure setup."
    )


# ============================================================
# SIGNAL HASH
# ============================================================

def signal_hash(signal):

    raw = (
        f"{signal['direction']}-"
        f"{signal['candle_time']}-"
        f"{signal['entry']:.2f}-"
        f"{signal['sl']:.2f}-"
        f"{signal['tp1']:.2f}"
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()


# ============================================================
# SCAN
# ============================================================

def scan(
    send_no_trade=False,
    chat_id=None
):

    global LAST_SIGNAL_HASH
    global LAST_SIGNAL_TIME
    global LAST_M1_CANDLE
    global LAST_M5_CANDLE
    global LAST_M15_CANDLE

    logging.info(
        "========== NEW XAUUSD SCAN =========="
    )

    m15 = get_candles("15min", 160)
    m5 = get_candles("5min", 160)
    m1 = get_candles("1min", 160)

    if (
        len(m15) < 30
        or len(m5) < 30
        or len(m1) < 30
    ):

        logging.error(
            "Incomplete data M15=%d M5=%d M1=%d",
            len(m15),
            len(m5),
            len(m1)
        )

        if send_no_trade:

            send_telegram(
                "⚠️ XAUUSD\n\n"
                "Market data incomplete.",
                chat_id
            )

        return

    # --------------------------------------------------------
    # FRESHNESS CHECK
    # --------------------------------------------------------

    latest_m1 = m1[-1]
    latest_m5 = m5[-1]
    latest_m15 = m15[-1]

    old_m1 = LAST_M1_CANDLE
    old_m5 = LAST_M5_CANDLE
    old_m15 = LAST_M15_CANDLE

    LAST_M1_CANDLE = latest_m1["datetime"]
    LAST_M5_CANDLE = latest_m5["datetime"]
    LAST_M15_CANDLE = latest_m15["datetime"]

    logging.info(
        "MARKET TIME | M1=%s | M5=%s | M15=%s",
        LAST_M1_CANDLE,
        LAST_M5_CANDLE,
        LAST_M15_CANDLE
    )

    if old_m1 == LAST_M1_CANDLE:

        logging.info(
            "M1 candle unchanged since previous scan."
        )

    else:

        logging.info(
            "NEW CLOSED M1 CANDLE DETECTED."
        )

    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------

    result = analyze_market(
        m15,
        m5,
        m1
    )

    if not result:

        logging.warning(
            "Analysis returned no usable setup."
        )

        if send_no_trade:

            send_telegram(
                "⚠️ XAUUSD\n\n"
                "Unable to produce a valid setup.",
                chat_id
            )

        return

    # --------------------------------------------------------
    # NO TRADE
    # --------------------------------------------------------

    if result["direction"] == "NONE":

        logging.info(
            "NO TRADE | score=%s | buy=%s | sell=%s | M1=%s",
            result["score"],
            result["buy_score"],
            result["sell_score"],
            result["candle_time"]
        )

        if send_no_trade:

            send_telegram(
                format_no_trade(result),
                chat_id
            )

        return

    # --------------------------------------------------------
    # DUPLICATE SIGNAL PROTECTION
    # --------------------------------------------------------

    current_hash = signal_hash(result)

    if current_hash == LAST_SIGNAL_HASH:

        logging.info(
            "Same signal already sent."
        )

        return

    LAST_SIGNAL_HASH = current_hash
    LAST_SIGNAL_TIME = time.time()

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

    logging.info(
        "VALID SIGNAL %s | score=%s | candle=%s",
        result["direction"],
        result["score"],
        result["candle_time"]
    )

    send_telegram(
        format_signal(result),
        chat_id
    )


# ============================================================
# BACKGROUND SCANNER
# ============================================================

def scanner_loop():

    logging.info(
        "========================================"
    )

    logging.info(
        "XAUUSD CANDLE EXPERT SCANNER STARTED"
    )

    logging.info(
        "NO TIME FILTER"
    )

    logging.info(
        "CONTINUOUS SCANNING: ON"
    )

    logging.info(
        "SCAN INTERVAL: %s seconds",
        SCAN_SECONDS
    )

    logging.info(
        "MINIMUM SCORE: %s",
        MIN_SCORE
    )

    logging.info(
        "LOT: %s",
        LOT_SIZE
    )

    logging.info(
        "========================================"
    )

    while True:

        try:

            scan()

        except Exception as e:

            logging.exception(
                "Scanner error: %s",
                e
            )

        time.sleep(
            SCAN_SECONDS
        )


# ============================================================
# HOME
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return (
        "XAUUSD Signal Bot is running.",
        200
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        message = data.get(
            "message",
            {}
        )

        if not isinstance(message, dict):
            return "OK", 200

        text = message.get(
            "text",
            ""
        )

        if not isinstance(text, str):
            return "OK", 200

        text = text.strip()

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get("id")

        if not text:
            return "OK", 200

        logging.info(
            "Telegram command=%s chat_id=%s",
            text,
            chat_id
        )

        # ----------------------------------------------------
        # START
        # ----------------------------------------------------

        if text.startswith("/start"):

            send_telegram(

                "🟡 XAUUSD GOLD "
                "CANDLE EXPERT BOT\n\n"

                "🟢 ONLINE\n"
                "🟢 NO TIME FILTER\n"
                "🟢 CONTINUOUS SCANNING\n"
                "🟢 M1/M5/M15 ANALYSIS\n\n"

                "Commands:\n"
                "/signal - analyze now\n"
                "/status - bot status\n"
                "/debug - market data",

                chat_id
            )

        # ----------------------------------------------------
        # SIGNAL
        # ----------------------------------------------------

        elif text.startswith("/signal"):

            send_telegram(

                "🔎 XAUUSD SCAN\n\n"
                "Reading fresh M15/M5/M1 data...\n"
                "Checking candle structure...\n"
                "Checking liquidity...\n"
                "Checking BOS...\n\n"
                "Please wait...",

                chat_id
            )

            scan(
                send_no_trade=True,
                chat_id=chat_id
            )

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        elif text.startswith("/status"):

            telegram_token, _, twelve_key = get_env()

            telegram_status = (
                "OK"
                if telegram_token
                else "MISSING"
            )

            data_status = (
                "OK"
                if twelve_key
                else "MISSING"
            )

            last_signal = (
                "NONE"
                if LAST_SIGNAL_TIME is None
                else "AVAILABLE"
            )

            send_telegram(

                "🟢 XAUUSD BOT STATUS\n\n"

                "TIME FILTER: OFF\n"
                "CONTINUOUS SCAN: ON\n"

                f"SCAN: {SCAN_SECONDS}s\n"
                f"MIN SCORE: {MIN_SCORE}\n"
                f"LOT: {LOT_SIZE}\n\n"

                f"Telegram API: {telegram_status}\n"
                f"Twelve Data: {data_status}\n\n"

                f"LAST M1: "
                f"{LAST_M1_CANDLE or 'NOT READ'}\n"

                f"LAST M5: "
                f"{LAST_M5_CANDLE or 'NOT READ'}\n"

                f"LAST M15: "
                f"{LAST_M15_CANDLE or 'NOT READ'}\n\n"

                f"LAST SIGNAL: {last_signal}",

                chat_id
            )

        # ----------------------------------------------------
        # DEBUG
        # ----------------------------------------------------

        elif text.startswith("/debug"):

            m15 = get_candles("15min", 40)
            m5 = get_candles("5min", 40)
            m1 = get_candles("1min", 40)

            if not m1 or not m5 or not m15:

                send_telegram(
                    "⚠️ DEBUG\n\n"
                    "Unable to retrieve market data.",
                    chat_id
                )

                return "OK", 200

            a = m1[-1]
            b = m5[-1]
            c = m15[-1]

            send_telegram(

                "🔧 XAUUSD DEBUG\n\n"

                "M1\n"
                f"Time: {a['datetime']}\n"
                f"O: {a['open']:.2f}\n"
                f"H: {a['high']:.2f}\n"
                f"L: {a['low']:.2f}\n"
                f"C: {a['close']:.2f}\n\n"

                "M5\n"
                f"Time: {b['datetime']}\n"
                f"O: {b['open']:.2f}\n"
                f"H: {b['high']:.2f}\n"
                f"L: {b['low']:.2f}\n"
                f"C: {b['close']:.2f}\n\n"

                "M15\n"
                f"Time: {c['datetime']}\n"
                f"O: {c['open']:.2f}\n"
                f"H: {c['high']:.2f}\n"
                f"L: {c['low']:.2f}\n"
                f"C: {c['close']:.2f}",

                chat_id
            )

        # ----------------------------------------------------
        # UNKNOWN
        # ----------------------------------------------------

        else:

            send_telegram(

                "Unknown command.\n\n"

                "/start\n"
                "/signal\n"
                "/status\n"
                "/debug",

                chat_id
            )

        return "OK", 200

    except Exception as e:

        logging.exception(
            "Webhook error: %s",
            e
        )

        return "OK", 200


# ============================================================
# START APPLICATION
# ============================================================

if __name__ == "__main__":

    logging.info(
        "Starting XAUUSD Telegram Bot..."
    )

    scanner_thread = threading.Thread(
        target=scanner_loop,
        daemon=True
    )

    scanner_thread.start()

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
