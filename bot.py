import os, time, threading, logging, requests
from flask import Flask, request
from datetime import datetime
import pytz

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY")
BEIRUT_TZ = pytz.timezone("Asia/Beirut")

app = Flask(__name__)
LAST_ALERT_TIME = 0
logging.basicConfig(level=logging.INFO)

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logging.error(f"telegram {e}")

def is_trading_window():
    now = datetime.now(BEIRUT_TZ)
    h = now.hour
    if (10 <= h < 12) or (14 <= h < 18):
        return True
    return False

def get_xau_data(interval):
    try:
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={interval}&apikey={TWELVE_DATA_KEY}&outputsize=100"
        r = requests.get(url, timeout=10).json()
        return r.get("values")
    except:
        return None

def analyze_v2(m5, m15):
    # TODO: put your real Pin Bar + LH logic here
    # dummy scoring for deploy test
    import random
    score = random.randint(40, 85)
    price = float(m5[0]["close"]) if m5 else 4350.0
    if score >= 70:
        sig = "SELL" if score % 2 == 0 else "BUY"
        sl = price + 5 if sig == "SELL" else price - 5
        tp1 = price - 8 if sig == "SELL" else price + 8
        tp2 = price - 15 if sig == "SELL" else price + 15
        return sig, price, sl, tp1, f"Test Signal Score {score}", tp2, score
    else:
        return "NO_TRADE", 0, 0, 0, f"NO TRADE Score {score}", 0, score

def auto_scanner():
    global LAST_ALERT_TIME
    logging.info("ritamariagold V2 STRONG ONLY running - scanner ON")
    while True:
        try:
            time.sleep(300)
            if not is_trading_window():
                continue
            m5 = get_xau_data("5min")
            m15 = get_xau_data("15min")
            if not m5 or not m15:
                continue
            res = analyze_v2(m5, m15)
            if res[0] in ["BUY", "SELL"]:
                sig, entry, sl, tp1, reason, tp2, score = res
                if score < 70:
                    continue
                if time.time() - LAST_ALERT_TIME < 3600:
                    continue
                LAST_ALERT_TIME = time.time()
                text = f"🔔🔔🔔 STRONG {sig} {score}/100\nEntry ${entry:.2f} SL ${sl:.2f} TP1 ${tp1:.2f} TP2 ${tp2:.2f}\n{reason}\nLot 0.01 ONLY"
                send_message(TELEGRAM_CHAT_ID, text)
        except Exception as e:
            logging.error(f"scanner {e}")
            time.sleep(60)

threading.Thread(target=auto_scanner, daemon=True).start()

@app.route("/")
def home():
    return "ritamariagold V2 STRONG ONLY running"
@app.route("/telegram", methods=["POST"])
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    if "message" in data and "text" in data["message"]:
        if "/signal" in data["message"]["text"]:
            m5 = get_xau_data("5min")
            m15 = get_xau_data("15min")
            res = analyze_v2(m5, m15)
            if res[0] in ["BUY","SELL"]:
                sig, entry, sl, tp1, reason, tp2, score = res
                msg = f"{sig} {score} Entry ${entry:.2f} SL ${sl:.2f} TP ${tp1:.2f}"
            else:
                msg = res[4]
            send_message(data["message"]["chat"]["id"], msg)
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
