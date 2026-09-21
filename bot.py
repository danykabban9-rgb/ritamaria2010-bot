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

# Scan continuously — NO TIME FILTER
SCAN_SECONDS = 60

# Minimum setup score
MIN_SCORE = 78

# Small-account protection
LOT_SIZE = "0.01 ONLY"

# Prevent duplicate alerts
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
# ENVIRONMENT
# ============================================================

def get_env():

    telegram_token = os.getenv("TELEGRAM_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    twelve_data_key = os.getenv("TWELVE_DATA_KEY")

    return (
        telegram_token,
        telegram_chat_id,
        twelve_data_key
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message, chat_id=None):

    telegram_token, default_chat_id, _ = get_env()

    if not telegram_token:

        logging.error(
            "TELEGRAM_TOKEN is missing."
        )

        return False

    target_chat_id = (
        chat_id
        if chat_id is not None
        else default_chat_id
    )

    if not target_chat_id:

        logging.error(
            "No Telegram chat ID available."
        )

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

        logging.info(
            "Telegram message sent successfully."
        )

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

    url = (
        "https://api.twelvedata.com/time_series"
    )

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
                "Invalid Twelve Data response for %s",
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

        values = data.get(
            "values",
            []
        )

        if not isinstance(values, list):

            logging.error(
                "Twelve Data values is not a list: %s",
                interval
            )

            return []

        raw_candles = []

        for item in values:

            if not isinstance(item, dict):
                continue

            try:

                candle = {
                    "datetime": str(
                        item["datetime"]
                    ),
                    "open": float(
                        item["open"]
                    ),
                    "high": float(
                        item["high"]
                    ),
                    "low": float(
                        item["low"]
                    ),
                    "close": float(
                        item["close"]
                    )
                }

                if (
                    candle["high"]
                    < candle["low"]
                ):
                    continue

                raw_candles.append(
                    candle
                )

            except (
                KeyError,
                TypeError,
                ValueError
            ):

                continue

        if len(raw_candles) < 3:

            logging.error(
                "Not enough valid candles for %s",
                interval
            )

            return []

        # Twelve Data returns newest first.
        #
        # The first candle can be the currently forming
        # candle. Remove it so the strategy works only with
        # confirmed/closed candles.
        #
        # Then reverse to oldest -> newest.

        closed_candles = raw_candles[1:]

        closed_candles.reverse()

        logging.info(
            "%s closed candles: %d | latest=%s",
            interval,
            len(closed_candles),
            closed_candles[-1]["datetime"]
        )

        return closed_candles

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
        - max(
            c["open"],
            c["close"]
        )
    )


