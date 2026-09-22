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

# Market-data cache
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

# Cache lifetimes
# M1 refreshed roughly every minute
# M5 only needs refreshing when a new M5 candle appears
# M15 only needs refreshing when a new M15 candle appears

CACHE_TTL = {
    "1min": 55,
    "5min": 240,
    "15min": 840
}

# Protect Twelve Data after HTTP 429
TWELVE_BACKOFF_UNTIL = 0
TWELVE_BACKOFF_SECONDS = 90

# Prevent simultaneous market-data requests
TWELVE_REQUEST_LOCK = threading.Lock()


# ============================================================
# BASIC CONFIG CHECK
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

def telegram_api(method, data=None, timeout=20):

    if not TELEGRAM_TOKEN:
        return None

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/{method}"
    )

    try:

        r = requests.post(
            url,
            data=data or {},
            timeout=timeout
        )

        if not r.ok:

            logging.error(
                "Telegram HTTP %s: %s",
                r.status_code,
                r.text[:500]
            )

            return None

        result = r.json()

        if not result.get("ok"):

            logging.error(
                "Telegram error: %s",
                result
            )

        return result

    except Exception as e:

        logging.error(
            "Telegram request error: %s",
            e
        )

        return None


def send_telegram(message, chat_id=None):

    target = chat_id or TELEGRAM_CHAT_ID

    if not target:

        logging.error(
            "No Telegram chat ID"
        )

        return False

    result = telegram_api(
        "sendMessage",
        {
            "chat_id": target,
            "text": message,
            "parse_mode": "HTML"
        }
    )

    return bool(
        result
        and result.get("ok")
    )


def telegram_startup_test():

    if not config_ok():

        logging.error(
            "Missing TELEGRAM_TOKEN, "
            "TELEGRAM_CHAT_ID or TWELVE_DATA_KEY"
        )

        return

    result = telegram_api(
        "getMe"
    )

    if result and result.get("ok"):

        bot = result["result"]

        logging.info(
            "Telegram connected: @%s",
            bot.get("username")
        )

        send_telegram(
            "🟢 <b>ritamariagold ONLINE</b>\n\n"
            "Gold candle scanner is running.\n"
            "Send /signal for manual XAU/USD analysis."
        )

    else:

        logging.error(
            "Telegram connection failed."
        )


# ============================================================
# TWELVE DATA
# ============================================================

def request_twelve_data(interval, outputsize=80):

    global TWELVE_BACKOFF_UNTIL

    now = time.time()

    # Do not hammer Twelve Data after a 429
    if now < TWELVE_BACKOFF_UNTIL:

        remaining = int(
            TWELVE_BACKOFF_UNTIL - now
        )

        logging.warning(
            "Twelve Data cooling down: %ss remaining",
            remaining
        )

        return []


    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "format": "JSON"
    }


    # Only one Twelve Data request at a time
    with TWELVE_REQUEST_LOCK:

        try:

            r = requests.get(
                TWELVE_URL,
                params=params,
                timeout=20
            )

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if r.status_code == 429:

                TWELVE_BACKOFF_UNTIL = (
                    time.time()
                    + TWELVE_BACKOFF_SECONDS
                )

                logging.error(
                    "Twelve Data HTTP 429 "
                    "| %s | backing off %ss",
                    interval,
                    TWELVE_BACKOFF_SECONDS
                )

                return []


            if not r.ok:

                logging.error(
                    "Twelve Data HTTP %s | %s",
                    r.status_code,
                    interval
                )

                return []


            data = r.json()


            # ------------------------------------------------
            # API ERROR INSIDE JSON
            # ------------------------------------------------

            if "values" not in data:

                logging.error(
                    "Twelve Data %s error: %s",
                    interval,
                    data
                )

                return []


            candles = []

            for x in reversed(
                data["values"]
            ):

                try:

                    candles.append(
                        {
                            "time": x["datetime"],
                            "open": float(x["open"]),
                            "high": float(x["high"]),
                            "low": float(x["low"]),
                            "close": float(x["close"])
                        }
                    )

                except Exception:

                    continue


            if not candles:

                logging.warning(
                    "Twelve Data returned "
                    "zero candles: %s",
                    interval
                )

                return []


            logging.info(
                "Twelve Data OK | %s | candles=%s | latest=%s",
                interval,
                len(candles),
                candles[-1]["time"]
            )

            return candles


        except Exception as e:

            logging.error(
                "Twelve Data request error "
                "%s: %s",
                interval,
                e
            )

            return []


# ============================================================
# CACHED MARKET DATA
# ============================================================

