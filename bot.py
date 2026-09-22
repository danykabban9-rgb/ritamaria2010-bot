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

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if TELEGRAM_TOKEN
    else ""
)

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

LAST_AUTO_SETUP_ID = None
LAST_MANUAL_SETUP_ID = None

TELEGRAM_OFFSET = 0

# Prevent two scans from running at the same time
SCAN_LOCK = threading.Lock()


# ============================================================
# TELEGRAM SEND
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram variables are missing.")
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
            logging.error(
                "Telegram send error: %s",
                response.text
            )
            return False

        return True

    except requests.RequestException as e:
        logging.error(
            "Telegram connection error: %s",
            e
        )
        return False

    except Exception as e:
        logging.exception(
            "Telegram unexpected error: %s",
            e
        )
        return False


# ============================================================
# TELEGRAM POLLING
# ============================================================

def process_telegram_command(text_message):

    global LAST_MANUAL_SETUP_ID

    if not text_message:
        return

    try:

        if text_message.startswith("/start"):

            send_telegram(
                "🟢 XAUUSD Candle Expert ONLINE\n\n"
                "Commands:\n"
                "/signal - scan gold now\n"
                "/status - bot status"
            )

        elif text_message.startswith("/status"):

            send_telegram(
                "🟢 BOT ONLINE\n\n"
                f"Symbol: {SYMBOL}\n"
                f"Scan: every {SCAN_SECONDS}s\n"
                f"Minimum score: {MIN_SCORE}\n"
                f"Lot: {LOT_SIZE}"
            )

        elif text_message.startswith("/signal"):

            logging.info("Manual /signal received.")

            setup = analyze_market()

            if not setup:

                send_telegram(
                    "⏳ NO HIGH-QUALITY SETUP\n\n"
                    "The candle structure does not "
                    "currently meet the 80-point "
                    "confirmation threshold.\n\n"
                    "No trade."
                )

                return

            current_id = setup_id(setup)

            message = format_signal(setup)

            if current_id == LAST_MANUAL_SETUP_ID:

                message = (
                    "ℹ️ SAME SETUP\n\n"
                    + message
                )

            if send_telegram(message):

                LAST_MANUAL_SETUP_ID = current_id

                logging.info(
                    "Manual signal sent: %s",
                    setup["direction"]
                )

    except Exception as e:

        logging.exception(
            "Command processing error: %s",
            e
        )


def telegram_polling():

    global TELEGRAM_OFFSET

    if not TELEGRAM_TOKEN:

        logging.error(
            "TELEGRAM_TOKEN missing. "
            "Telegram polling cannot start."
        )

        return

    logging.info(
        "Telegram polling started."
    )

    # Remove old webhook so getUpdates works.
    try:

        requests.post(
            f"{TELEGRAM_API}/deleteWebhook",
            params={
                "drop_pending_updates": False
            },
            timeout=15
        )

        logging.info(
            "Telegram webhook removed. "
            "Polling mode active."
        )

    except Exception as e:

        logging.error(
            "Could not remove Telegram webhook: %s",
            e
        )

    while True:

        try:

            response = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    "offset": TELEGRAM_OFFSET,
                    "timeout": 25,
                    "allowed_updates": '["message"]'
                },
                timeout=35
            )

            if response.status_code != 200:

                logging.error(
                    "Telegram getUpdates error: %s",
                    response.text
                )

                time.sleep(5)
                continue

            data = response.json()

            if not data.get("ok"):

                logging.error(
                    "Telegram API returned error: %s",
                    data
                )

                time.sleep(5)
                continue

            updates = data.get(
                "result",
                []
            )

            for update in updates:

                try:

                    TELEGRAM_OFFSET = (
                        update["update_id"] + 1
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

                        # Process command without killing polling.
                        threading.Thread(
                            target=process_telegram_command,
                            args=(text_message,),
                            daemon=True
                        ).start()

                except Exception as e:

                    logging.exception(
                        "Update processing error: %s",
                        e
                    )

        except requests.RequestException as e:

            logging.error(
                "Telegram polling connection error: %s",
                e
            )

            time.sleep(5)

        except Exception as e:

            logging.exception(
                "Telegram polling unexpected error: %s",
                e
            )

            time.sleep(5)


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(interval, outputsize=100):

    if not TWELVE_DATA_KEY:

        logging.error(
            "TWELVE_DATA_KEY is missing."
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

            logging.error(
                "Twelve Data HTTP error: %s",
                response.status_code
            )

            return []

        data = response.json()

        if "values" not in data:

            logging.error(
                "Twelve Data response: %s",
                data
            )

            return []

        candles = []

        for item in reversed(data["values"]):

            try:

                candles.append({
                    "time": item["datetime"],
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"])
                })

            except (KeyError, ValueError, TypeError):

                continue

        return candles

    except requests.RequestException as e:

        logging.error(
            "Twelve Data connection error: %s",
            e
        )

        return []

    except Exception as e:

        logging.exception(
            "Twelve Data unexpected error: %s",
            e
        )

        return []


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

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

        true_range = max(
            tr1,
            tr2,
            tr3
        )

        true_ranges.append(
            true_range
        )

    if len(true_ranges) < period:
        return None

    recent = true_ranges[-period:]

    return (
        sum(recent)
        / len(recent)
    )


# ============================================================
# CANDLE ANALYSIS
# ============================================================

def bullish_engulfing(previous, current):

    return (
        previous["close"] < previous["open"]
        and current["close"] > current["
