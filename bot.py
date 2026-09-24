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

SCAN_LOCK = threading.Lock()

# ============================================================
# STABILITY / WATCHDOG
# ============================================================

shutdown_event = threading.Event()

scanner_thread = None
telegram_thread = None

scanner_restart_count = 0
telegram_restart_count = 0

last_scanner_heartbeat = 0
last_telegram_heartbeat = 0

thread_manager_lock = threading.Lock()


# ============================================================
# TELEGRAM SEND
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        logging.error(
            "Telegram variables are missing."
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
# TELEGRAM COMMAND PROCESSOR
# ============================================================

def process_telegram_command(text_message):

    global LAST_MANUAL_SETUP_ID

    if not text_message:
        return

    try:

        if text_message.startswith("/start"):

            send_telegram(
                "🟢 XAUUSD CANDLE EXPERT ONLINE\n\n"
                "Commands:\n"
                "/signal - Full gold analysis\n"
                "/status - Bot status"
            )

        elif text_message.startswith("/status"):

            scanner_alive = (
                scanner_thread is not None
                and scanner_thread.is_alive()
            )

            telegram_alive = (
                telegram_thread is not None
                and telegram_thread.is_alive()
            )

            send_telegram(
                "🟢 RITAMARIAGOLD STATUS\n\n"
                f"Scanner: "
                f"{'🟢 RUNNING' if scanner_alive else '🔴 STOPPED'}\n"
                f"Telegram: "
                f"{'🟢 RUNNING' if telegram_alive else '🔴 STOPPED'}\n"
                f"Symbol: {SYMBOL}\n"
                f"Scan: every {SCAN_SECONDS}s\n"
                f"Minimum score: {MIN_SCORE}\n"
                f"Lot: {LOT_SIZE}\n"
                f"Scanner restarts: {scanner_restart_count}\n"
                f"Telegram restarts: {telegram_restart_count}\n\n"
                "Mode: Candle Structure Expert"
            )

        elif text_message.startswith("/signal"):

            logging.info(
                "Manual /signal received."
            )

            result = analyze_market()

            if not result:

                send_telegram(
                    "⚠️ ANALYSIS FAILED\n\n"
                    "Market data could not be analyzed."
                )

                return

            message = format_analysis_report(
                result
            )

            send_telegram(message)

            setup = result.get("setup")

            if setup:

                LAST_MANUAL_SETUP_ID = setup_id(
                    setup
                )

                logging.info(
                    "Manual signal sent: %s",
                    setup["direction"]
                )

    except Exception as e:

        logging.exception(
            "Command processing error: %s",
            e
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():

    global TELEGRAM_OFFSET
    global last_telegram_heartbeat

    if not TELEGRAM_TOKEN:

        logging.error(
            "TELEGRAM_TOKEN missing."
        )

        return

    logging.info(
        "Telegram polling started."
    )

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

    while not shutdown_event.is_set():

        try:

            last_telegram_heartbeat = time.time()

            response = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    "offset": TELEGRAM_OFFSET,
                    "timeout": 25,
                    "allowed_updates": '["message"]'
                },
                timeout=35
            )

            last_telegram_heartbeat = time.time()

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
                    "Telegram API error: %s",
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
                            "Telegram command: %s",
                            text_message
                        )

                        threading.Thread(
                           