def get_candles(interval, outputsize=80):

    now = time.time()

    with CACHE_LOCK:

        cached = DATA_CACHE.get(interval)

        if cached:

            candles = cached["candles"]
            saved_time = cached["time"]

            if (
                candles
                and now - saved_time
                < CACHE_TTL[interval]
            ):

                logging.info(
                    "CACHE HIT | %s | candles=%s",
                    interval,
                    len(candles)
                )

                return candles


    # Cache expired — request fresh data
    candles = request_twelve_data(
        interval,
        outputsize
    )


    if candles:

        with CACHE_LOCK:

            DATA_CACHE[interval] = {
                "candles": candles,
                "time": time.time()
            }

        return candles


    # --------------------------------------------------------
    # If Twelve Data failed, use previous cached data
    # instead of declaring the market unavailable.
    # --------------------------------------------------------

    with CACHE_LOCK:

        old = DATA_CACHE.get(interval)

        if old and old["candles"]:

            logging.warning(
                "Using previous cached %s data "
                "because fresh request failed.",
                interval
            )

            return old["candles"]


    return []


# ============================================================
# CANDLE MATH
# ============================================================

def body(c):

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


def bullish(c):

    return c["close"] > c["open"]


def bearish(c):

    return c["close"] < c["open"]


def strong_body(c):

    return (
        body(c)
        / candle_range(c)
        >= 0.60
    )


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if not values:
        return None

    k = 2.0 / (
        period + 1.0
    )

    result = values[0]

    for value in values[1:]:

        result = (
            value * k
            + result * (1 - k)
        )

    return result


def trend_score(candles):

    closes = [
        x["close"]
        for x in candles
    ]

    if len(closes) < 50:

        return 0, "NEUTRAL"

    e20 = ema(
        closes[-50:],
        20
    )

    e50 = ema(
        closes[-50:],
        50
    )

    last = closes[-1]

    if last > e20 > e50:

        return 20, "BULLISH"

    if last < e20 < e50:

        return 20, "BEARISH"

    if last > e20:

        return 10, "BULLISH"

    if last < e20:

        return 10, "BEARISH"

    return 0, "NEUTRAL"


# ============================================================
# CANDLE PATTERNS
# ============================================================

def bullish_engulfing(a, b):

    return (
        bearish(a)
        and bullish(b)
        and b["open"] <= a["close"]
        and b["close"] >= a["open"]
        and body(b) > body(a)
    )


def bearish_engulfing(a, b):

    return (
        bullish(a)
        and bearish(b)
        and b["open"] >= a["close"]
        and b["close"] <= a["open"]
        and body(b) > body(a)
    )


def bullish_rejection(c):

    return (
        lower_wick(c)
        >= body(c) * 1.5
        and lower_wick(c)
        > upper_wick(c)
    )


def bearish_rejection(c):

    return (
        upper_wick(c)
        >= body(c) * 1.5
        and upper_wick(c)
        > lower_wick(c)
    )


def displacement(c):

    return (
        strong_body(c)
        and body(c)
        / candle_range(c)
        >= 0.70
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_analysis(candles):

    if len(candles) < 15:

        return 0, "NEUTRAL"

    recent = candles[-12:]

    highs = [
        x["high"]
        for x in recent[:-2]
    ]

    lows = [
        x["low"]
        for x in recent[:-2]
    ]

    last = recent[-1]

    previous_high = max(highs)
    previous_low = min(lows)

    if last["close"] > previous_high:

        return 20, "BULLISH BOS"

    if last["close"] < previous_low:

        return 20, "BEARISH BOS"

    if last["close"] > recent[-3]["high"]:

        return 10, "BULLISH STRUCTURE"

    if last["close"] < recent[-3]["low"]:

        return 10, "BEARISH STRUCTURE"

    return 0, "NEUTRAL"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def liquidity_sweep(candles):

    if len(candles) < 10:

        return 0, "NONE"

    previous = candles[-8:-2]

    last = candles[-1]

    high = max(
        x["high"]
        for x in previous
    )

    low = min(
        x["low"]
        for x in previous
    )


    # Sell-side sweep
    if (
        last["low"] < low
        and last["close"] > low
        and bullish_rejection(last)
    ):

        return (
            20,
            "BULLISH LIQUIDITY SWEEP"
        )


    # Buy-side sweep
    if (
        last["high"] > high
        and last["close"] < high
        and bearish_rejection(last)
    ):

        return (
            20,
            "BEARISH LIQUIDITY SWEEP"
        )


    return 0, "NONE"


# ============================================================
# M1 ENTRY
# ============================================================

def entry_analysis(m1):

    if len(m1) < 5:

        return 0, "NEUTRAL"

    a = m1[-2]
    b = m1[-1]

    score = 0
    reasons = []


    if bullish_engulfing(a, b):

        score += 30
        reasons.append(
            "Bullish engulfing"
        )

    elif bearish_engulfing(a, b):

        score += 30
        reasons.append(
            "Bearish engulfing"
        )


    if bullish_rejection(b):

        score += 20
        reasons.append(
            "Bullish rejection"
        )


    if bearish_rejection(b):

        score += 20
        reasons.append(
            "Bearish rejection"
        )


    if displacement(b):

        score += 15
        reasons.append(
            "Displacement"
        )


    if bullish(b):

        direction = "BUY"

    elif bearish(b):

        direction = "SELL"

    else:

        direction = "NEUTRAL"


    return (
        min(score, 40),
        direction
        + (
            " | "
            + ", ".join(reasons)
            if reasons
            else ""
        )
    )


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
            abs(
                c["high"]
                - p["close"]
            ),
            abs(
                c["low"]
                - p["close"]
            )
        )

        trs.append(tr)


    recent = trs[-period:]

    return (
        sum(recent)
        / len(recent)
    )


