import os
import time
import threading
import logging
import hashlib
import requests
from datetime import datetime
from flask import Flask, request, jsonify


# ============================================================
# RITAMARIAGOLD
# XAUUSD CANDLE STRUCTURE EXPERT
# STABLE / DIAGNOSTIC VERSION
# ============================================================

SYMBOL = "XAU/USD"

SCAN_SECONDS = 60

MIN_SCORE = 80
LOT_SIZE = "0.01 ONLY"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if TELEGRAM_TOKEN
    else ""
)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# GLOBAL STATE
# ============================================================

LAST_AUTO_SETUP_ID = None
LAST_MANUAL_SETUP_ID = None

TELEGRAM_OFFSET = 0

SHUTDOWN_EVENT = threading.Event()

scanner_thread = None
telegram_thread = None
supervisor_thread = None

scanner_restart_count = 0
telegram_restart_count = 0

last_scanner_heartbeat = 0
last_telegram_heartbeat = 0

last_scan_time = 0
last_scan_result = "NOT STARTED"

last_telegram_success = 0
last_telegram_error = ""

last_data_success = 0
last_data_error = ""

services_started = False

# IMPORTANT:
# RLock prevents the startup deadlock in the previous version.
thread_manager_lock = threading.RLock()

SCAN_LOCK = threading.Lock()


# ============================================================
# TIME
# ============================================================

def now_string():

    try:
        return datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return "unknown"


# ============================================================
# TELEGRAM SEND
# ============================================================

