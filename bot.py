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

# Active scalper
WATCH_SCORE = 50
ENTRY_SCORE = 60

LOT_SIZE = "0.01 ONLY"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

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

    url = f"https://api.telegram.org/bot{token}/sendMessage"

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
                "Twelve Data %s error: %s",
                interval,
                data.get("message")
            )

            return []

        values = data.get("values", [])

        if not isinstance(values, list):

            logging.error(
                "%s returned invalid candle data",
                interval
            )

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

        # Need enough candles BEFORE removing
        # the newest potentially-forming candle.

        if len(candles) < 31:

            logging.error(
                "%s incomplete BEFORE filtering: %d candles",
                interval,
                len(candles)
            )

            return []

        # Twelve Data returns newest first.
        # Remove newest candle because it may still be forming.

        candles = candles[1:]

        # Convert oldest -> newest.

        candles.reverse()

        # Final validation.

        if len(candles) < 30:

            logging.error(
                "%s incomplete AFTER filtering: %d closed candles",
                interval,
                len(candles)
            )

            return []

        latest = candles[-1]

        logging.info(
            "%s | %d CLOSED candles | latest=%s | "
            "O=%.2f H=%.2f L=%.2f C=%.2f",
            interval,
            len(candles),
            latest["datetime"],
            latest["open"],
            latest["high"],
            latest["low"],
            latest["close"]
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

    return abs(
        c["close"] - c["open"]
    )


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

    return (
        sum(trs[-period:]) / period
    )


# ============================================================
# STRUCTURE
# ============================================================

def structure(candles, lookback=20):

    if len(candles) < lookback:
        return "RANGE"

    x = candles[-lookback:]

    half = lookback // 2

    first = x[:half]
    second = x[half:]

    first_high = max(
        c["high"] for c in first
    )

    first_low = min(
        c["low"] for c in first
    )

    second_high = max(
        c["high"] for c in second
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
        x["high"] for x in reference
    )

    low = min(
        x["low"] for x in reference
    )

    # Sell-side liquidity swept
    # then price closes back above.

    if (
        c["low"] < low
        and c["close"] > low
    ):

        return "BULLISH"

    # Buy-side liquidity swept
    # then price closes back below.

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
        lower(c) >= max(
            b * 1.4,
            r * 0.30
        )
        and c["close"] > c["low"] + r * 0.55
    ):

        return "BULLISH"

    if (
        upper(c) >= max(
            b * 1.4,
            r * 0.30
        )
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

    avg = (
        sum(
            rng(x)
            for x in candles[-11:-1]
        )
        / 10
    )

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
        x["high"] for x in previous
    )

    l = min(
        x["low"] for x in previous
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
            and last["close"]
            > previous[-1]["close"]
        )

    if direction == "SELL":

        had_bullish = any(
            bull(x)
            for x in previous
        )

        return (
            had_bullish
            and bear(last)
            and last["close"]
            < previous[-1]["close"]
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
        x["high"] for x in reference
    )

    low = min(
        x["low"] for x in reference
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
    # M15 BACKGROUND
    # --------------------------------------------------------

    if m15_structure == "BULLISH":

        buy += 5
        buy_reasons.append("M15 bullish")

    elif m15_structure == "BEARISH":

        sell += 5
        sell_reasons.append("M15 bearish")

    # --------------------------------------------------------
    # LIQUIDITY SWEEP
    # --------------------------------------------------------

    if sw == "BULLISH":

        buy += 20
        buy_reasons.append("liquidity sweep")

    elif sw == "BEARISH":

        sell += 20
        sell_reasons.append("liquidity sweep")

    # --------------------------------------------------------
    # REJECTION
    # --------------------------------------------------------

    if rej == "BULLISH":

        buy += 12
        buy_reasons.append("bullish rejection")

    elif rej == "BEARISH":

        sell += 12
        sell_reasons.append("bearish rejection")

    # --------------------------------------------------------
    # ENGULFING
    # --------------------------------------------------------

    if eng == "BULLISH":

        buy += 10
        buy_reasons.append("bullish engulfing")

    elif eng == "BEARISH":

        sell += 10
        sell_reasons.append("bearish engulfing")

    # --------------------------------------------------------
    # DISPLACEMENT
    # --------------------------------------------------------

    if disp == "BULLISH":

        buy += 12
        buy_reasons.append("bullish displacement")

    elif disp == "BEARISH":

        sell += 12
        sell_reasons.append("bearish displacement")

    # --------------------------------------------------------
    # MICRO BOS
    # --------------------------------------------------------

    if bos == "BULLISH":

        buy += 15
        buy_reasons.append("M1 BOS")

    elif bos == "BEARISH":

        sell += 15
        sell_reasons.append("M1 BOS")

    # --------------------------------------------------------
    # BREAKOUT
    # --------------------------------------------------------

    if brk == "BULLISH":

        buy += 12
        buy_reasons.append("M1 breakout")

    elif brk == "BEARISH":

        sell += 12
        sell_reasons.append("M1 breakout")

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    if m5_structure == "BULLISH":

        if pullback_signal(m1, "BUY"):

            buy += 10
            buy_reasons.append("M1 pullback")

    elif m5_structure == "BEARISH":

        if pullback_signal(m1, "SELL"):

            sell += 10
            sell_reasons.append("M1 pullback")

    # --------------------------------------------------------
    # DETERMINE DIRECTION
    # --------------------------------------------------------

    difference = abs(
        buy - sell
    )

    if difference < 8:

        direction = "NONE"
        score = max(buy, sell)

    elif buy > sell:

        direction = "BUY"
        score = buy

    else:

        direction = "SELL"
        score = sell

    # --------------------------------------------------------
    # SETUP TYPE
    # --------------------------------------------------------

    if direction == "BUY":

        if (
            sw == "BULLISH"
            and (
                rej == "BULLISH"
                or eng == "BULLISH"
            )
        ):

            setup = "REVERSAL SCALP"

        elif (
            m5_structure == "BULLISH"
            and pullback_signal(m1, "BUY")
        ):

            setup = "TREND CONTINUATION"

        elif (
            brk == "BULLISH"
            or bos == "BULLISH"
        ):

            setup = "BREAKOUT SCALP"

        else:

            setup = "M1 MOMENTUM"

    elif direction == "SELL":

        if (
            sw == "BEARISH"
            and (
                rej == "BEARISH"
                or eng == "BEARISH"
            )
        ):

            setup = "REVERSAL SCALP"

        elif (
            m5_structure == "BEARISH"
            and pullback_signal(m1, "SELL")
        ):

            setup = "TREND CONTINUATION"

        elif (
            brk == "BEARISH"
            or bos == "BEARISH"
        ):

            setup = "BREAKOUT SCALP"

        else:

            setup = "M1 MOMENTUM"

    else:

        setup = "MIXED"

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if direction == "NONE":

        status = "WAIT"

    elif score >= ENTRY_SCORE:

        status = "SIGNAL"

    elif score >= WATCH_SCORE:

        status = "WATCH"

    else:

        status = "WAIT"

    # --------------------------------------------------------
    # PRICE / SL / TP
    # --------------------------------------------------------

    last = m1[-1]

    entry = last["close"]

    a = atr(m1, 14)

    if a is None or a <= 0:

        a = rng(last)

    if direction == "BUY":

        swing = recent_low(
            m1,
            8
        )

        sl = swing - (
            a * 0.15
        )

        risk = entry - sl

        if risk <= 0:
            risk = a

        tp1 = entry + (
            risk * 0.90
        )

        tp2 = entry + (
            risk * 1.60
        )

    elif direction == "SELL":

        swing = recent_high(
            m1,
            8
        )

        sl = swing + (
            a * 0.15
        )

        risk = sl - entry

        if risk <= 0:
            risk = a

        tp1 = entry - (
            risk * 0.90
        )

        tp2 = entry - (
            risk * 1.60
        )

    else:

        sl = None
        tp1 = None
        tp2 = None

    return {
        "direction": direction,
        "score": score,
        "buy_score": buy,
        "sell_score": sell,
        "status": status,
        "setup": setup,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "atr": a,
        "m5": m5_structure,
        "m15": m15_structure,
        "sweep": sw,
        "rejection": rej,
        "engulfing": eng,
        "displacement": disp,
        "bos": bos,
        "breakout": brk,
        "buy_reasons": buy_reasons,
        "sell_reasons": sell_reasons,
        "candle_time": last["datetime"]
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_analysis(result):

    if result is None:

        return (
            "⚠️ DATA INCOMPLETE\n\n"
            "M1/M5/M15 candle data is insufficient."
        )

    direction = result["direction"]
    score = result["score"]
    status = result["status"]

    if direction == "NONE":

        return (
            "⏳ XAUUSD CANDLE SCALPER\n\n"
            "STATUS: WAIT\n"
            f"BUY SCORE: {result['buy_score']}\n"
            f"SELL SCORE: {result['sell_score']}\n"
            f"M5: {result['m5']}\n"
            f"M15: {result['m15']}\n"
            f"M1: {result['candle_time']}\n\n"
            "No clear directional edge yet."
        )

    if direction == "BUY":
        emoji = "🟢"
        side = "BUY"
        reasons = result["buy_reasons"]

    else:
        emoji = "🔴"
        side = "SELL"
        reasons = result["sell_reasons"]

    reason_text = ", ".join(
        reasons[:6]
    )

    text = (
        f"{emoji} XAUUSD {side} SCALP\n\n"
        f"STATUS: {status}\n"
        f"SCORE: {score}\n"
        f"SETUP: {result['setup']}\n\n"
        f"ENTRY: {result['entry']:.2f}\n"
        f"SL: {result['sl']:.2f}\n"
        f"TP1: {result['tp1']:.2f}\n"
        f"TP2: {result['tp2']:.2f}\n\n"
        f"LOT: {LOT_SIZE}\n\n"
        f"M5: {result['m5']}\n"
        f"M15: {result['m15']}\n"
        f"REASONS: {reason_text}\n\n"
        f"CANDLE: {result['candle_time']}"
    )

    return text


# ============================================================
# FETCH + ANALYZE
# ============================================================

def get_analysis():

    m1 = get_candles("1min")
    m5 = get_candles("5min")
    m15 = get_candles("15min")

    logging.info(
        "DATA CHECK | M1=%d | M5=%d | M15=%d",
        len(m1),
        len(m5),
        len(m15)
    )

    if not m1 or not m5 or not m15:

        return None

    return analyze(
        m1,
        m5,
        m15
    )


# ============================================================
# SETUP ID
# ============================================================

def setup_id(result):

    if result is None:
        return None

    raw = (
        f"{result['direction']}|"
        f"{result['setup']}|"
        f"{result['candle_time']}"
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()[:16]


# ============================================================
# MANUAL SIGNAL
# ============================================================

def send_manual_signal(chat_id):

    result = get_analysis()

    if result is None:

        send_telegram(
            "⚠️ DATA INCOMPLETE\n\n"
            "Could not get complete M1/M5/M15 data.",
            chat_id
        )

        return

    message = format_analysis(result)

    send_telegram(
        message,
        chat_id
    )


# ============================================================
# AUTO SCANNER
# ============================================================

def scanner_loop():

    global LAST_SIGNAL_ID
    global LAST_WATCH_ID

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
        "WATCH SCORE: %s",
        WATCH_SCORE
    )

    logging.info(
        "ENTRY SCORE: %s",
        ENTRY_SCORE
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

            result = get_analysis()

            if result is None:

                logging.warning(
                    "SCAN RESULT | DATA INCOMPLETE"
                )

            else:

                logging.info(
                    "SCAN RESULT | status=%s | "
                    "direction=%s | score=%s | "
                    "BUY=%s | SELL=%s | setup=%s",
                    result["status"],
                    result["direction"],
                    result["score"],
                    result["buy_score"],
                    result["sell_score"],
                    result["setup"]
                )

                sid = setup_id(result)

                # ------------------------------------------------
                # ENTRY SIGNAL
                # ------------------------------------------------

                if (
                    result["status"] == "SIGNAL"
                    and sid != LAST_SIGNAL_ID
                ):

                    message = format_analysis(
                        result
                    )

                    if send_telegram(message):

                        LAST_SIGNAL_ID = sid

                        logging.info(
                            "ENTRY ALERT SENT | %s | %s",
                            result["direction"],
                            result["score"]
                        )

                # ------------------------------------------------
                # WATCH ALERT
                # ------------------------------------------------

                elif (
                    result["status"] == "WATCH"
                    and sid != LAST_WATCH_ID
                ):

                    message = format_analysis(
                        result
                    )

                    message = (
                        "👀 WATCH SETUP\n\n"
                        + message
                    )

                    if send_telegram(message):

                        LAST_WATCH_ID = sid

                        logging.info(
                            "WATCH ALERT SENT | %s | %s",
                            result["direction"],
                            result["score"]
                        )

        except Exception as e:

            logging.exception(
                "Scanner error: %s",
                e
            )

        time.sleep(
            SCAN_SECONDS
        )


# ============================================================
# DEBUG
# ============================================================

def debug_data():

    m1 = get_candles("1min")
    m5 = get_candles("5min")
    m15 = get_candles("15min")

    lines = []

    lines.append("🔧 XAUUSD DEBUG")
    lines.append("")

    lines.append(
        f"M1 candles: {len(m1)}"
    )

    if m1:
        c = m1[-1]

        lines.append(
            f"M1: {c['datetime']} "
            f"O {c['open']:.2f} "
            f"H {c['high']:.2f} "
            f"L {c['low']:.2f} "
            f"C {c['close']:.2f}"
        )

    lines.append("")

    lines.append(
        f"M5 candles: {len(m5)}"
    )

    if m5:
        c = m5[-1]

        lines.append(
            f"M5: {c['datetime']} "
            f"O {c['open']:.2f} "
            f"H {c['high']:.2f} "
            f"L {c['low']:.2f} "
            f"C {c['close']:.2f}"
        )

    lines.append("")

    lines.append(
        f"M15 candles: {len(m15)}"
    )

    if m15:
        c = m15[-1]

        lines.append(
            f"M15: {c['datetime']} "
            f"O {c['open']:.2f} "
            f"H {c['high']:.2f} "
            f"L {c['low']:.2f} "
            f"C {c['close']:.2f}"
        )

    if m1 and m5 and m15:

        result = analyze(
            m1,
            m5,
            m15
        )

        if result:

            lines.append("")
            lines.append(
                f"STATUS: {result['status']}"
            )

            lines.append(
                f"DIRECTION: {result['direction']}"
            )

            lines.append(
                f"SCORE: {result['score']}"
            )

            lines.append(
                f"BUY SCORE: {result['buy_score']}"
            )

            lines.append(
                f"SELL SCORE: {result['sell_score']}"
            )

            lines.append(
                f"SETUP: {result['setup']}"
            )

            lines.append(
                f"SWEEP: {result['sweep']}"
            )

            lines.append(
                f"REJECTION: {result['rejection']}"
            )

            lines.append(
                f"ENGULFING: {result['engulfing']}"
            )

            lines.append(
                f"DISPLACEMENT: {result['displacement']}"
            )

            lines.append(
                f"BOS: {result['bos']}"
            )

            lines.append(
                f"BREAKOUT: {result['breakout']}"
            )

    return "\n".join(lines)


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    try:

        update = request.get_json(
            silent=True
        )

        if not update:
            return "OK"

        message = update.get(
            "message"
        )

        if not message:
            return "OK"

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        text = (
            message.get(
                "text",
                ""
            )
            .strip()
            .lower()
        )

        if text.startswith(
            "/signal"
        ):

            send_manual_signal(
                chat_id
            )

        elif text.startswith(
            "/debug"
        ):

            send_telegram(
                debug_data(),
                chat_id
            )

        elif text.startswith(
            "/status"
        ):

            send_telegram(
                "🟢 XAUUSD CANDLE BOT\n\n"
                "Continuous scanning: ON\n"
                "No time filter\n"
                f"Scan: every {SCAN_SECONDS}s\n"
                f"Watch score: {WATCH_SCORE}\n"
                f"Entry score: {ENTRY_SCORE}\n"
                f"Lot: {LOT_SIZE}",
                chat_id
            )

        else:

            send_telegram(
                "Commands:\n\n"
                "/signal - current scalp analysis\n"
                "/debug - data/debug information\n"
                "/status - bot status",
                chat_id
            )

        return "OK"

    except Exception as e:

        logging.exception(
            "Webhook error: %s",
            e
        )

        return "OK"


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/")
def home():

    return (
        "XAUUSD Candle Expert Bot is running."
    )


@app.route("/health")
def health():

    return {
        "status": "running",
        "symbol": SYMBOL,
        "scan_seconds": SCAN_SECONDS,
        "watch_score": WATCH_SCORE,
        "entry_score": ENTRY_SCORE,
        "lot": LOT_SIZE
    }


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    print(
        "========================================"
    )

    print(
        "XAUUSD CANDLE EXPERT BOT STARTING"
    )

    print(
        "NO TIME FILTER"
    )

    print(
        "CONTINUOUS SCANNING: ON"
    )

    print(
        f"SCAN INTERVAL: {SCAN_SECONDS} seconds"
    )

    print(
        f"WATCH SCORE: {WATCH_SCORE}"
    )

    print(
        f"ENTRY SCORE: {ENTRY_SCORE}"
    )

    print(
        f"LOT: {LOT_SIZE}"
    )

    print(
        "========================================"
    )

    scanner = threading.Thread(
        target=scanner_loop,
        daemon=True
    )

    scanner.start()

    app.run(
        host="0.0.0.0",
        port=port
    )
