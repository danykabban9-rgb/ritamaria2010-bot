import os
import time
import threading
import logging
import requests
import hashlib

from flask import Flask, request
from datetime import datetime
import pytz


# ============================================================
# EXISTING RENDER VARIABLES
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

BEIRUT_TZ = pytz.timezone("Asia/Beirut")

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


# ============================================================
# GLOBAL STATE
# ============================================================

LAST_AUTO_SETUP_ID = None
LAST_AUTO_SIGNAL = None
LAST_AUTO_ALERT_TIME = 0

# Used to prevent duplicate manual alerts
LAST_MANUAL_SETUP_ID = None


# ============================================================
# TELEGRAM
# ============================================================

def send_message(chat_id, text):

    try:

        if not TELEGRAM_TOKEN:
            logging.error("TELEGRAM_TOKEN missing")
            return False

        if not chat_id:
            logging.error("Telegram chat ID missing")
            return False

        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/sendMessage"
        )

        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=15
        )

        if response.status_code != 200:
            logging.error(
                f"Telegram error: {response.text}"
            )
            return False

        return True

    except Exception as e:

        logging.error(
            f"Telegram send error: {e}"
        )

        return False


# ============================================================
# TRADING HOURS
# ============================================================

def is_trading_window():

    now = datetime.now(BEIRUT_TZ)
    h = now.hour

    # Main active periods
    if 9 <= h < 12:
        return True

    if 14 <= h < 19:
        return True

    return False


# ============================================================
# GET XAU DATA
# ============================================================

def get_xau_data(interval, outputsize=150):

    try:

        if not TWELVE_DATA_KEY:
            logging.error("TWELVE_DATA_KEY missing")
            return None

        url = (
            "https://api.twelvedata.com/time_series"
            f"?symbol=XAU/USD"
            f"&interval={interval}"
            f"&apikey={TWELVE_DATA_KEY}"
            f"&outputsize={outputsize}"
            f"&order=ASC"
        )

        response = requests.get(
            url,
            timeout=15
        )

        data = response.json()

        if "values" not in data:

            logging.error(
                f"Twelve Data error: {data}"
            )

            return None

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
            f"Market data error: {e}"
        )

        return None


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def candle_parts(c):

    body = abs(
        c["close"] - c["open"]
    )

    upper = (
        c["high"]
        - max(c["open"], c["close"])
    )

    lower = (
        min(c["open"], c["close"])
        - c["low"]
    )

    total = c["high"] - c["low"]

    return body, upper, lower, total


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def bullish_rejection(c):

    body, upper, lower, total = candle_parts(c)

    if total <= 0:
        return False

    return (
        lower >= max(body * 1.5, 0.01)
        and lower > upper
        and c["close"] >
        c["low"] + total * 0.55
    )


def bearish_rejection(c):

    body, upper, lower, total = candle_parts(c)

    if total <= 0:
        return False

    return (
        upper >= max(body * 1.5, 0.01)
        and upper > lower
        and c["close"] <
        c["high"] - total * 0.55
    )


def bullish_engulfing(prev, c):

    return (
        bearish(prev)
        and bullish(c)
        and c["open"] <= prev["close"]
        and c["close"] >= prev["open"]
        and abs(c["close"] - c["open"])
        > abs(prev["close"] - prev["open"])
    )