def send_telegram(message):

    global last_telegram_success
    global last_telegram_error

    if not TELEGRAM_TOKEN:

        last_telegram_error = (
            "TELEGRAM_TOKEN missing"
        )

        logging.error(
            "TELEGRAM_TOKEN is missing."
        )

        return False

    if not TELEGRAM_CHAT_ID:

        last_telegram_error = (
            "TELEGRAM_CHAT_ID missing"
        )

        logging.error(
            "TELEGRAM_CHAT_ID is missing."
        )

        return False

    if not message:

        last_telegram_error = (
            "Empty Telegram message"
        )

        logging.error(
            "Telegram message is empty."
        )

        return False

    url = f"{TELEGRAM_API}/sendMessage"

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

            last_telegram_error = (
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

            logging.error(
                "Telegram HTTP error: %s",
                response.status_code
            )

            return False

        data = response.json()

        if not data.get("ok"):

            last_telegram_error = str(data)

            logging.error(
                "Telegram API rejected message: %s",
                data
            )

            return False

        last_telegram_success = time.time()
        last_telegram_error = ""

        return True

    except requests.RequestException as e:

        last_telegram_error = str(e)

        logging.error(
            "Telegram connection error: %s",
            e
        )

        return False

    except Exception as e:

        last_telegram_error = str(e)

        logging.exception(
            "Telegram send unexpected error."
        )

        return False


# ============================================================
# TELEGRAM API TEST
# ============================================================

def telegram_api_test():

    if not TELEGRAM_TOKEN:

        return False, "TELEGRAM_TOKEN missing"

    try:

        response = requests.get(
            f"{TELEGRAM_API}/getMe",
            timeout=10
        )

        if response.status_code != 200:

            return False, (
                f"HTTP {response.status_code}"
            )

        data = response.json()

        if not data.get("ok"):

            return False, str(data)

        username = (
            data.get("result", {})
            .get("username", "unknown")
        )

        return True, username

    except Exception as e:

        return False, str(e)


# ============================================================
# TELEGRAM COMMAND PROCESSOR
# ============================================================

def process_telegram_command(text_message):

    global LAST_MANUAL_SETUP_ID

    try:

        if not text_message:

            return

        parts = text_message.strip().split()

        if not parts:

            return

        command = parts[0].lower()

        # ====================================================
        # START
        # ====================================================

        if command == "/start":

            send_telegram(

                "🟢 RITAMARIAGOLD ONLINE\n"
                "━━━━━━━━━━━━━━━━━━\n\n"

                "XAUUSD Candle Structure Expert\n\n"

                "Commands:\n"
                "/signal - Full gold analysis\n"
                "/status - Service status\n"
                "/debug - Full diagnostics\n\n"

                f"Scanner: every {SCAN_SECONDS}s\n"
                f"Minimum score: {MIN_SCORE}\n"
                f"Lot: {LOT_SIZE}"
            )

            return

        # ====================================================
        # STATUS
        # ====================================================

        if command == "/status":

            send_telegram(
                build_status_message()
            )

            return

        # ====================================================
        # DEBUG
        # ====================================================

        if command == "/debug":

            send_telegram(
                build_debug_message()
            )

            return

        # ====================================================
        # SIGNAL
        # ====================================================

        if command == "/signal":

            logging.info(
                "Manual /signal received."
            )

            result = analyze_market()

            if not result:

                send_telegram(

                    "⚠️ ANALYSIS FAILED\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"

                    "Market data could not be analyzed.\n\n"

                    f"Data error:\n"
                    f"{last_data_error or 'Unknown'}"
                )

                return

            message = format_analysis_report(
                result
            )

            send_telegram(message)

            setup = result.get("setup")

            if setup:

                LAST_MANUAL_SETUP_ID = (
                    setup_id(setup)
                )

            return

        # ====================================================
        # UNKNOWN
        # ====================================================

        send_telegram(

            "❓ Unknown command.\n\n"

            "Available:\n"
            "/start\n"
            "/signal\n"
            "/status\n"
            "/debug"
        )

    except Exception as e:

        logging.exception(
            "Command processing error."
        )

        try:

            send_telegram(
                f"⚠️ Command error:\n{str(e)[:500]}"
            )

        except Exception:
            pass


# ============================================================
# STATUS MESSAGE
# ============================================================

def build_status_message():

    scanner_alive = (
        scanner_thread is not None
        and scanner_thread.is_alive()
    )

    telegram_alive = (
        telegram_thread is not None
        and telegram_thread.is_alive()
    )

    supervisor_alive = (
        supervisor_thread is not None
        and supervisor_thread.is_alive()
    )

    scanner_age = (
        int(time.time() - last_scanner_heartbeat)
        if last_scanner_heartbeat
        else -1
    )

    telegram_age = (
        int(time.time() - last_telegram_heartbeat)
        if last_telegram_heartbeat
        else -1
    )

    return (

        "🟢 RITAMARIAGOLD STATUS\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"Scanner: "
        f"{'🟢 ALIVE' if scanner_alive else '🔴 DEAD'}\n"

        f"Telegram: "
        f"{'🟢 ALIVE' if telegram_alive else '🔴 DEAD'}\n"

        f"Supervisor: "
        f"{'🟢 ALIVE' if supervisor_alive else '🔴 DEAD'}\n\n"

        f"Scanner heartbeat: "
        f"{scanner_age}s ago\n"

        f"Telegram heartbeat: "
        f"{telegram_age}s ago\n\n"

        f"Scanner restarts: "
        f"{scanner_restart_count}\n"

        f"Telegram restarts: "
        f"{telegram_restart_count}\n\n"

        f"Last scan: "
        f"{last_scan_result}\n\n"

        f"Symbol: {SYMBOL}\n"
        f"Scan: {SCAN_SECONDS}s\n"
        f"Minimum score: {MIN_SCORE}\n"
        f"Lot: {LOT_SIZE}"
    )


# ============================================================
# DEBUG MESSAGE
# ============================================================

def build_debug_message():

    scanner_alive = (
        scanner_thread is not None
        and scanner_thread.is_alive()
    )

    telegram_alive = (
        telegram_thread is not None
        and telegram_thread.is_alive()
    )

    supervisor_alive = (
        supervisor_thread is not None
        and supervisor_thread.is_alive()
    )

    # Telegram API

    telegram_ok, telegram_info = (
        telegram_api_test()
    )

    # Time since events

    if last_scanner_heartbeat:

        scanner_age = int(
            time.time()
            - last_scanner_heartbeat
        )

    else:

        scanner_age = -1

    if last_telegram_heartbeat:

        telegram_age = int(
            time.time()
            - last_telegram_heartbeat
        )

    else:

        telegram_age = -1

    if last_data_success:

        data_age = int(
            time.time()
            - last_data_success
        )

    else:

        data_age = -1

    return (

        "🛠 RITAMARIAGOLD DEBUG\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"Process: 🟢 ALIVE\n"

        f"Telegram thread: "
        f"{'🟢 ALIVE' if telegram_alive else '🔴 DEAD'}\n"

        f"Scanner thread: "
        f"{'🟢 ALIVE' if scanner_alive else '🔴 DEAD'}\n"

        f"Supervisor: "
        f"{'🟢 ALIVE' if supervisor_alive else '🔴 DEAD'}\n\n"

        f"Telegram API: "
        f"{'🟢 OK' if telegram_ok else '🔴 ERROR'}\n"

        f"Telegram bot: "
        f"{telegram_info}\n\n"

        f"Scanner heartbeat: "
        f"{scanner_age}s ago\n"

        f"Telegram heartbeat: "
        f"{telegram_age}s ago\n"

        f"Data success: "
        f"{data_age}s ago\n\n"

        f"Last scan:\n"
        f"{last_scan_result}\n\n"

        f"Last Telegram error:\n"
        f"{last_telegram_error or 'NONE'}\n\n"

        f"Last data error:\n"
        f"{last_data_error or 'NONE'}\n\n"

        f"Scanner restarts: "
        f"{scanner_restart_count}\n"

        f"Telegram restarts: "
        f"{telegram_restart_count}\n\n"

        f"Server time:\n"
        f"{now_string()}"
    )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():

    global TELEGRAM_OFFSET
    global last_telegram_heartbeat
    global last_telegram_error

    logging.info(
        "=================================================="
    )

    logging.info(
        "TELEGRAM POLLING STARTED"
    )

    logging.info(
        "=================================================="
    )

    if not TELEGRAM_TOKEN:

        logging.error(
            "TELEGRAM_TOKEN missing."
        )

        return

    # --------------------------------------------------------
    # Check Telegram
    # --------------------------------------------------------

    ok, info = telegram_api_test()

    if ok:

        logging.info(
            "Telegram API OK | Bot: %s",
            info
        )

    else:

        logging.error(
            "Telegram API test failed: %s",
            info
        )

    # --------------------------------------------------------
    # Remove webhook
    # --------------------------------------------------------

    try:

        response = requests.post(
            f"{TELEGRAM_API}/deleteWebhook",
            params={
                "drop_pending_updates": False
            },
            timeout=15
        )

        logging.info(
            "deleteWebhook: %s",
            response.text[:500]
        )

    except Exception as e:

        logging.error(
            "deleteWebhook failed: %s",
            e
        )

    # --------------------------------------------------------
    # Poll
    # --------------------------------------------------------

    while not SHUTDOWN_EVENT.is_set():

        try:

            last_telegram_heartbeat = (
                time.time()
            )

            response = requests.get(

                f"{TELEGRAM_API}/getUpdates",

                params={
                    "offset": TELEGRAM_OFFSET,
                    "timeout": 20,
                    "allowed_updates": '["message"]'
                },

                timeout=30
            )

            last_telegram_heartbeat = (
                time.time()
            )

            if response.status_code != 200:

                last_telegram_error = (
                    f"getUpdates HTTP "
                    f"{response.status_code}"
                )

                logging.error(
                    "Telegram getUpdates HTTP error: %s",
                    response.status_code
                )

                time.sleep(5)

                continue

            data = response.json()

            if not data.get("ok"):

                last_telegram_error = str(data)

                logging.error(
                    "Telegram getUpdates error: %s",
                    data
                )

                time.sleep(5)

                continue

            last_telegram_error = ""

            updates = data.get(
                "result",
                []
            )

            for update in updates:

                try:

                    update_id = update.get(
                        "update_id"
                    )

                    if update_id is not None:

                        TELEGRAM_OFFSET = (
                            update_id + 1
                        )

                    message = update.get(
                        "message",
                        {}
                    )

                    text_message = message.get(
                        "text",
                        ""
                    )

                    if text_message:

                        logging.info(
                            "Telegram command received: %s",
                            text_message
                        )

                        threading.Thread(

                            target=process_telegram_command,

                            args=(text_message,),

                            name="telegram-command",

                            daemon=True

                        ).start()

                except Exception:

                    logging.exception(
                        "Telegram update processing error."
                    )

        except requests.RequestException as e:

            last_telegram_error = str(e)

            logging.error(
                "Telegram polling connection error: %s",
                e
            )

            time.sleep(5)

        except Exception as e:

            last_telegram_error = str(e)

            logging.exception(
                "Telegram polling unexpected error."
            )

            time.sleep(5)

    logging.warning(
        "Telegram polling stopped."
    )


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(interval, outputsize=100):

    global last_data_success
    global last_data_error

    if not TWELVE_DATA_KEY:

        last_data_error = (
            "TWELVE_DATA_KEY missing"
        )

        logging.error(
            "TWELVE_DATA_KEY missing."
        )

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

            last_data_error = (
                f"HTTP {response.status_code}"
            )

            logging.error(
                "Twelve Data HTTP error: %s",
                response.status_code
            )

            return []

        data = response.json()

        if "values" not in data:

            last_data_error = str(data)

            logging.error(
                "Twelve Data response: %s",
                data
            )

            return []

        candles = []

        for item in reversed(
            data["values"]
        ):

            try:

                candles.append({

                    "time":
                        item["datetime"],

                    "open":
                        float(item["open"]),

                    "high":
                        float(item["high"]),

                    "low":
                        float(item["low"]),

                    "close":
                        float(item["close"])
                })

            except (
                KeyError,
                ValueError,
                TypeError
            ):

                continue

        if candles:

            last_data_success = time.time()
            last_data_error = ""

        return candles

    except requests.RequestException as e:

        last_data_error = str(e)

        logging.error(
            "Twelve Data connection error: %s",
            e
        )

        return []

    except Exception as e:

        last_data_error = str(e)

        logging.exception(
            "Twelve Data unexpected error."
        )

        return []


# ============================================================
# ATR
# ============================================================

def calculate_atr(
    candles,
    period=14
):

    if len(candles) < period + 1:

        return None

    true_ranges = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        tr1 = (
            current["high"]
            - current["low"]
        )

        tr2 = abs(
            current["high"]
            - previous["close"]
        )

        tr3 = abs(
            current["low"]
            - previous["close"]
        )

        true_ranges.append(
            max(
                tr1,
                tr2,
                tr3
            )
        )

    if len(true_ranges) < period:

        return None

    return (
        sum(true_ranges[-period:])
        / period
    )


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def bullish_engulfing(previous, current):

    return (

        previous["close"]
        < previous["open"]

        and

        current["close"]
        > current["open"]

        and

        current["open"]
        <= previous["close"]

        and

        current["close"]
        >= previous["open"]
    )


def bearish_engulfing(previous, current):

    return (

        previous["close"]
        > previous["open"]

        and

        current["close"]
        < current["open"]

        and

        current["open"]
        >= previous["close"]

        and

        current["close"]
        <= previous["open"]
    )


def bullish_rejection(candle):

    body = abs(
        candle["close"]
        - candle["open"]
    )

    lower_wick = (
        min(
            candle["open"],
            candle["close"]
        )
        - candle["low"]
    )

    upper_wick = (
        candle["high"]
        - max(
            candle["open"],
            candle["close"]
        )
    )

    if body == 0:

        body = 0.00001

    return (

        lower_wick > body * 1.5

        and

        lower_wick > upper_wick

        and

        candle["close"]
        > candle["open"]
    )


def bearish_rejection(candle):

    body = abs(
        candle["close"]
        - candle["open"]
    )

    upper_wick = (
        candle["high"]
        - max(
            candle["open"],
            candle["close"]
        )
    )

    lower_wick = (
        min(
            candle["open"],
            candle["close"]
        )
        - candle["low"]
    )

    if body == 0:

        body = 0.00001

    return (

        upper_wick > body * 1.5

        and

        upper_wick > lower_wick

        and

        candle["close"]
        < candle["open"]
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(candles):

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

    last_close = recent[-1]["close"]

    previous_high = max(
        highs[:-3]
    )

    previous_low = min(
        lows[:-3]
    )

    if last_close > previous_high:

        return "BULLISH"

    if last_close < previous_low:

        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(candles):

    if len(candles) < 10:

        return None

    current = candles[-1]

    recent = candles[-10:-2]

    previous_high = max(
        c["high"]
        for c in recent
    )

    previous_low = min(
        c["low"]
        for c in recent
    )

    if (
        current["high"]
        > previous_high

        and

        current["close"]
        < previous_high
    ):

        return "BEARISH"

    if (
        current["low"]
        < previous_low

        and

        current["close"]
        > previous_low
    ):

        return "BULLISH"

    return None


# ============================================================
# MARKET ANALYSIS
# ============================================================

def analyze_market():

    if not SCAN_LOCK.acquire(
        blocking=False
    ):

        logging.warning(
            "Another analysis is running."
        )

        return None

    try:

        logging.info(
            "Starting full XAUUSD analysis..."
        )

        candles_m5 = get_candles(
            "5min",
            100
        )

        candles_m1 = get_candles(
            "1min",
            100
        )

        candles_m15 = get_candles(
            "15min",
            100
        )

        if (
            len(candles_m5) < 30
            or len(candles_m1) < 10
            or len(candles_m15) < 20
        ):

            logging.warning(
                "Not enough candle data."
            )

            return None

        m15_structure = market_structure(
            candles_m15
        )

        m5_structure = market_structure(
            candles_m5
        )

        sweep = detect_liquidity_sweep(
            candles_m5
        )

        previous_m1 = candles_m1[-2]
        current_m1 = candles_m1[-1]

        atr = calculate_atr(
            candles_m5,
            14
        )

        if atr is None or atr <= 0:

            logging.warning(
                "Invalid ATR."
            )

            return None

        price = current_m1["close"]

        # ----------------------------------------------------
        # ENGULFING
        # ----------------------------------------------------

        m1_engulfing = "NONE"

        if bullish_engulfing(
            previous_m1,
            current_m1
        ):

            m1_engulfing = (
                "BULLISH ENGULFING"
            )

        elif bearish_engulfing(
            previous_m1,
            current_m1
        ):

            m1_engulfing = (
                "BEARISH ENGULFING"
            )

        # ----------------------------------------------------
        # REJECTION
        # ----------------------------------------------------

        m1_rejection = "NONE"

        if bullish_rejection(
            current_m1
        ):

            m1_rejection = (
                "BULLISH REJECTION"
            )

        elif bearish_rejection(
            current_m1
        ):

            m1_rejection = (
                "BEARISH REJECTION"
            )

        # ----------------------------------------------------
        # SCORES
        # ----------------------------------------------------

        buy_score = 0
        sell_score = 0

        reasons_buy = []
        reasons_sell = []

        if m15_structure == "BULLISH":

            buy_score += 20

            reasons_buy.append(
                "M15 bullish structure"
            )

        elif m15_structure == "BEARISH":

            sell_score += 20

            reasons_sell.append(
                "M15 bearish structure"
            )

        if m5_structure == "BULLISH":

            buy_score += 20

            reasons_buy.append(
                "M5 bullish structure"
            )

        elif m5_structure == "BEARISH":

            sell_score += 20

            reasons_sell.append(
                "M5 bearish structure"
            )

        if sweep == "BULLISH":

            buy_score += 20

            reasons_buy.append(
                "Bullish liquidity sweep"
            )

        elif sweep == "BEARISH":

            sell_score += 20

            reasons_sell.append(
                "Bearish liquidity sweep"
            )

        if (
            m1_engulfing
            == "BULLISH ENGULFING"
        ):

            buy_score += 20

            reasons_buy.append(
                "M1 bullish engulfing"
            )

        elif (
            m1_engulfing
            == "BEARISH ENGULFING"
        ):

            sell_score += 20

            reasons_sell.append(
                "M1 bearish engulfing"
            )

        if (
            m1_rejection
            == "BULLISH REJECTION"
        ):

            buy_score += 15

            reasons_buy.append(
                "M1 bullish rejection"
            )

        elif (
            m1_rejection
            == "BEARISH REJECTION"
        ):

            sell_score += 15

            reasons_sell.append(
                "M1 bearish rejection"
            )

        # ----------------------------------------------------
        # DISPLACEMENT
        # ----------------------------------------------------

        candle_range = (
            current_m1["high"]
            - current_m1["low"]
        )

        displacement = "NONE"

        if candle_range > atr * 0.20:

            if (
                current_m1["close"]
                > current_m1["open"]
            ):

                buy_score += 10

                displacement = (
                    "BULLISH DISPLACEMENT"
                )

                reasons_buy.append(
                    "Bullish displacement"
                )

            elif (
                current_m1["close"]
                < current_m1["open"]
            ):

                sell_score += 10

                displacement = (
                    "BEARISH DISPLACEMENT"
                )

                reasons_sell.append(
                    "Bearish displacement"
                )

        # ----------------------------------------------------
        # OVERALL
        # ----------------------------------------------------

        if (
            m15_structure == "BULLISH"
            and
            m5_structure == "BULLISH"
        ):

            overall = "STRONG BULLISH"

        elif (
            m15_structure == "BEARISH"
            and
            m5_structure == "BEARISH"
        ):

            overall = "STRONG BEARISH"

        elif (
            m15_structure == "BULLISH"
            or
            m5_structure == "BULLISH"
        ):

            overall = "BULLISH / MIXED"

        elif (
            m15_structure == "BEARISH"
            or
            m5_structure == "BEARISH"
        ):

            overall = "BEARISH / MIXED"

        else:

            overall = "NEUTRAL / RANGE"

        # ----------------------------------------------------
        # SIGNAL
        # ----------------------------------------------------

        setup = None

        if (
            buy_score >= MIN_SCORE
            and
            buy_score > sell_score
        ):

            entry = price

            sl = (
                entry
                - atr * 0.80
            )

            tp1 = (
                entry
                + atr * 0.80
            )

            tp2 = (
                entry
                + atr * 1.50
            )

            setup = {

                "direction": "BUY",

                "score": buy_score,

                "entry": entry,

                "sl": sl,

                "tp1": tp1,

                "tp2": tp2,

                "atr": atr,

                "reasons": reasons_buy,

                "candle_time":
                    current_m1["time"]
            }

        elif (
            sell_score >= MIN_SCORE
            and
            sell_score > buy_score
        ):

            entry = price

            sl = (
                entry
                + atr * 0.80
            )

            tp1 = (
                entry
                - atr * 0.80
            )

            tp2 = (
                entry
                - atr * 1.50
            )

            setup = {

                "direction": "SELL",

                "score": sell_score,

                "entry": entry,

                "sl": sl,

                "tp1": tp1,

                "tp2": tp2,

                "atr": atr,

                "reasons": reasons_sell,

                "candle_time":
                    current_m1["time"]
            }

        return {

            "price": price,

            "atr": atr,

            "m15": m15_structure,

            "m5": m5_structure,

            "overall": overall,

            "sweep":
                sweep or "NONE",

            "m1_engulfing":
                m1_engulfing,

            "m1_rejection":
                m1_rejection,

            "displacement":
                displacement,

            "buy_score":
                buy_score,

            "sell_score":
                sell_score,

            "candle_time":
                current_m1["time"],

            "setup": setup
        }

    except Exception as e:

        logging.exception(
            "Market analysis error."
        )

        return None

    finally:

        SCAN_LOCK.release()


# ============================================================
# FORMAT REPORT
# ============================================================

def format_analysis_report(result):

    overall = result["overall"]

    if (
        "BULLISH" in overall
        and
        "BEARISH" not in overall
    ):

        overall_emoji = "🟢"

    elif "BEARISH" in overall:

        overall_emoji = "🔴"

    else:

        overall_emoji = "⚪"

    setup = result["setup"]

    message = (

        "🔎 XAUUSD CANDLE EXPERT\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"💰 PRICE: "
        f"{result['price']:.2f}\n"

        f"📊 OVERALL: "
        f"{overall_emoji} {overall}\n\n"

        "🏦 MARKET STRUCTURE\n"

        f"• M15: {result['m15']}\n"
        f"• M5: {result['m5']}\n"
        f"• Liquidity Sweep: "
        f"{result['sweep']}\n\n"

        "🕯 M1 CANDLE\n"

        f"• Engulfing: "
        f"{result['m1_engulfing']}\n"

        f"• Rejection: "
        f"{result['m1_rejection']}\n"

        f"• Displacement: "
        f"{result['displacement']}\n\n"

        "📈 SCORE\n"

        f"• BUY: {result['buy_score']}\n"
        f"• SELL: {result['sell_score']}\n"
        f"• Required: {MIN_SCORE}\n\n"

        f"📏 M5 ATR: "
        f"{result['atr']:.2f}\n"

        f"🕐 Candle: "
        f"{result['candle_time']}\n\n"
    )

    if setup:

        direction = setup["direction"]

        emoji = (
            "🟢"
            if direction == "BUY"
            else "🔴"
        )

        reasons = "\n".join(
            f"• {reason}"
            for reason in setup["reasons"]
        )

        message += (

            "━━━━━━━━━━━━━━━━━━\n"

            f"🚨 SIGNAL: "
            f"{emoji} {direction}\n"

            "━━━━━━━━━━━━━━━━━━\n\n"

            f"ENTRY: {setup['entry']:.2f}\n"
            f"SL:    {setup['sl']:.2f}\n"
            f"TP1:   {setup['tp1']:.2f}\n"
            f"TP2:   {setup['tp2']:.2f}\n\n"

            f"SCORE: {setup['score']}\n"
            f"LOT: {LOT_SIZE}\n\n"

            "WHY THIS SIGNAL:\n"
            f"{reasons}\n\n"

            "⚠️ Manual execution only.\n"
            "Wait for price confirmation."
        )

    else:

        highest_score = max(
            result["buy_score"],
            result["sell_score"]
        )

        missing = max(
            0,
            MIN_SCORE - highest_score
        )

        if (
            result["buy_score"]
            >
            result["sell_score"]
        ):

            candidate = "BUY"

        elif (
            result["sell_score"]
            >
            result["buy_score"]
        ):

            candidate = "SELL"

        else:

            candidate = "NONE"

        message += (

            "━━━━━━━━━━━━━━━━━━\n"
            "⏳ NO TRADE\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"Current candidate: {candidate}\n"

            f"Highest score: "
            f"{highest_score}\n"

            f"Points needed: "
            f"{missing}\n\n"

            "The candle structure has not "
            f"reached the {MIN_SCORE}-point "
            "confirmation threshold.\n\n"

            "🛑 NO ENTRY\n"
            "🛑 NO SL\n"
            "🛑 NO TP\n\n"

            "Waiting for stronger "
            "M1/M5/M15 confirmation."
        )

    return message


# ============================================================
# SETUP ID
# ============================================================

def setup_id(setup):

    raw = (
        f"{setup['direction']}-"
        f"{setup['candle_time']}-"
        f"{round(setup['entry'], 2)}"
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()


# ============================================================
# SCANNER
# ============================================================

def scanner():

    global LAST_AUTO_SETUP_ID
    global last_scanner_heartbeat
    global last_scan_time
    global last_scan_result

    logging.info(
        "=================================================="
    )

    logging.info(
        "🟢 GOLD SCANNER STARTED"
    )

    logging.info(
        "=================================================="
    )

    while not SHUTDOWN_EVENT.is_set():

        cycle_start = time.time()

        try:

            last_scanner_heartbeat = (
                time.time()
            )

            logging.info(
                "🟢 SCANNER ALIVE | "
                "Starting XAUUSD scan"
            )

            result = analyze_market()

            last_scan_time = time.time()

            last_scanner_heartbeat = (
                time.time()
            )

            if result:

                last_scan_result = (
                    f"M15={result['m15']} | "
                    f"M5={result['m5']} | "
                    f"BUY={result['buy_score']} | "
                    f"SELL={result['sell_score']}"
                )

                logging.info(
                    "ANALYSIS | %s",
                    last_scan_result
                )

                setup = result.get("setup")

                if setup:

                    current_id = setup_id(
                        setup
                    )

                    if (
                        current_id
                        != LAST_AUTO_SETUP_ID
                    ):

                        message = (
                            format_analysis_report(
                                result
                            )
                        )

                        sent = send_telegram(
                            message
                        )

                        if sent:

                            LAST_AUTO_SETUP_ID = (
                                current_id
                            )

                            logging.info(
                                "🚨 AUTO SIGNAL SENT | "
                                "%s | SCORE=%s",
                                setup["direction"],
                                setup["score"]
                            )

                        else:

                            logging.error(
                                "Signal created but "
                                "Telegram send failed."
                            )

                    else:

                        logging.info(
                            "Duplicate setup ignored."
                        )

                else:

                    logging.info(
                        "⏳ NO HIGH-QUALITY SETUP"
                    )

            else:

                last_scan_result = (
                    "ANALYSIS FAILED"
                )

                logging.warning(
                    "Analysis returned no result."
                )

        except Exception as e:

            last_scan_result = (
                f"ERROR: {str(e)[:200]}"
            )

            logging.exception(
                "🔥 SCANNER ERROR"
            )

        elapsed = (
            time.time()
            - cycle_start
        )

        remaining = max(
            1,
            SCAN_SECONDS - elapsed
        )

        logging.info(
            "Scanner cycle finished. "
            "Next scan in %.1f seconds.",
            remaining
        )

        SHUTDOWN_EVENT.wait(
            remaining
        )

    logging.warning(
        "Scanner thread stopped."
    )


# ============================================================
# START SCANNER
# ============================================================

def start_scanner_thread():

    global scanner_thread

    with thread_manager_lock:

        if (
            scanner_thread is not None
            and
            scanner_thread.is_alive()
        ):

            return False

        scanner_thread = threading.Thread(
            target=scanner,
            name="gold-scanner",
            daemon=True
        )

        scanner_thread.start()

        logging.info(
            "🟢 Scanner thread created."
        )

        return True


# ============================================================
# START TELEGRAM
# ============================================================

def start_telegram_thread():

    global telegram_thread

    with thread_manager_lock:

        if (
            telegram_thread is not None
            and
            telegram_thread.is_alive()
        ):

            return False

        telegram_thread = threading.Thread(
            target=telegram_polling,
            name="telegram-polling",
            daemon=True
        )

        telegram_thread.start()

        logging.info(
            "🟢 Telegram thread created."
        )

        return True


# ============================================================
# SUPERVISOR
# ============================================================

def supervisor():

    global scanner_restart_count
    global telegram_restart_count

    logging.info(
        "=================================================="
    )

    logging.info(
        "🛡 SUPERVISOR STARTED"
    )

    logging.info(
        "=================================================="
    )

    while not SHUTDOWN_EVENT.is_set():

        try:

            # ------------------------------------------------
            # Scanner
            # ------------------------------------------------

            if (
                scanner_thread is None
                or
                not scanner_thread.is_alive()
            ):

                scanner_restart_count += 1

                logging.warning(
                    "⚠️ SCANNER DEAD - "
                    "RESTARTING #%s",
                    scanner_restart_count
                )

                start_scanner_thread()

            # ------------------------------------------------
            # Telegram
            # ------------------------------------------------

            if TELEGRAM_TOKEN:

                if (
                    telegram_thread is None
                    or
                    not telegram_thread.is_alive()
                ):

                    telegram_restart_count += 1

                    logging.warning(
                        "⚠️ TELEGRAM DEAD - "
                        "RESTARTING #%s",
                        telegram_restart_count
                    )

                    start_telegram_thread()

            # ------------------------------------------------
            # Heartbeat
            # ------------------------------------------------

            now = time.time()

            scanner_age = (
                int(
                    now
                    - last_scanner_heartbeat
                )
                if last_scanner_heartbeat
                else -1
            )

            telegram_age = (
                int(
                    now
                    - last_telegram_heartbeat
                )
                if last_telegram_heartbeat
                else -1
            )

            logging.info(

                "🛡 WATCHDOG | "
                "Scanner=%s | "
                "Telegram=%s | "
                "ScannerHB=%ss | "
                "TelegramHB=%ss",

                (
                    "ALIVE"
                    if (
                        scanner_thread
                        and
                        scanner_thread.is_alive()
                    )
                    else
                    "DEAD"
                ),

                (
                    "ALIVE"
                    if (
                        telegram_thread
                        and
                        telegram_thread.is_alive()
                    )
                    else
                    "DEAD"
                ),

                scanner_age,

                telegram_age
            )

        except Exception:

            logging.exception(
                "Supervisor error."
            )

        SHUTDOWN_EVENT.wait(10)

    logging.warning(
        "Supervisor stopped."
    )


# ============================================================
# START ALL SERVICES
# ============================================================

def start_background_services():

    global services_started
    global supervisor_thread

    with thread_manager_lock:

        if services_started:

            logging.info(
                "Background services already started."
            )

            return

        services_started = True

        logging.info(
            "=================================================="
        )

        logging.info(
            "🚀 STARTING RITAMARIAGOLD"
        )

        logging.info(
            "=================================================="
        )

        # Start scanner

        start_scanner_thread()

        # Start Telegram

        if TELEGRAM_TOKEN:

            start_telegram_thread()

        else:

            logging.error(
                "❌ TELEGRAM_TOKEN missing."
            )

        # Start supervisor

        supervisor_thread = threading.Thread(
            target=supervisor,
            name="bot-supervisor",
            daemon=True
        )

        supervisor_thread.start()

        logging.info(
            "🟢 ALL BACKGROUND SERVICES STARTED."
        )


# ============================================================
# FLASK HOME
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    scanner_alive = (
        scanner_thread is not None
        and
        scanner_thread.is_alive()
    )

    telegram_alive = (
        telegram_thread is not None
        and
        telegram_thread.is_alive()
    )

    supervisor_alive = (
        supervisor_thread is not None
        and
        supervisor_thread.is_alive()
    )

    return jsonify({

        "status":
            "online",

        "bot":
            "RitamariaGold",

        "symbol":
            SYMBOL,

        "scanner":
            "running"
            if scanner_alive
            else
            "stopped",

        "telegram":
            "running"
            if telegram_alive
            else
            "stopped",

        "supervisor":
            "running"
            if supervisor_alive
            else
            "stopped",

        "scanner_restarts":
            scanner_restart_count,

        "telegram_restarts":
            telegram_restart_count,

        "last_scan":
            last_scan_result,

        "server_time":
            now_string()
    })


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    scanner_alive = (
        scanner_thread is not None
        and
        scanner_thread.is_alive()
    )

    telegram_alive = (
        telegram_thread is not None
        and
        telegram_thread.is_alive()
    )

    supervisor_alive = (
        supervisor_thread is not None
        and
        supervisor_thread.is_alive()
    )

    return jsonify({

        "status":
            "healthy",

        "scanner":
            scanner_alive,

        "telegram":
            telegram_alive,

        "supervisor":
            supervisor_alive,

        "scanner_restarts":
            scanner_restart_count,

        "telegram_restarts":
            telegram_restart_count,

        "last_scan":
            last_scan_result,

        "timestamp":
            int(time.time())
    })


# ============================================================
# MANUAL WEB SIGNAL
# ============================================================

@app.route(
    "/signal",
    methods=["GET", "POST"]
)
def manual_signal():

    global LAST_MANUAL_SETUP_ID

    try:

        result = analyze_market()

        if not result:

            return jsonify({
                "status":
                    "analysis_failed",

                "data_error":
                    last_data_error
            }), 500

        message = format_analysis_report(
            result
        )

        sent = send_telegram(
            message
        )

        if not sent:

            return jsonify({
                "status":
                    "telegram_failed",

                "telegram_error":
                    last_telegram_error
            }), 500

        setup = result.get("setup")

        if setup:

            LAST_MANUAL_SETUP_ID = (
                setup_id(setup)
            )

        return jsonify({

            "status":
                "analysis_sent",

            "has_signal":
                bool(setup),

            "buy_score":
                result["buy_score"],

            "sell_score":
                result["sell_score"]
        })

    except Exception as e:

        logging.exception(
            "Manual signal error."
        )

        return jsonify({

            "status":
                "error",

            "message":
                str(e)

        }), 500


# ============================================================
# TELEGRAM WEBHOOK COMPATIBILITY
# ============================================================

@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        message = data.get(
            "message",
            {}
        )

        text_message = message.get(
            "text",
            ""
        )

        if text_message:

            process_telegram_command(
                text_message
            )

        return jsonify({
            "ok": True
        })

    except Exception as e:

        logging.exception(
            "Webhook error."
        )

        return jsonify({

            "ok":
                False,

            "error":
                str(e)

        }), 500


# ============================================================
# START SERVICES ON IMPORT
# ============================================================

start_background_services()


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    logging.info(
        "Starting Flask on port %s",
        port
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
