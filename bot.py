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

TD_URL = "https://api.twelvedata.com/time_series"

LAST_AUTO_SETUP_ID = None
LAST_MANUAL_SETUP_ID = None

# Last Telegram chat that contacted the bot.
# This lets /signal and /status work even if
# TELEGRAM_CHAT_ID is temporarily unavailable.
LAST_TELEGRAM_CHAT_ID = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

app = Flask(__name__)


# ============================================================
# ENVIRONMENT
# ============================================================

def get_env(name):
    value = os.getenv(name)
    if value:
        return value.strip()
    return None


def telegram_token():
    return get_env("TELEGRAM_TOKEN")


def telegram_chat_id():
    return get_env("TELEGRAM_CHAT_ID")


def twelve_data_key():
    return get_env("TWELVE_DATA_KEY")


def render_url():
    value = get_env("RENDER_EXTERNAL_URL")

    if value:
        return value.rstrip("/")

    # Render normally exposes this automatically.
    value = get_env("RENDER_EXTERNAL_HOSTNAME")

    if value:
        return "https://" + value.rstrip("/")

    return None


# ============================================================
# TELEGRAM
# ============================================================

def tg_send(text, chat_id=None):

    token = telegram_token()

    if not token:
        logging.error(
            "Telegram token missing from environment."
        )
        return False

    target_chat = (
        chat_id
        or telegram_chat_id()
        or LAST_TELEGRAM_CHAT_ID
    )

    if not target_chat:
        logging.error(
            "Telegram chat ID unavailable."
        )
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            json={
                "chat_id": target_chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not response.ok:

            logging.error(
                "Telegram HTTP error %s: %s",
                response.status_code,
                response.text[:500],
            )

            return False

        data = response.json()

        if not data.get("ok"):

            logging.error(
                "Telegram API error: %s",
                response.text[:500],
            )

            return False

        logging.info(
            "Telegram message sent successfully."
        )

        return True

    except Exception as e:

        logging.exception(
            "Telegram send failed: %s",
            e,
        )

        return False


def telegram_env_check():

    token = telegram_token()
    chat = telegram_chat_id()
    td = twelve_data_key()

    logging.info(
        "ENV CHECK | TELEGRAM_TOKEN=%s | "
        "TELEGRAM_CHAT_ID=%s | TWELVE_DATA_KEY=%s",
        "OK" if token else "MISSING",
        "OK" if chat else "MISSING",
        "OK" if td else "MISSING",
    )


def set_webhook():

    token = telegram_token()
    url_base = render_url()

    if not token:

        logging.error(
            "Cannot set Telegram webhook: "
            "TELEGRAM_TOKEN missing."
        )

        return False

    if not url_base:

        logging.warning(
            "Cannot set webhook: Render URL unavailable."
        )

        return False

    webhook_url = (
        f"{url_base}/telegram"
    )

    telegram_url = (
        f"https://api.telegram.org/"
        f"bot{token}/setWebhook"
    )

    try:

        response = requests.post(
            telegram_url,
            json={
                "url": webhook_url
            },
            timeout=15,
        )

        logging.info(
            "Webhook result: %s",
            response.text[:500],
        )

        return response.ok

    except Exception as e:

        logging.exception(
            "Webhook setup failed: %s",
            e,
        )

        return False


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(interval, size=220):

    key = twelve_data_key()

    if not key:
        raise RuntimeError(
            "Missing TWELVE_DATA_KEY"
        )

    params = {
        "symbol": SYMBOL,
        "interval":
