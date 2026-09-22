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

            send_telegram(
                "🟢 BOT ONLINE\n\n"
                f"Symbol: {SYMBOL}\n"
                f"Scan: every {SCAN_SECONDS}s\n"
                f"Minimum signal score: {MIN_SCORE}\n"
                f"Lot: {LOT_SIZE}\n\n"
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
                "Telegram polling error: %s",
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

        for item in reversed(
            data["values"]
        ):

            try:

                candles.append({
                    "time": item["datetime"],
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"])
                })

            except (
                KeyError,
                ValueError,
                TypeError
            ):

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
            "Twelve Data error: %s",
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

        true_ranges.append(
            max(
                tr1,
                tr2,
                tr3
            )
        )

    if len(true_ranges) < period:

        return None

    recent = true_ranges[-period:]

    return (
        sum(recent)
        / len(recent)
    )


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def bullish_engulfing(
    previous,
    current
):

    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(
    previous,
    current
):

    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
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
        and lower_wick > upper_wick
        and candle["close"] > candle["open"]
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
        and upper_wick > lower_wick
        and candle["close"] < candle["open"]
    )


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(candles):

    if len(candles) < 20:

        return "NEUTRAL"

    recent = candles[-20:]

    highs = [
        candle["high"]
        for candle in recent
    ]

    lows = [
        candle["low"]
        for candle in recent
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
        candle["high"]
        for candle in recent
    )

    previous_low = min(
        candle["low"]
        for candle in recent
    )

    if (
        current["high"] > previous_high
        and current["close"] < previous_high
    ):

        return "BEARISH"

    if (
        current["low"] < previous_low
        and current["close"] > previous_low
    ):

        return "BULLISH"

    return None


# ============================================================
# FULL MARKET ANALYSIS
# ============================================================

def analyze_market():

    if not SCAN_LOCK.acquire(
        blocking=False
    ):

        logging.warning(
            "Another analysis is already running."
        )

        return None

    try:

        logging.info(
            "Starting full market analysis..."
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

        # ====================================================
        # STRUCTURE
        # ====================================================

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

            return None

        price = current_m1["close"]

        # ====================================================
        # M1 CANDLE
        # ====================================================

        m1_engulfing = "NONE"

        if bullish_engulfing(
            previous_m1,
            current_m1
        ):

            m1_engulfing = "BULLISH ENGULFING"

        elif bearish_engulfing(
            previous_m1,
            current_m1
        ):

            m1_engulfing = "BEARISH ENGULFING"

        m1_rejection = "NONE"

        if bullish_rejection(
            current_m1
        ):

            m1_rejection = "BULLISH REJECTION"

        elif bearish_rejection(
            current_m1
        ):

            m1_rejection = "BEARISH REJECTION"

        # ====================================================
        # SCORES
        # ====================================================

        buy_score = 0
        sell_score = 0

        reasons_buy = []
        reasons_sell = []

        # M15

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

        # M5

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

        # Sweep

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

        # Engulfing

        if m1_engulfing == "BULLISH ENGULFING":

            buy_score += 20

            reasons_buy.append(
                "M1 bullish engulfing"
            )

        elif m1_engulfing == "BEARISH ENGULFING":

            sell_score += 20

            reasons_sell.append(
                "M1 bearish engulfing"
            )

        # Rejection

        if m1_rejection == "BULLISH REJECTION":

            buy_score += 15

            reasons_buy.append(
                "M1 bullish rejection"
            )

        elif m1_rejection == "BEARISH REJECTION":

            sell_score += 15

            reasons_sell.append(
                "M1 bearish rejection"
            )

        # Displacement

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

        # ====================================================
        # OVERALL DIRECTION
        # ====================================================

        if (
            m15_structure == "BULLISH"
            and m5_structure == "BULLISH"
        ):

            overall = "STRONG BULLISH"

        elif (
            m15_structure == "BEARISH"
            and m5_structure == "BEARISH"
        ):

            overall = "STRONG BEARISH"

        elif (
            m15_structure == "BULLISH"
            or m5_structure == "BULLISH"
        ):

            overall = "BULLISH / MIXED"

        elif (
            m15_structure == "BEARISH"
            or m5_structure == "BEARISH"
        ):

            overall = "BEARISH / MIXED"

        else:

            overall = "NEUTRAL / RANGE"

        # ====================================================
        # SELECT SIGNAL
        # ====================================================

        setup = None

        if (
            buy_score >= MIN_SCORE
            and buy_score > sell_score
        ):

            direction = "BUY"
            score = buy_score
            reasons = reasons_buy

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
                "direction": direction,
                "score": score,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "atr": atr,
                "reasons": reasons,
                "candle_time": current_m1["time"]
            }

        elif (
            sell_score >= MIN_SCORE
            and sell_score > buy_score
        ):

            direction = "SELL"
            score = sell_score
            reasons = reasons_sell

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
                "direction": direction,
                "score": score,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "atr": atr,
                "reasons": reasons,
                "candle_time": current_m1["time"]
            }

        return {
            "price": price,
            "atr": atr,
            "m15": m15_structure,
            "m5": m5_structure,
            "overall": overall,
            "sweep": sweep or "NONE",
            "m1_engulfing": m1_engulfing,
            "m1_rejection": m1_rejection,
            "displacement": displacement,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "candle_time": current_m1["time"],
            "setup": setup
        }

    finally:

        SCAN_LOCK.release()