def lower_wick(c):

    return (
        min(
            c["open"],
            c["close"]
        )
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

    try:

        return (
            sum(
                candle_range(c)
                for c in recent
            )
            / period
        )

    except Exception:

        return None


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_direction(candles):

    if len(candles) < 20:
        return "NEUTRAL"

    recent = candles[-20:]

    highs = [
        c["high"]
        for c in recent
    ]

    lows = [
        c["low"]
        for c in recent
    ]

    first_high = max(
        highs[:10]
    )

    second_high = max(
        highs[10:]
    )

    first_low = min(
        lows[:10]
    )

    second_low = min(
        lows[10:]
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
# BREAK OF STRUCTURE
# ============================================================

def detect_bos(candles):

    if len(candles) < 12:
        return "NONE"

    reference = candles[-7:-1]

    current = candles[-1]

    previous_high = max(
        c["high"]
        for c in reference
    )

    previous_low = min(
        c["low"]
        for c in reference
    )

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

    previous_high = max(
        c["high"]
        for c in previous
    )

    previous_low = min(
        c["low"]
        for c in previous
    )

    # Bullish liquidity sweep:
    # price takes previous low and closes back above it

    if (
        current["low"] < previous_low
        and current["close"] > previous_low
    ):

        return "BULLISH"

    # Bearish liquidity sweep:
    # price takes previous high and closes back below it

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

    if not isinstance(c, dict):

        return "NONE"

    rng = candle_range(c)

    body = candle_body(c)

    if rng <= 0:

        return "NONE"

    # Ignore extremely small dojis
    if body <= rng * 0.05:

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
        and current["open"]
        <= previous["close"]
        and current["close"]
        >= previous["open"]
    ):

        return "BULLISH"

    # Bearish engulfing

    if (
        is_bullish(previous)
        and is_bearish(current)
        and current["open"]
        >= previous["close"]
        and current["close"]
        <= previous["open"]
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

    avg = average_range(
        candles[:-1],
        14
    )

    if not avg:

        return "NONE"

    current_range = candle_range(
        current
    )

    # Strong expansion candle

    if current_range < avg * 1.5:

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

    true_ranges = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]

        previous = candles[i - 1]

        tr = max(
            current["high"]
            - current["low"],

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

    if len(true_ranges) < period:

        return None

    return (
        sum(
            true_ranges[-period:]
        )
        / period
    )


# ============================================================
# M1 CANDLE QUALITY
# ============================================================

def directional_candle_quality(c):

    rng = candle_range(c)

    body = candle_body(c)

    if rng <= 0:

        return 0

    ratio = body / rng

    if ratio >= 0.70:

        return 2

    if ratio >= 0.50:

        return 1

    return 0


# ============================================================
# FULL MARKET ANALYSIS
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

        logging.warning(
            "Insufficient candle history."
        )

        return None

    # ========================================================
    # HIGHER-TIMEFRAME STRUCTURE
    # ========================================================

    m15_direction = structure_direction(
        m15
    )

    m5_direction = structure_direction(
        m5
    )

    m5_bos = detect_bos(
        m5
    )

    # ========================================================
    # M1 CANDLE EXPERT
    # ========================================================

    latest_m1 = m1[-1]

    m1_sweep = liquidity_sweep(
        m1
    )

    # IMPORTANT:
    # One candle is passed here.
    # This fixes the original TypeError.

    m1_rejection = rejection_signal(
        latest_m1
    )

    m1_engulfing = engulfing_signal(
        m1
    )

    m1_displacement = displacement(
        m1
    )

    m1_quality = directional_candle_quality(
        latest_m1
    )

    atr = calculate_atr(
        m1
    )

    if not atr:

        return None

    # ========================================================
    # SCORE
    # ========================================================

    score_buy = 0
    score_sell = 0

    # M15 = primary directional context

    if m15_direction == "BULLISH":

        score_buy += 20

    elif m15_direction == "BEARISH":

        score_sell += 20

    # M5 = intermediate structure

    if m5_direction == "BULLISH":

        score_buy += 15

    elif m5_direction == "BEARISH":

        score_sell += 15

    # M5 BOS

    if m5_bos == "BULLISH":

        score_buy += 20

    elif m5_bos == "BEARISH":

        score_sell += 20

    # M1 liquidity

    if m1_sweep == "BULLISH":

        score_buy += 20

    elif m1_sweep == "BEARISH":

        score_sell += 20

    # M1 rejection

    if m1_rejection == "BULLISH":

        score_buy += 10

    elif m1_rejection == "BEARISH":

        score_sell += 10

    # M1 engulfing

    if m1_engulfing == "BULLISH":

        score_buy += 10

    elif m1_engulfing == "BEARISH":

        score_sell += 10

    # M1 displacement

    if m1_displacement == "BULLISH":

        score_buy += 10

    elif m1_displacement == "BEARISH":

        score_sell += 10

    # ========================================================
    # EXTRA CANDLE QUALITY
    # ========================================================

    if m1_quality > 0:

        if is_bullish(latest_m1):

            score_buy += m1_quality

        elif is_bearish(latest_m1):

            score_sell += m1_quality

    # ========================================================
    # DIRECTION
    # ========================================================

    if (
        score_buy >= MIN_SCORE
        and score_buy > score_sell
    ):

        direction = "BUY"

        score = score_buy

    elif (
        score_sell >= MIN_SCORE
        and score_sell > score_buy
    ):

        direction = "SELL"

        score = score_sell

    else:

        return {
            "direction": "NONE",
            "score": max(
                score_buy,
                score_sell
            ),
            "m15": m15_direction,
            "m5": m5_direction,
            "bos": m5_bos,
            "sweep": m1_sweep,
            "rejection": m1_rejection,
            "engulfing": m1_engulfing,
            "displacement": m1_displacement,
            "quality": m1_quality,
            "atr": atr
        }

    # ========================================================
    # ENTRY
    # ========================================================

    entry = latest_m1["close"]

    # ========================================================
    # STOP / TARGET
    # ========================================================

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

        tp1 = (
            entry
            + risk * 1.5
        )

        tp2 = (
            entry
            + risk * 2.2
        )

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

        tp1 = (
            entry
            - risk * 1.5
        )

        tp2 = (
            entry
            - risk * 2.2
        )

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
        "displacement": m1_displacement,
        "quality": m1_quality,
        "candle_time": latest_m1["datetime"]
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
        s["entry"]
        - s["sl"]
    )

    reward = abs(
        s["tp2"]
        - s["entry"]
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

        f"M15 STRUCTURE: "
        f"{s['m15']}\n"

        f"M5 STRUCTURE:  "
        f"{s['m5']}\n"

        f"M5 BOS:        "
        f"{s['bos']}\n\n"

        f"M1 LIQUIDITY:  "
        f"{s['sweep']}\n"

        f"M1 REJECTION:  "
        f"{s['rejection']}\n"

        f"M1 ENGULFING:  "
        f"{s['engulfing']}\n"

        f"M1 DISPLACE:   "
        f"{s['displacement']}\n"

        f"M1 QUALITY:    "
        f"{s['quality']}/2\n\n"

        f"ATR: {s['atr']:.2f}\n"

        f"CANDLE: "
        f"{s['candle_time']}\n\n"

        f"LOT: {LOT_SIZE}\n\n"

        "⚠️ SIGNAL ONLY — "
        "MANUAL EXECUTION"
    )


# ============================================================
# NO TRADE
# ============================================================

def format_no_trade(s):

    return (
        "⚪ XAUUSD — NO TRADE\n\n"

        "Candle structure is not "
        "strong enough yet.\n\n"

        f"SCORE: "
        f"{s.get('score', 0)}/100\n\n"

        f"M15: "
        f"{s.get('m15', 'N/A')}\n"

        f"M5:  "
        f"{s.get('m5', 'N/A')}\n"

        f"BOS: "
        f"{s.get('bos', 'N/A')}\n\n"

        f"M1 Sweep: "
        f"{s.get('sweep', 'N/A')}\n"

        f"M1 Rejection: "
        f"{s.get('rejection', 'N/A')}\n"

        f"M1 Engulfing: "
        f"{s.get('engulfing', 'N/A')}\n"

        f"M1 Displacement: "
        f"{s.get('displacement', 'N/A')}\n\n"

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

    logging.info(
        "Starting XAUUSD scan..."
    )

    # --------------------------------------------------------
    # GET CLOSED CANDLES
    # --------------------------------------------------------

    m15 = get_candles(
        "15min",
        160
    )

    m5 = get_candles(
        "5min",
        160
    )

    m1 = get_candles(
        "1min",
        160
    )

    if (
        len(m15) < 30
        or len(m5) < 30
        or len(m1) < 30
    ):

        logging.error(
            "Incomplete market data: "
            "M15=%d M5=%d M1=%d",
            len(m15),
            len(m5),
            len(m1)
        )

        if send_no_trade:

            send_telegram(
                "⚠️ XAUUSD\n\n"
                "Market data is incomplete. "
                "Try again in a moment.",
                chat_id
            )

        return

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
                "The candle data could not "
                "produce a valid setup.",
                chat_id
            )

        return

    # --------------------------------------------------------
    # NO TRADE
    # --------------------------------------------------------

    if result["direction"] == "NONE":

        logging.info(
            "No setup | score=%s",
            result["score"]
        )

        if send_no_trade:

            send_telegram(
                format_no_trade(
                    result
                ),
                chat_id
            )

        return

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    current_hash = signal_hash(
        result
    )

    if current_hash == LAST_SIGNAL_HASH:

        logging.info(
            "Same setup already sent."
        )

        return

    LAST_SIGNAL_HASH = current_hash

    LAST_SIGNAL_TIME = time.time()

    # --------------------------------------------------------
    # SEND SIGNAL
    # --------------------------------------------------------

    message = format_signal(
        result
    )

    logging.info(
        "VALID SIGNAL: %s | SCORE=%s",
        result["direction"],
        result["score"]
    )

    send_telegram(
        message,
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
        "SCAN INTERVAL: %s seconds",
        SCAN_SECONDS
    )

    logging.info(
        "MINIMUM SCORE: %s",
        MIN_SCORE
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
# TELEGRAM WEBHOOK
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

        if not isinstance(
            message,
            dict
        ):

            return "OK", 200

        text = message.get(
            "text",
            ""
        )

        if not isinstance(
            text,
            str
        ):

            return "OK", 200

        text = text.strip()

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        if not text:

            return "OK", 200

        logging.info(
            "Telegram command=%s chat_id=%s",
            text,
            chat_id
        )

        # ====================================================
        # START
        # ====================================================

        if text.startswith(
            "/start"
        ):

            send_telegram(

                "🟡 XAUUSD GOLD "
                "CANDLE EXPERT BOT\n\n"

                "🟢 Bot is online.\n"
                "🟢 No time restriction.\n"
                "🟢 Continuous scanning.\n\n"

                "Commands:\n"
                "/signal — analyze now\n"
                "/status — bot status",

                chat_id
            )

        # ====================================================
        # SIGNAL
        # ====================================================

        elif text.startswith(
            "/signal"
        ):

            send_telegram(

                "🔎 XAUUSD SCAN\n\n"
                "Analyzing:\n"
                "M15 structure\n"
                "M5 structure + BOS\n"
                "M1 liquidity + candle patterns\n\n"
                "Please wait...",

                chat_id
            )

            scan(
                send_no_trade=True,
                chat_id=chat_id
            )

        # ====================================================
        # STATUS
        # ====================================================

        elif text.startswith(
            "/status"
        ):

            telegram_token, _, twelve_key = (
                get_env()
            )

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

                "🟢 XAUUSD BOT ONLINE\n\n"

                "TIME FILTER: OFF\n"
                "CONTINUOUS SCAN: ON\n\n"

                f"Scan interval: "
                f"{SCAN_SECONDS}s\n"

                f"Minimum score: "
                f"{MIN_SCORE}/100\n"

                f"Lot: {LOT_SIZE}\n\n"

                f"Telegram API: "
                f"{telegram_status}\n"

                f"Twelve Data: "
                f"{data_status}\n\n"

                f"Last signal: "
                f"{last_signal}",

                chat_id
            )

        # ====================================================
        # UNKNOWN COMMAND
        # ====================================================

        else:

            send_telegram(

                "Unknown command.\n\n"

                "Available commands:\n"
                "/start\n"
                "/signal\n"
                "/status",

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
# START
# ============================================================

if __name__ == "__main__":

    logging.info(
        "Starting XAUUSD Telegram Bot..."
    )

    # Start scanner
    scanner_thread = threading.Thread(
        target=scanner_loop,
        daemon=True
    )

    scanner_thread.start()

    # Render supplies PORT
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