def bearish_engulfing(prev, c):

    return (
        bullish(prev)
        and bearish(c)
        and c["open"] >= prev["close"]
        and c["close"] <= prev["open"]
        and abs(c["close"] - c["open"])
        > abs(prev["close"] - prev["open"])
    )


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    return (
        sum(true_ranges[-period:])
        / period
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(candles):

    if len(candles) < 20:
        return "NEUTRAL"

    old = candles[-16:-8]
    new = candles[-8:]

    old_high = max(
        x["high"] for x in old
    )

    old_low = min(
        x["low"] for x in old
    )

    new_high = max(
        x["high"] for x in new
    )

    new_low = min(
        x["low"] for x in new
    )

    if (
        new_high > old_high
        and new_low > old_low
    ):
        return "BULLISH"

    if (
        new_high < old_high
        and new_low < old_low
    ):
        return "BEARISH"

    last = candles[-1]

    if last["close"] > old_high:
        return "BULLISH"

    if last["close"] < old_low:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# LIQUIDITY SWEEPS
# ============================================================

def bullish_liquidity_sweep(candles):

    if len(candles) < 12:
        return False

    current = candles[-1]

    previous_low = min(
        x["low"]
        for x in candles[-11:-1]
    )

    return (
        current["low"] < previous_low
        and current["close"] > previous_low
    )


def bearish_liquidity_sweep(candles):

    if len(candles) < 12:
        return False

    current = candles[-1]

    previous_high = max(
        x["high"]
        for x in candles[-11:-1]
    )

    return (
        current["high"] > previous_high
        and current["close"] < previous_high
    )


# ============================================================
# BREAK OF STRUCTURE
# ============================================================

def bullish_bos(candles):

    if len(candles) < 12:
        return False

    current = candles[-1]

    previous_high = max(
        x["high"]
        for x in candles[-11:-1]
    )

    return current["close"] > previous_high


def bearish_bos(candles):

    if len(candles) < 12:
        return False

    current = candles[-1]

    previous_low = min(
        x["low"]
        for x in candles[-11:-1]
    )

    return current["close"] < previous_low


# ============================================================
# DISPLACEMENT
# ============================================================

def bullish_displacement(candles):

    if len(candles) < 6:
        return False

    current = candles[-1]

    body, upper, lower, total = candle_parts(
        current
    )

    if total <= 0:
        return False

    previous_bodies = []

    for c in candles[-6:-1]:

        b, _, _, _ = candle_parts(c)

        previous_bodies.append(b)

    average_body = (
        sum(previous_bodies)
        / len(previous_bodies)
    )

    return (
        bullish(current)
        and body > average_body * 1.5
        and body / total > 0.55
    )


def bearish_displacement(candles):

    if len(candles) < 6:
        return False

    current = candles[-1]

    body, upper, lower, total = candle_parts(
        current
    )

    if total <= 0:
        return False

    previous_bodies = []

    for c in candles[-6:-1]:

        b, _, _, _ = candle_parts(c)

        previous_bodies.append(b)

    average_body = (
        sum(previous_bodies)
        / len(previous_bodies)
    )

    return (
        bearish(current)
        and body > average_body * 1.5
        and body / total > 0.55
    )


# ============================================================
# SETUP ID
# ============================================================

def make_setup_id(m1, m5, m15):

    # IMPORTANT:
    # Use the latest CLOSED candle.
    # This means a new setup gets a new ID.

    m1c = m1[-2]
    m5c = m5[-2]
    m15c = m15[-2]

    raw = (
        f"{m1c['datetime']}|"
        f"{m1c['open']}|"
        f"{m1c['high']}|"
        f"{m1c['low']}|"
        f"{m1c['close']}|"
        f"{m5c['datetime']}|"
        f"{m5c['close']}|"
        f"{m15c['datetime']}|"
        f"{m15c['close']}"
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()[:12]


# ============================================================
# MAIN ANALYSIS
# ============================================================

def analyze_pro(m1, m5, m15):

    if (
        not m1
        or not m5
        or not m15
        or len(m1) < 40
        or len(m5) < 40
        or len(m15) < 40
    ):
        return None

    # --------------------------------------------------------
    # REMOVE CURRENT FORMING CANDLE
    # --------------------------------------------------------

    m1c = m1[:-1]
    m5c = m5[:-1]
    m15c = m15[:-1]

    current_m1 = m1c[-1]

    entry = current_m1["close"]

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    trend15 = market_structure(m15c)
    trend5 = market_structure(m5c)

    # --------------------------------------------------------
    # M5 LIQUIDITY / BOS
    # --------------------------------------------------------

    buy_sweep = bullish_liquidity_sweep(m5c)
    sell_sweep = bearish_liquidity_sweep(m5c)

    buy_bos = bullish_bos(m5c)
    sell_bos = bearish_bos(m5c)

    # --------------------------------------------------------
    # M1 CONFIRMATION
    # --------------------------------------------------------

    m1_prev = m1c[-2]
    m1_last = m1c[-1]

    buy_rejection = bullish_rejection(
        m1_last
    )

    sell_rejection = bearish_rejection(
        m1_last
    )

    buy_engulfing = bullish_engulfing(
        m1_prev,
        m1_last
    )

    sell_engulfing = bearish_engulfing(
        m1_prev,
        m1_last
    )

    buy_displacement = bullish_displacement(
        m1c
    )

    sell_displacement = bearish_displacement(
        m1c
    )

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr = calculate_atr(
        m5c,
        14
    )

    if not atr or atr <= 0:
        return None

    # Abnormally large candle = don't chase
    latest_m5_range = (
        m5c[-1]["high"]
        - m5c[-1]["low"]
    )

    if latest_m5_range > atr * 2.8:

        return {
            "signal": "NO_TRADE",
            "score": 0,
            "price": entry,
            "reason": "Abnormal M5 volatility"
        }

    # ========================================================
    # BUY SCORE
    # ========================================================

    buy_score = 0
    buy_reasons = []

    if trend15 == "BULLISH":

        buy_score += 20
        buy_reasons.append(
            "M15 bullish structure"
        )

    if trend5 == "BULLISH":

        buy_score += 15
        buy_reasons.append(
            "M5 bullish structure"
        )

    if buy_sweep:

        buy_score += 20
        buy_reasons.append(
            "M5 sell-side liquidity sweep"
        )

    if buy_bos:

        buy_score += 15
        buy_reasons.append(
            "M5 bullish BOS"
        )

    if buy_rejection:

        buy_score += 10
        buy_reasons.append(
            "M1 bullish rejection"
        )

    if buy_engulfing:

        buy_score += 10
        buy_reasons.append(
            "M1 bullish engulfing"
        )

    if buy_displacement:

        buy_score += 10
        buy_reasons.append(
            "M1 bullish displacement"
        )

    # ========================================================
    # SELL SCORE
    # ========================================================

    sell_score = 0
    sell_reasons = []

    if trend15 == "BEARISH":

        sell_score += 20
        sell_reasons.append(
            "M15 bearish structure"
        )

    if trend5 == "BEARISH":

        sell_score += 15
        sell_reasons.append(
            "M5 bearish structure"
        )

    if sell_sweep:

        sell_score += 20
        sell_reasons.append(
            "M5 buy-side liquidity sweep"
        )

    if sell_bos:

        sell_score += 15
        sell_reasons.append(
            "M5 bearish BOS"
        )

    if sell_rejection:

        sell_score += 10
        sell_reasons.append(
            "M1 bearish rejection"
        )

    if sell_engulfing:

        sell_score += 10
        sell_reasons.append(
            "M1 bearish engulfing"
        )

    if sell_displacement:

        sell_score += 10
        sell_reasons.append(
            "M1 bearish displacement"
        )

    # ========================================================
    # DECISION
    # ========================================================

    if (
        buy_score < 80
        and sell_score < 80
    ):

        return {
            "signal": "NO_TRADE",
            "score": max(
                buy_score,
                sell_score
            ),
            "price": entry,
            "trend15": trend15,
            "trend5": trend5,
            "reason": (
                f"M15={trend15} | "
                f"M5={trend5} | "
                f"BUY={buy_score} "
                f"SELL={sell_score}"
            )
        }

    # ========================================================
    # BUY
    # ========================================================

    if (
        buy_score >= 80
        and buy_score > sell_score
    ):

        recent_low = min(
            x["low"]
            for x in m5c[-8:]
        )

        sl_distance = max(
            entry - recent_low + 0.30,
            atr * 0.9
        )

        sl = entry - sl_distance

        tp1 = (
            entry
            + sl_distance * 1.5
        )

        tp2 = (
            entry
            + sl_distance * 2.3
        )

        return {
            "signal": "BUY",
            "score": buy_score,
            "price": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "atr": atr,
            "trend15": trend15,
            "trend5": trend5,
            "reasons": buy_reasons,
            "candle_time": current_m1["datetime"]
        }

    # ========================================================
    # SELL
    # ========================================================

    if (
        sell_score >= 80
        and sell_score > buy_score
    ):

        recent_high = max(
            x["high"]
            for x in m5c[-8:]
        )

        sl_distance = max(
            recent_high - entry + 0.30,
            atr * 0.9
        )

        sl = entry + sl_distance

        tp1 = (
            entry
            - sl_distance * 1.5
        )

        tp2 = (
            entry
            - sl_distance * 2.3
        )

        return {
            "signal": "SELL",
            "score": sell_score,
            "price": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "atr": atr,
            "trend15": trend15,
            "trend5": trend5,
            "reasons": sell_reasons,
            "candle_time": current_m1["datetime"]
        }

    return {
        "signal": "NO_TRADE",
        "score": max(
            buy_score,
            sell_score
        ),
        "price": entry,
        "reason": "Conflicting structure"
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(result, status="NEW SETUP"):

    signal = result["signal"]

    emoji = (
        "🟢"
        if signal == "BUY"
        else "🔴"
    )

    reasons = "\n".join(
        f"✓ {x}"
        for x in result["reasons"]
    )

    return (
        f"{emoji} XAUUSD PRO SCALP "
        f"{signal}\n"
        f"🔥 Score: "
        f"{result['score']}/100\n"
        f"📌 {status}\n\n"

        f"🕐 Candle: "
        f"{result['candle_time']}\n\n"

        f"Entry: "
        f"${result['price']:.2f}\n"

        f"SL: "
        f"${result['sl']:.2f}\n"

        f"TP1: "
        f"${result['tp1']:.2f}\n"

        f"TP2: "
        f"${result['tp2']:.2f}\n\n"

        f"M15: "
        f"{result['trend15']}\n"

        f"M5: "
        f"{result['trend5']}\n\n"

        f"{reasons}\n\n"

        f"⚠️ LOT: 0.01 ONLY"
    )


# ============================================================
# MANUAL ANALYSIS
# ============================================================

def manual_signal(chat_id):

    global LAST_MANUAL_SETUP_ID

    logging.info(
        "Manual /signal requested"
    )

    m1 = get_xau_data(
        "1min",
        150
    )

    m5 = get_xau_data(
        "5min",
        150
    )

    m15 = get_xau_data(
        "15min",
        150
    )

    if (
        not m1
        or not m5
        or not m15
    ):

        send_message(
            chat_id,
            "⚠️ Market data unavailable. Try again."
        )

        return

    setup_id = make_setup_id(
        m1,
        m5,
        m15
    )

    result = analyze_pro(
        m1,
        m5,
        m15
    )

    if not result:

        send_message(
            chat_id,
            "⚠️ Analysis failed."
        )

        return

    # --------------------------------------------------------
    # NEW HIGH-CONFLUENCE SETUP
    # --------------------------------------------------------

    if result["signal"] in [
        "BUY",
        "SELL"
    ] and result["score"] >= 80:

        if setup_id == LAST_MANUAL_SETUP_ID:

            send_message(
                chat_id,
                (
                    "⚪ SAME SETUP\n\n"
                    f"Setup ID: {setup_id}\n"
                    f"Score: "
                    f"{result['score']}/100\n\n"
                    "No new candle-structure "
                    "confirmation yet."
                )
            )

            return

        LAST_MANUAL_SETUP_ID = setup_id

        send_message(
            chat_id,
            format_signal(
                result,
                "NEW SETUP"
            )
        )

        return

    # --------------------------------------------------------
    # NO TRADE
    # --------------------------------------------------------

    send_message(
        chat_id,
        (
            "⚪ NO TRADE\n\n"
            f"Setup ID: {setup_id}\n"
            f"Score: "
            f"{result.get('score', 0)}/100\n\n"
            f"{result.get('reason', 'Insufficient confluence')}\n\n"
            "Waiting for a stronger setup."
        )
    )


# ============================================================
# AUTOMATIC SCANNER
# ============================================================

def automatic_scan():

    global LAST_AUTO_SETUP_ID
    global LAST_AUTO_SIGNAL
    global LAST_AUTO_ALERT_TIME

    m1 = get_xau_data(
        "1min",
        150
    )

    m5 = get_xau_data(
        "5min",
        150
    )

    m15 = get_xau_data(
        "15min",
        150
    )

    if (
        not m1
        or not m5
        or not m15
    ):

        logging.warning(
            "Automatic scan: "
            "market data unavailable"
        )

        return

    setup_id = make_setup_id(
        m1,
        m5,
        m15
    )

    result = analyze_pro(
        m1,
        m5,
        m15
    )

    if not result:
        return

    logging.info(
        f"SCAN | "
        f"{result['signal']} | "
        f"{result.get('score', 0)}/100 | "
        f"Setup {setup_id}"
    )

    if result["signal"] not in [
        "BUY",
        "SELL"
    ]:
        return

    if result["score"] < 80:
        return

    # Same setup = don't alert again
    if (
        setup_id == LAST_AUTO_SETUP_ID
        and result["signal"] == LAST_AUTO_SIGNAL
    ):

        logging.info(
            "Same setup - alert skipped"
        )

        return

    # Safety cooldown
    if (
        time.time()
        - LAST_AUTO_ALERT_TIME
        < 600
    ):

        logging.info(
            "10-minute alert cooldown"
        )

        return

    text = format_signal(
        result,
        "NEW AUTOMATIC SETUP"
    )

    if send_message(
        TELEGRAM_CHAT_ID,
        text
    ):

        LAST_AUTO_SETUP_ID = setup_id
        LAST_AUTO_SIGNAL = result["signal"]
        LAST_AUTO_ALERT_TIME = time.time()

        logging.info(
            f"ALERT SENT | "
            f"{result['signal']} | "
            f"{result['score']}/100"
        )


# ============================================================
# SCANNER THREAD
# ============================================================

def auto_scanner():

    logging.info(
        "CANDLE STRUCTURE "
        "PRO SCALPER V2 STARTED"
    )

    time.sleep(15)

    while True:

        try:

            if is_trading_window():

                automatic_scan()

            else:

                logging.info(
                    "Outside trading window"
                )

            # Check every minute
            time.sleep(60)

        except Exception as e:

            logging.error(
                f"Scanner error: {e}"
            )

            time.sleep(30)


# ============================================================
# WEB ROUTES
# ============================================================

@app.route("/")
def home():

    return (
        "Candle Structure "
        "Pro Scalper V2 running"
    )


@app.route(
    "/telegram",
    methods=["POST"]
)
def webhook():

    try:

        data = request.get_json()

        if not data:
            return "ok"

        if "message" not in data:
            return "ok"

        message = data["message"]

        if "text" not in message:
            return "ok"

        text = (
            message["text"]
            .strip()
            .lower()
        )

        chat_id = message["chat"]["id"]

        # ----------------------------------------------------
        # MANUAL SIGNAL
        # ----------------------------------------------------

        if text.startswith("/signal"):

            manual_signal(
                chat_id
            )

        # ----------------------------------------------------
        # START
        # ----------------------------------------------------

        elif text == "/start":

            send_message(
                chat_id,
                (
                    "🤖 XAUUSD "
                    "Candle Structure "
                    "Pro Scalper V2\n\n"

                    "Automatic scanner: ON\n"
                    "Timeframes: M15 + M5 + M1\n"
                    "Minimum score: 80/100\n\n"

                    "/signal = analyze now"
                )
            )

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        elif text == "/status":

            send_message(
                chat_id,
                (
                    "🤖 BOT STATUS\n\n"
                    "Scanner: ON\n"
                    "Strategy: Candle Structure\n"
                    "M15: Context\n"
                    "M5: Structure\n"
                    "M1: Confirmation\n"
                    "Minimum score: 80/100\n"
                    "Lot: 0.01"
                )
            )

        return "ok"

    except Exception as e:

        logging.error(
            f"Webhook error: {e}"
        )

        return "ok"


# ============================================================
# START AUTO SCANNER
# ============================================================

threading.Thread(
    target=auto_scanner,
    daemon=True
).start()


# ============================================================
# START FLASK
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
