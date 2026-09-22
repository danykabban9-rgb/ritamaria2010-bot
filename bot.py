import os
import time
import threading
import logging
import hashlib

import requests
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

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).rstrip("/")

LAST_AUTO_SETUP_ID = None
LAST_MANUAL_SETUP_ID = None

TD_URL = "https://api.twelvedata.com/time_series"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

app = Flask(__name__)


# ============================================================
# TELEGRAM
# ============================================================

def tg_send(text):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error(
            "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID"
        )
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not r.ok:
            logging.error(
                "Telegram error: %s",
                r.text[:500]
            )
            return False

        return True

    except Exception as e:
        logging.exception(
            "Telegram send failed: %s",
            e
        )
        return False


def set_webhook():

    if not TELEGRAM_TOKEN:
        return

    if not RENDER_EXTERNAL_URL:
        logging.warning(
            "RENDER_EXTERNAL_URL not available."
        )
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/setWebhook"
    )

    webhook = (
        f"{RENDER_EXTERNAL_URL}/telegram"
    )

    try:

        r = requests.post(
            url,
            json={"url": webhook},
            timeout=15,
        )

        logging.info(
            "Webhook result: %s",
            r.text[:500]
        )

    except Exception as e:

        logging.exception(
            "Webhook setup failed: %s",
            e
        )


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(interval, size=220):

    if not TWELVE_DATA_KEY:
        raise RuntimeError(
            "Missing TWELVE_DATA_KEY"
        )

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": size,
        "apikey": TWELVE_DATA_KEY,
        "order": "ASC",
        "timezone": "UTC",
    }

    r = requests.get(
        TD_URL,
        params=params,
        timeout=20,
    )

    data = r.json()

    if not r.ok:
        raise RuntimeError(
            f"Twelve Data HTTP "
            f"{r.status_code}: {data}"
        )

    if data.get("status") == "error":
        raise RuntimeError(
            data.get(
                "message",
                str(data)
            )
        )

    values = data.get("values")

    if not values or len(values) < 60:
        raise RuntimeError(
            f"Not enough {interval} candles"
        )

    candles = []

    for x in values:

        try:

            candles.append({
                "datetime": x["datetime"],
                "open": float(x["open"]),
                "high": float(x["high"]),
                "low": float(x["low"]),
                "close": float(x["close"]),
            })

        except (
            KeyError,
            TypeError,
            ValueError
        ):
            continue

    if len(candles) < 60:
        raise RuntimeError(
            f"Invalid {interval} candle data"
        )

    # Ignore the newest candle because it
    # can still be forming.
    closed = candles[:-1]

    if len(closed) < 60:
        raise RuntimeError(
            f"Not enough closed {interval} candles"
        )

    return closed


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if len(values) < period:
        return None

    seed = sum(
        values[:period]
    ) / period

    result = [None] * (period - 1)
    result.append(seed)

    alpha = 2.0 / (period + 1.0)

    previous = seed

    for price in values[period:]:

        previous = (
            price * alpha
            + previous * (1 - alpha)
        )

        result.append(previous)

    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period=14):

    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, 0.0)
        )

        losses.append(
            max(-change, 0.0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    result = [None] * period

    if avg_loss == 0:

        result.append(100.0)

    else:

        rs = avg_gain / avg_loss

        result.append(
            100.0
            - (
                100.0
                / (1.0 + rs)
            )
        )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[i]
        ) / period

        if avg_loss == 0:

            result.append(100.0)

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            result.append(
                100.0
                - (
                    100.0
                    / (1.0 + rs)
                )
            )

    return result


# ============================================================
# ATR
# ============================================================

def atr(candles, period=14):

    if len(candles) <= period:
        return None

    trs = []

    for i, c in enumerate(candles):

        if i == 0:

            tr = (
                c["high"]
                - c["low"]
            )

        else:

            previous_close = (
                candles[i - 1]["close"]
            )

            tr = max(
                c["high"] - c["low"],
                abs(
                    c["high"]
                    - previous_close
                ),
                abs(
                    c["low"]
                    - previous_close
                ),
            )

        trs.append(tr)

    value = (
        sum(trs[:period])
        / period
    )

    for tr in trs[period:]:

        value = (
            (
                value
                * (period - 1)
            )
            + tr
        ) / period

    return value


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def body(c):
    return abs(
        c["close"]
        - c["open"]
    )


def candle_range(c):

    return max(
        c["high"]
        - c["low"],
        0.00001
    )


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


# ============================================================
# ENGULFING
# ============================================================

def bullish_engulfing(prev, cur):

    return (
        bearish(prev)
        and bullish(cur)
        and cur["open"]
        <= prev["close"]
        and cur["close"]
        >= prev["open"]
        and body(cur)
        > body(prev) * 1.05
    )


def bearish_engulfing(prev, cur):

    return (
        bullish(prev)
        and bearish(cur)
        and cur["open"]
        >= prev["close"]
        and cur["close"]
        <= prev["open"]
        and body(cur)
        > body(prev) * 1.05
    )


# ============================================================
# REJECTION CANDLES
# ============================================================

def rejection_bull(c):

    lower = (
        min(c["open"], c["close"])
        - c["low"]
    )

    upper = (
        c["high"]
        - max(c["open"], c["close"])
    )

    return (
        lower > body(c) * 1.5
        and lower > upper * 1.25
        and c["close"]
        > c["low"]
        + candle_range(c) * 0.55
    )


def rejection_bear(c):

    upper = (
        c["high"]
        - max(c["open"], c["close"])
    )

    lower = (
        min(c["open"], c["close"])
        - c["low"]
    )

    return (
        upper > body(c) * 1.5
        and upper > lower * 1.25
        and c["close"]
        < c["low"]
        + candle_range(c) * 0.45
    )


# ============================================================
# DISPLACEMENT
# ============================================================

def average_body(candles, n=20):

    sample = candles[-n:]

    return (
        sum(body(c) for c in sample)
        / max(len(sample), 1)
    )


def displacement_bull(
    c,
    average
):

    return (
        bullish(c)
        and body(c)
        > average * 1.6
        and c["close"]
        >= c["high"]
        - candle_range(c) * 0.20
    )


def displacement_bear(
    c,
    average
):

    return (
        bearish(c)
        and body(c)
        > average * 1.6
        and c["close"]
        <= c["low"]
        + candle_range(c) * 0.20
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def recent_swing_high(
    candles,
    lookback=20
):

    if len(candles) < lookback + 2:
        return None

    return max(
        c["high"]
        for c in candles[
            -lookback - 1:-1
        ]
    )


def recent_swing_low(
    candles,
    lookback=20
):

    if len(candles) < lookback + 2:
        return None

    return min(
        c["low"]
        for c in candles[
            -lookback - 1:-1
        ]
    )


# ============================================================
# MAIN ANALYZER
# ============================================================

def analyze():

    m15 = get_candles(
        "15min",
        180
    )

    m5 = get_candles(
        "5min",
        220
    )

    m1 = get_candles(
        "1min",
        180
    )

    close15 = [
        x["close"]
        for x in m15
    ]

    close5 = [
        x["close"]
        for x in m5
    ]

    ema15_fast = ema(
        close15,
        20
    )

    ema15_slow = ema(
        close15,
        50
    )

    ema5_fast = ema(
        close5,
        20
    )

    ema5_slow = ema(
        close5,
        50
    )

    rsi5 = rsi(
        close5,
        14
    )

    atr5 = atr(
        m5,
        14
    )

    if not all([
        ema15_fast,
        ema15_slow,
        ema5_fast,
        ema5_slow,
        rsi5,
        atr5,
    ]):

        raise RuntimeError(
            "Indicator calculation failed"
        )

    last15 = m15[-1]

    last5 = m5[-1]
    prev5 = m5[-2]

    last1 = m1[-1]
    prev1 = m1[-2]

    buy_score = 0
    sell_score = 0

    buy_reasons = []
    sell_reasons = []


    # ========================================================
    # M15 TREND — 25 POINTS
    # ========================================================

    if (
        ema15_fast[-1]
        > ema15_slow[-1]
        and last15["close"]
        > ema15_fast[-1]
    ):

        buy_score += 25

        buy_reasons.append(
            "M15 bullish trend"
        )


    if (
        ema15_fast[-1]
        < ema15_slow[-1]
        and last15["close"]
        < ema15_fast[-1]
    ):

        sell_score += 25

        sell_reasons.append(
            "M15 bearish trend"
        )


    # ========================================================
    # M5 TREND — 20 POINTS
    # ========================================================

    if (
        ema5_fast[-1]
        > ema5_slow[-1]
        and last5["close"]
        > ema5_fast[-1]
    ):

        buy_score += 20

        buy_reasons.append(
            "M5 EMA trend"
        )


    if (
        ema5_fast[-1]
        < ema5_slow[-1]
        and last5["close"]
        < ema5_fast[-1]
    ):

        sell_score += 20

        sell_reasons.append(
            "M5 EMA trend"
        )


    # ========================================================
    # BREAK OF STRUCTURE — 15 POINTS
    # ========================================================

    swing_high = recent_swing_high(
        m5,
        20
    )

    swing_low = recent_swing_low(
        m5,
        20
    )


    if (
        swing_high is not None
        and last5["close"]
        > swing_high
    ):

        buy_score += 15

        buy_reasons.append(
            "M5 bullish BOS"
        )


    if (
        swing_low is not None
        and last5["close"]
        < swing_low
    ):

        sell_score += 15

        sell_reasons.append(
            "M5 bearish BOS"
        )


    # ========================================================
    # LIQUIDITY SWEEP — 15 POINTS
    # ========================================================

    previous_high = max(
        c["high"]
        for c in m5[-11:-1]
    )

    previous_low = min(
        c["low"]
        for c in m5[-11:-1]
    )


    bullish_sweep = (
        last5["low"]
        < previous_low
        and last5["close"]
        > previous_low
    )


    bearish_sweep = (
        last5["high"]
        > previous_high
        and last5["close"]
        < previous_high
    )


    if bullish_sweep:

        buy_score += 15

        buy_reasons.append(
            "Sell-side liquidity sweep"
        )


    if bearish_sweep:

        sell_score += 15

        sell_reasons.append(
            "Buy-side liquidity sweep"
        )


    # ========================================================
    # M1 CANDLE CONFIRMATION — 15 POINTS
    # ========================================================

    avg1 = average_body(
        m1,
        20
    )


    bullish_pattern = (
        bullish_engulfing(
            prev1,
            last1
        )
        or rejection_bull(last1)
        or displacement_bull(
            last1,
            avg1
        )
    )


    bearish_pattern = (
        bearish_engulfing(
            prev1,
            last1
        )
        or rejection_bear(last1)
        or displacement_bear(
            last1,
            avg1
        )
    )


    if bullish_pattern:

        buy_score += 15

        buy_reasons.append(
            "M1 bullish candle confirmation"
        )


    if bearish_pattern:

        sell_score += 15

        sell_reasons.append(
            "M1 bearish candle confirmation"
        )


    # ========================================================
    # RSI — 10 POINTS
    # ========================================================

    current_rsi = rsi5[-1]


    if 50 <= current_rsi <= 68:

        buy_score += 10

        buy_reasons.append(
            f"RSI {current_rsi:.1f} bullish zone"
        )


    if 32 <= current_rsi <= 50:

        sell_score += 10

        sell_reasons.append(
            f"RSI {current_rsi:.1f} bearish zone"
        )


    # Avoid chasing extreme RSI

    if current_rsi > 72:
        buy_score -= 10

    if current_rsi < 28:
        sell_score -= 10


    # ========================================================
    # SELECT SIGNAL
    # ========================================================

    if (
        buy_score >= MIN_SCORE
        and buy_score > sell_score
    ):

        direction = "BUY"
        score = buy_score
        reasons = buy_reasons


    elif (
        sell_score >= MIN_SCORE
        and sell_score > buy_score
    ):

        direction = "SELL"
        score = sell_score
        reasons = sell_reasons


    else:

        return {
            "signal": False,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "rsi": current_rsi,
            "price": last1["close"],
            "time": last1["datetime"],
        }


    # ========================================================
    # SL / TP
    # ========================================================

    price = last1["close"]


    if direction == "BUY":

        structure_sl = min(
            min(
                c["low"]
                for c in m5[-8:]
            ),
            last5["low"]
        )

        atr_sl = (
            price
            - atr5 * 1.20
        )

        sl = min(
            structure_sl,
            atr_sl
        )

        risk = price - sl


        if (
            risk <= 0
            or risk > atr5 * 2.8
        ):

            return {
                "signal": False,
                "reason": "Stop too wide",
                "buy_score": buy_score,
                "sell_score": sell_score,
            }


        tp1 = price + risk * 0.8
        tp2 = price + risk * 1.3
        tp3 = price + risk * 1.8
        tp4 = price + risk * 2.4
        tp5 = price + risk * 3.2


    else:

        structure_sl = max(
            max(
                c["high"]
                for c in m5[-8:]
            ),
            last5["high"]
        )

        atr_sl = (
            price
            + atr5 * 1.20
        )

        sl = max(
            structure_sl,
            atr_sl
        )

        risk = sl - price


        if (
            risk <= 0
            or risk > atr5 * 2.8
        ):

            return {
                "signal": False,
                "reason": "Stop too wide",
                "buy_score": buy_score,
                "sell_score": sell_score,
            }


        tp1 = price - risk * 0.8
        tp2 = price - risk * 1.3
        tp3 = price - risk * 1.8
        tp4 = price - risk * 2.4
        tp5 = price - risk * 3.2


    # ========================================================
    # ENTRY ZONE
    # ========================================================

    zone = atr5 * 0.8

    entry_low = (
        price
        - zone * 0.35
    )

    entry_high = (
        price
        + zone * 0.35
    )


    # ========================================================
    # UNIQUE SETUP ID
    # ========================================================

    raw_id = (
        f"{direction}|"
        f"{last5['datetime']}|"
        f"{last1['datetime']}|"
        f"{round(price, 1)}|"
        f"{round(sl, 1)}|"
        f"{round(tp1, 1)}"
    )

    setup_id = hashlib.sha256(
        raw_id.encode()
    ).hexdigest()[:16]


    return {

        "signal": True,

        "direction": direction,

        "score": score,

        "price": price,

        "entry_low": entry_low,

        "entry_high": entry_high,

        "sl": sl,

        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp4": tp4,
        "tp5": tp5,

        "rsi": current_rsi,

        "atr": atr5,

        "reasons": reasons,

        "setup_id": setup_id,

        "candle_time": last1["datetime"],
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def fmt_price(x):
    return f"{x:.2f}"


def format_signal(a):

    direction = a["direction"]

    emoji = (
        "🟢"
        if direction == "BUY"
        else "🔴"
    )

    reasons = "\n".join(
        f"• {x}"
        for x in a["reasons"]
    )

    return (
        f"{emoji} "
        f"<b>XAUUSD {direction} SIGNAL</b>\n\n"

        f"<b>ENTRY:</b> "
        f"{fmt_price(a['entry_low'])}"
        f" – "
        f"{fmt_price(a['entry_high'])}\n"

        f"<b>SL:</b> "
        f"{fmt_price(a['sl'])}\n\n"

        f"<b>TP1:</b> "
        f"{fmt_price(a['tp1'])}\n"

        f"<b>TP2:</b> "
        f"{fmt_price(a['tp2'])}\n"

        f"<b>TP3:</b> "
        f"{fmt_price(a['tp3'])}\n"

        f"<b>TP4:</b> "
        f"{fmt_price(a['tp4'])}\n"

        f"<b>TP5:</b> "
        f"{fmt_price(a['tp5'])}\n\n"

        f"<b>SCORE:</b> "
        f"{a['score']}/100\n"

        f"<b>RSI:</b> "
        f"{a['rsi']:.1f}\n"

        f"<b>ATR:</b> "
        f"{a['atr']:.2f}\n\n"

        f"<b>CONFIRMATION:</b>\n"
        f"{reasons}\n\n"

        f"LOT: <b>{LOT_SIZE}</b>\n"

        f"⚠️ Manual execution. "
        f"No automatic trading."
    )


# ============================================================
# SCAN
# ============================================================

def scan_once(manual=False):

    global LAST_AUTO_SETUP_ID
    global LAST_MANUAL_SETUP_ID

    try:

        result = analyze()


        if not result.get("signal"):

            logging.info(
                "No signal | "
                "BUY=%s SELL=%s",
                result.get("buy_score"),
                result.get("sell_score")
            )

            if manual:

                tg_send(
                    "⚪ <b>No valid setup now.</b>\n\n"
                    f"BUY score: "
                    f"{result.get('buy_score', 0)}/100\n"
                    f"SELL score: "
                    f"{result.get('sell_score', 0)}/100\n"
                    f"RSI: "
                    f"{result.get('rsi', 0):.1f}\n\n"
                    f"Minimum required: "
                    f"{MIN_SCORE}/100"
                )

            return result


        setup_id = result["setup_id"]


        if manual:

            if setup_id == LAST_MANUAL_SETUP_ID:
                return result

            LAST_MANUAL_SETUP_ID = setup_id

            tg_send(
                format_signal(result)
            )


        else:

            if setup_id == LAST_AUTO_SETUP_ID:
                return result

            LAST_AUTO_SETUP_ID = setup_id

            tg_send(
                format_signal(result)
            )


        logging.info(
            "%s SIGNAL | score=%s | price=%.2f",
            result["direction"],
            result["score"],
            result["price"]
        )

        return result


    except Exception as e:

        logging.exception(
            "Scanner error"
        )

        if manual:

            tg_send(
                "⚠️ <b>Scanner error</b>\n\n"
                f"<code>{str(e)[:700]}</code>"
            )

        return None


# ============================================================
# AUTO SCANNER
# ============================================================

def scanner_loop():

    time.sleep(8)

    while True:

        try:

            scan_once(
                manual=False
            )

        except Exception:

            logging.exception(
                "Auto scanner error"
            )

        time.sleep(
            SCAN_SECONDS
        )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/telegram")
def telegram_webhook():

    update = (
        request
        .get_json(
            silent=True
        )
        or {}
    )

    message = update.get(
        "message",
        {}
    )

    text = (
        message
        .get("text")
        or ""
    ).strip()


    if text.startswith("/start"):

        tg_send(
            "🟡 "
            "<b>XAUUSD Candle + "
            "Structure Scalper</b>\n\n"

            "Commands:\n"
            "/signal — scan now\n"
            "/status — bot status"
        )


    elif text.startswith("/signal"):

        threading.Thread(
            target=scan_once,
            kwargs={
                "manual": True
            },
            daemon=True,
        ).start()


    elif text.startswith("/status"):

        tg_send(
            "🟢 <b>BOT ONLINE</b>\n\n"
            f"Symbol: {SYMBOL}\n"
            f"Scan: every "
            f"{SCAN_SECONDS}s\n"
            f"Minimum score: "
            f"{MIN_SCORE}/100\n"
            f"Lot: {LOT_SIZE}"
        )


    return jsonify({
        "ok": True
    })


# ============================================================
# RENDER HEALTH
# ============================================================

@app.get("/")
def home():

    return (
        "XAUUSD candle scalper "
        "is running."
    )


@app.get("/health")
def health():

    return jsonify({

        "ok": True,

        "symbol": SYMBOL,

        "scan_seconds":
            SCAN_SECONDS,

        "min_score":
            MIN_SCORE,
    })


# ============================================================
# START
# ============================================================

def startup():

    logging.info(
        "Starting XAUUSD "
        "candle + structure scalper..."
    )

    set_webhook()

    thread = threading.Thread(
        target=scanner_loop,
        daemon=True,
    )

    thread.start()


startup()


if __name__ == "__main__":

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