# ============================================================
# FULL ANALYSIS MESSAGE
# ============================================================

def format_analysis_report(result):

    overall = result["overall"]

    if (
        "BULLISH" in overall
        and "BEARISH" not in overall
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

        f"💰 PRICE: {result['price']:.2f}\n"
        f"📊 OVERALL: "
        f"{overall_emoji} {overall}\n\n"

        "🏦 MARKET STRUCTURE\n"
        f"• M15: {result['m15']}\n"
        f"• M5:  {result['m5']}\n"
        f"• Liquidity Sweep: {result['sweep']}\n\n"

        "🕯 M1 CANDLE\n"
        f"• Engulfing: {result['m1_engulfing']}\n"
        f"• Rejection: {result['m1_rejection']}\n"
        f"• Displacement: {result['displacement']}\n\n"

        "📈 SCORE\n"
        f"• BUY:  {result['buy_score']}/80+\n"
        f"• SELL: {result['sell_score']}/80+\n"
        f"• Required: {MIN_SCORE}\n\n"

        f"📏 M5 ATR: {result['atr']:.2f}\n"
        f"🕐 Candle: {result['candle_time']}\n\n"
    )

    # ========================================================
    # SIGNAL
    # ========================================================

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
            f"🚨 SIGNAL: {emoji} {direction}\n"
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

        # ====================================================
        # NO TRADE EXPLANATION
        # ====================================================

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
            > result["sell_score"]
        ):

            candidate = "BUY"

        elif (
            result["sell_score"]
            > result["buy_score"]
        ):

            candidate = "SELL"

        else:

            candidate = "NONE"

        message += (
            "━━━━━━━━━━━━━━━━━━\n"
            "⏳ NO TRADE\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"Current candidate: {candidate}\n"
            f"Highest score: {highest_score}\n"
            f"Points needed: {missing}\n\n"

            "Reason:\n"
            "The complete candle structure "
            "has not reached the required "
            f"{MIN_SCORE}-point confirmation.\n\n"

            "🛑 NO ENTRY\n"
            "🛑 NO SL\n"
            "🛑 NO TP\n\n"

            "The bot is waiting for stronger "
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
# AUTOMATIC SCANNER
# ============================================================

def scanner():

    global LAST_AUTO_SETUP_ID

    logging.info(
        "Gold scanner started."
    )

    while True:

        cycle_start = time.time()

        try:

            result = analyze_market()

            if result:

                setup = result.get(
                    "setup"
                )

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

                        if send_telegram(
                            message
                        ):

                            LAST_AUTO_SETUP_ID = (
                                current_id
                            )

                            logging.info(
                                "AUTO SIGNAL SENT: %s",
                                setup["direction"]
                            )

                    else:

                        logging.info(
                            "Duplicate setup ignored."
                        )

                else:

                    logging.info(
                        "No high-quality setup."
                    )

        except Exception as e:

            logging.exception(
                "SCANNER ERROR: %s",
                e
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

        while remaining > 0:

            sleep_time = min(
                5,
                remaining
            )

            time.sleep(
                sleep_time
            )

            remaining -= sleep_time


# ============================================================
# FLASK
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify({
        "status": "online",
        "bot": "XAUUSD Candle Expert",
        "symbol": SYMBOL,
        "scan_seconds": SCAN_SECONDS,
        "minimum_score": MIN_SCORE
    })


@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify({
        "status": "healthy"
    })


# ============================================================
# WEB MANUAL SIGNAL
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
                "status": "analysis_failed"
            }), 500

        message = format_analysis_report(
            result
        )

        if send_telegram(message):

            setup = result.get(
                "setup"
            )

            if setup:

                LAST_MANUAL_SETUP_ID = (
                    setup_id(setup)
                )

            return jsonify({
                "status": "analysis_sent",
                "has_signal": bool(setup)
            })

        return jsonify({
            "status": "telegram_failed"
        }), 500

    except Exception as e:

        logging.exception(
            "Manual signal error: %s",
            e
        )

        return jsonify({
            "status": "error",
            "message": str(e)
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
            "Webhook error: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# START BACKGROUND SERVICES
# ============================================================

def start_background_services():

    scanner_thread = threading.Thread(
        target=scanner,
        name="gold-scanner",
        daemon=True
    )

    scanner_thread.start()

    telegram_thread = threading.Thread(
        target=telegram_polling,
        name="telegram-polling",
        daemon=True
    )

    telegram_thread.start()

    logging.info(
        "Background services started."
    )


start_background_services()


# ============================================================
# RUN
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