# ============================================================
# COMPLETE MARKET ANALYSIS
# ============================================================

def analyze_market():

    # --------------------------------------------------------
    # These now use cache.
    #
    # Normal scan:
    # M1  -> approximately once/minute
    # M5  -> approximately every 4 minutes
    # M15 -> approximately every 14 minutes
    #
    # This dramatically reduces Twelve Data usage.
    # --------------------------------------------------------

    m1 = get_candles(
        "1min",
        80
    )

    m5 = get_candles(
        "5min",
        80
    )

    m15 = get_candles(
        "15min",
        80
    )


    if (
        len(m1) < 30
        or len(m5) < 30
        or len(m15) < 30
    ):

        return {
            "valid": False,
            "reason": (
                "Not enough market data."
            )
        }


    price = m1[-1]["close"]


    # --------------------------------------------------------
    # M15
    # --------------------------------------------------------

    m15_trend_score, m15_trend = (
        trend_score(m15)
    )


    # --------------------------------------------------------
    # M5
    # --------------------------------------------------------

    m5_trend_score, m5_trend = (
        trend_score(m5)
    )

    structure_score, structure = (
        structure_analysis(m5)
    )


    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    liquidity_score, liquidity = (
        liquidity_sweep(m5)
    )


    # --------------------------------------------------------
    # M1 ENTRY
    # --------------------------------------------------------

    entry_score, entry_signal = (
        entry_analysis(m1)
    )


    buy_points = 0
    sell_points = 0


    # M15
    if m15_trend == "BULLISH":

        buy_points += m15_trend_score

    elif m15_trend == "BEARISH":

        sell_points += m15_trend_score


    # M5
    if m5_trend == "BULLISH":

        buy_points += m5_trend_score

    elif m5_trend == "BEARISH":

        sell_points += m5_trend_score


    # Structure
    if "BULLISH" in structure:

        buy_points += structure_score

    elif "BEARISH" in structure:

        sell_points += structure_score


    # Liquidity
    if "BULLISH" in liquidity:

        buy_points += liquidity_score

    elif "BEARISH" in liquidity:

        sell_points += liquidity_score


    # M1
    if entry_signal.startswith("BUY"):

        buy_points += entry_score

    elif entry_signal.startswith("SELL"):

        sell_points += entry_score


    if buy_points > sell_points:

        direction = "BUY"

    elif sell_points > buy_points:

        direction = "SELL"

    else:

        direction = "NONE"


    final_score = max(
        buy_points,
        sell_points
    )


    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr_value = atr(
        m5,
        14
    )


    if atr_value is None:

        return {
            "valid": False,
            "reason": "ATR unavailable."
        }


    # --------------------------------------------------------
    # Directional agreement
    # --------------------------------------------------------

    if direction == "BUY":

        if m15_trend == "BEARISH":

            final_score -= 20

        if m5_trend == "BEARISH":

            final_score -= 15


    elif direction == "SELL":

        if m15_trend == "BULLISH":

            final_score -= 20

        if m5_trend == "BULLISH":

            final_score -= 15


    final_score = max(
        0,
        final_score
    )


    # --------------------------------------------------------
    # ENTRY / SL / TP
    # --------------------------------------------------------

    if direction == "BUY":

        entry = price

        sl = (
            entry
            - atr_value * 1.10
        )

        risk = entry - sl

        tp1 = (
            entry
            + risk * 1.20
        )

        tp2 = (
            entry
            + risk * 2.00
        )


    elif direction == "SELL":

        entry = price

        sl = (
            entry
            + atr_value * 1.10
        )

        risk = sl - entry

        tp1 = (
            entry
            - risk * 1.20
        )

        tp2 = (
            entry
            - risk * 2.00
        )


    else:

        entry = price
        sl = price
        tp1 = price
        tp2 = price


    # --------------------------------------------------------
    # SETUP ID
    # --------------------------------------------------------

    setup_source = (
        m5[-1]["time"]
        + direction
        + str(round(entry, 2))
        + structure
        + liquidity
    )


    setup_id = hashlib.sha256(
        setup_source.encode()
    ).hexdigest()[:16]


    return {
        "valid": True,
        "direction": direction,
        "score": final_score,
        "price": price,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "atr": atr_value,
        "m15_trend": m15_trend,
        "m5_trend": m5_trend,
        "structure": structure,
        "liquidity": liquidity,
        "entry_signal": entry_signal,
        "setup_id": setup_id
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(a, manual=False):

    if not a["valid"]:

        return (
            "⚠️ <b>XAU/USD</b>\n\n"
            + a["reason"]
        )


    if a["direction"] == "NONE":

        return (
            "👀 <b>XAU/USD — NO TRADE</b>\n\n"
            f"Score: <b>{a['score']}/100</b>\n"
            f"Price: <b>{a['price']:.2f}</b>\n\n"
            f"M15: {a['m15_trend']}\n"
            f"M5: {a['m5_trend']}\n"
            f"Structure: {a['structure']}\n"
            f"Liquidity: {a['liquidity']}\n"
            f"M1: {a['entry_signal']}\n\n"
            "Waiting for a cleaner candle setup."
        )


    if a["score"] < MIN_SCORE:

        return (
            "👀 <b>XAU/USD — WAIT</b>\n\n"
            f"Direction: {a['direction']}\n"
            f"Score: <b>{a['score']}/100</b>\n"
            f"Price: <b>{a['price']:.2f}</b>\n\n"
            f"M15: {a['m15_trend']}\n"
            f"M5: {a['m5_trend']}\n"
            f"Structure: {a['structure']}\n"
            f"Liquidity: {a['liquidity']}\n"
            f"M1: {a['entry_signal']}\n\n"
            f"Minimum required: {MIN_SCORE}\n"
            "No trade yet."
        )


    emoji = (
        "🟢"
        if a["direction"] == "BUY"
        else "🔴"
    )

    mode = (
        "MANUAL"
        if manual
        else "AUTO"
    )


    return (
        f"{emoji} <b>XAU/USD "
        f"{a['direction']} — {mode}</b>\n\n"
        f"<b>Score:</b> {a['score']}/100\n"
        f"<b>Entry:</b> {a['entry']:.2f}\n"
        f"<b>SL:</b> {a['sl']:.2f}\n"
        f"<b>TP1:</b> {a['tp1']:.2f}\n"
        f"<b>TP2:</b> {a['tp2']:.2f}\n\n"
        f"<b>LOT:</b> {LOT_SIZE}\n\n"
        f"M15: {a['m15_trend']}\n"
        f"M5: {a['m5_trend']}\n"
        f"Structure: {a['structure']}\n"
        f"Liquidity: {a['liquidity']}\n"
        f"M1: {a['entry_signal']}\n\n"
        "⚠️ Signal only — verify price before execution."
    )


# ============================================================
# AUTOMATIC SCANNER
# ============================================================

def scanner():

    global LAST_AUTO_SETUP_ID

    logging.info(
        "Automatic XAU/USD scanner started."
    )


    while True:

        try:

            if not config_ok():

                logging.error(
                    "Configuration incomplete."
                )

                time.sleep(60)

                continue


            analysis = analyze_market()


            if not analysis.get("valid"):

                logging.warning(
                    "Scanner: %s",
                    analysis.get("reason")
                )


            else:

                logging.info(
                    "SCAN | %s | score=%s | price=%.2f",
                    analysis["direction"],
                    analysis["score"],
                    analysis["price"]
                )


                if (
                    analysis["direction"]
                    != "NONE"
                    and analysis["score"]
                    >= MIN_SCORE
                ):

                    setup_id = (
                        analysis["setup_id"]
                    )


                    if (
                        setup_id
                        != LAST_AUTO_SETUP_ID
                    ):

                        message = (
                            format_signal(
                                analysis,
                                manual=False
                            )
                        )


                        if send_telegram(
                            message
                        ):

                            LAST_AUTO_SETUP_ID = (
                                setup_id
                            )

                            logging.info(
                                "AUTO SIGNAL SENT: %s",
                                setup_id
                            )

                        else:

                            logging.error(
                                "AUTO SIGNAL FAILED TO SEND"
                            )


            time.sleep(
                SCAN_SECONDS
            )


        except Exception as e:

            logging.exception(
                "Scanner exception: %s",
                e
            )

            time.sleep(30)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def handle_command(text, chat_id):

    text = (
        text
        or ""
    ).strip()


    if not text:

        return


    command = (
        text
        .split()[0]
        .lower()
    )


    if command.startswith(
        "/start"
    ):

        send_telegram(

            "🟢 <b>ritamariagold</b>\n\n"
            "XAU/USD candle-analysis bot "
            "is online.\n\n"
            "<b>Commands:</b>\n"
            "/signal — analyze gold now\n"
            "/status — check bot status\n"
            "/help — show commands",

            chat_id
        )


    elif command.startswith(
        "/help"
    ):

        send_telegram(

            "📊 <b>ritamariagold</b>\n\n"
            "/signal — immediate XAU/USD analysis\n"
            "/status — bot/data status\n"
            "/help — commands",

            chat_id
        )


    elif command.startswith(
        "/status"
    ):

        send_telegram(

            "🟢 <b>BOT STATUS</b>\n\n"
            "Telegram: CONNECTED\n"
            "Scanner: RUNNING\n"
            "Symbol: XAU/USD\n"
            "Timeframe: M1 + M5 + M15\n"
            f"Minimum score: {MIN_SCORE}\n"
            f"Lot: {LOT_SIZE}\n\n"
            "Twelve Data cache: ACTIVE",

            chat_id
        )


    elif command.startswith(
        "/signal"
    ):

        send_telegram(

            "🔎 <b>Analyzing XAU/USD...</b>\n"
            "Reading M1 / M5 / M15 candles.",

            chat_id
        )


        analysis = analyze_market()


        global LAST_MANUAL_SETUP_ID


        message = format_signal(
            analysis,
            manual=True
        )


        if analysis.get("valid"):

            LAST_MANUAL_SETUP_ID = (
                analysis.get("setup_id")
            )


        send_telegram(
            message,
            chat_id
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():

    global LAST_UPDATE_ID

    logging.info(
        "Telegram polling started."
    )


    telegram_api(
        "deleteWebhook",
        {
            "drop_pending_updates": False
        }
    )


    while True:

        try:

            result = telegram_api(

                "getUpdates",

                {
                    "offset":
                        LAST_UPDATE_ID + 1,

                    "timeout": 25,

                    "allowed_updates":
                        '["message"]'
                },

                timeout=35
            )


            if (
                not result
                or not result.get("ok")
            ):

                time.sleep(5)

                continue


            updates = result.get(
                "result",
                []
            )


            for update in updates:

                LAST_UPDATE_ID = (
                    update["update_id"]
                )


                message = update.get(
                    "message"
                )


                if not message:

                    continue


                text = message.get(
                    "text",
                    ""
                )


                chat = message.get(
                    "chat",
                    {}
                )


                chat_id = chat.get(
                    "id"
                )


                if not chat_id:

                    continue


                logging.info(
                    "Telegram message from %s: %s",
                    chat_id,
                    text
                )


                if text.startswith("/"):

                    handle_command(
                        text,
                        chat_id
                    )


        except Exception as e:

            logging.exception(
                "Telegram polling error: %s",
                e
            )

            time.sleep(5)


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "status": "online",

        "bot": "ritamariagold",

        "symbol": SYMBOL,

        "scanner": "running",

        "cache": "active"

    })


@app.route("/health")
def health():

    return jsonify({

        "status": "healthy",

        "telegram":
            bool(TELEGRAM_TOKEN),

        "telegram_chat_id":
            bool(TELEGRAM_CHAT_ID),

        "twelve_data":
            bool(TWELVE_DATA_KEY)

    })


# ============================================================
# START
# ============================================================

def start_background_services():

    telegram_startup_test()


    threading.Thread(
        target=telegram_polling,
        daemon=True
    ).start()


    threading.Thread(
        target=scanner,
        daemon=True
    ).start()


start_background_services()


# ============================================================
# RENDER
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )


    app.run(
        host="0.0.0.0",
        port=port
    )
