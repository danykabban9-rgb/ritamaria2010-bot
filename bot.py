# app.py - ritamariagold FINAL - 6 trades/week - strong signal only
import os, requests, logging, threading, time
from datetime import datetime, timezone
from flask import Flask, request
import pytz

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

BEIRUT = pytz.timezone('Asia/Beirut')
NEWS_CACHE = {"time": 0, "events": []}
LAST_ALERT_TIME = 0

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logging.error(e)

def is_trading_window():
    now = datetime.now(BEIRUT)
    if now.weekday() > 4:
        return False, "Weekend"
    h = now.hour + now.minute/60
    if (10 <= h < 12) or (14 <= h < 18):
        return True, f"OPEN {now.strftime('%H:%M')}"
    return False, f"Outside 10-12 / 14-18 ({now.strftime('%H:%M')})"

def get_news():
    if time.time() - NEWS_CACHE["time"] < 1800 and NEWS_CACHE["events"]:
        return NEWS_CACHE["events"]
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        data = r.json()
        high = [e for e in data if e.get("impact")=="High" and e.get("currency")=="USD"]
        NEWS_CACHE["time"]=time.time(); NEWS_CACHE["events"]=high
        return high
    except:
        return []

def is_news_block():
    now_utc = datetime.now(timezone.utc)
    for ev in get_news():
        try:
            ev_t = datetime.fromisoformat(ev["date"].replace("Z","+00:00"))
            diff = (ev_t - now_utc).total_seconds()/60
            if -30 <= diff <= 60:
                return True, f"RED NEWS {ev.get('title')} in {int(diff)}min - NO TRADE"
        except: pass
    return False, "No news"

def get_xau_data(interval):
    try:
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={interval}&apikey={TWELVE_API_KEY}&outputsize=100"
        r = requests.get(url, timeout=10).json()
        return r.get("values", [])[::-1]
    except: return []

def calc_atr(data, n=14):
    try:
        trs=[]
        for i in range(1, len(data)):
            h=float(data[i]["high"]); l=float(data[i]["low"]); pc=float(data[i-1]["close"])
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        return sum(trs[-n:])/n if len(trs)>=n else None
    except: return None

def analyze_v2(m5, m15):
    can, win_msg = is_trading_window()
    if not can: return ("NO TRADE", win_msg)
    blocked, news_msg = is_news_block()
    if blocked: return ("NO TRADE", news_msg)
    if not m5 or not m15: return ("NO TRADE", "No data")

    atr = calc_atr(m5, 14)
    if not atr or not (0.5 <= atr <= 8.0):
        return ("NO TRADE", f"ATR {atr} unsafe")

    # pattern
    last = m5[-1]
    body = abs(float(last["close"])-float(last["open"]))
    wick = float(last["high"])-float(last["low"])
    pat = "No pattern"; pat_score=0; signal_dir=None
    if wick > 0 and body*2.5 < wick:
        if float(last["close"]) < float(last["open"]):
            pat="Pin Bar Bearish"; pat_score=30; signal_dir="SELL"
        else:
            pat="Pin Bar Bullish"; pat_score=30; signal_dir="BUY"

    # structure
    highs=[float(c["high"]) for c in m15[-10:]]; lows=[float(c["low"]) for c in m15[-10:]]
    struct="Chop"; struct_score=0
    if highs[-1] < max(highs[:-1]): struct="LH"; struct_score=30
    if lows[-1] > min(lows[:-1]): struct="HL"; struct_score=30

    if pat_score==0 or struct_score==0:
        return ("NO TRADE", f"Score {pat_score+struct_score}/100 - {pat} + {struct}")

    # trend filter EMA50
    closes=[float(c["close"]) for c in m15[-50:]]
    ema50=sum(closes[-50:])/50
    price=closes[-1]
    trend_score=0; trend_msg="No trend"
    if signal_dir=="SELL" and price < ema50: trend_score=20; trend_msg="With downtrend"
    if signal_dir=="BUY" and price > ema50: trend_score=20; trend_msg="With uptrend"
    if trend_score==0:
        return ("NO TRADE", f"Against trend - SKIP")

    score = pat_score + struct_score + trend_score + 10 + 10 # atr health + news passed
    if score < 70:
        return ("NO TRADE", f"Score {score}/100 - {pat} + {struct} + {trend_msg}")

    buffer = 0.80
    entry = float(m5[-1]["close"])
    if signal_dir=="SELL":
        sl = entry + atr*1.0 + buffer
        tp1 = entry - atr*1.0
        tp2 = entry - atr*2.0
    else:
        sl = entry - atr*1.0 - buffer
        tp1 = entry + atr*1.0
        tp2 = entry + atr*2.0

    reason = f"{pat} + {struct} + {trend_msg} | ATR {atr:.2f} + $0.80 buffer | Score {score}/100"
    return (signal_dir, entry, sl, tp1, tp2, reason, score)

def auto_scanner():
    global LAST_ALERT_TIME
    while True:
        try:
            time.sleep(300) # check every 5 min
            can,_ = is_trading_window()
            if not can: continue
            blocked,_ = is_news_block()
            if blocked: continue
            m5=get_xau_data("5min"); m15=get_xau_data("15min")
            res = analyze_v2(m5, m15)
            if res[0] in ["BUY","SELL"] and len(res)==7:
                signal, entry, sl, tp1, tp2, reason, score = res
                if time.time() - LAST_ALERT_TIME < 3600: continue
                if score >= 70:
                    LAST_ALERT_TIME=time.time()
                    text = f"🔔🔔🔔 *STRONG SIGNAL {score}/100*\n*{signal} XAU/USD*\nEntry: ${entry:.2f}\nSL: ${sl:.2f} (1.0x ATR + $0.80)\nTP1: ${tp1:.2f} -> CLOSE 50% + MOVE SL TO BE\nTP2: ${tp2:.2f}\n\n_{reason}_\n\nLot: 0.01 ONLY"
                    send_message(TELEGRAM_CHAT_ID, text)
        except Exception as e:
            logging.error(f"scanner {e}"); time.sleep(60)

threading.Thread(target=auto_scanner, daemon=True).start()

@app.route("/webhook", methods=["POST"])
def webhook():
    data=request.get_json()
    if "message" in data and "/signal" in data["message"].get("text",""):
        m5=get_xau_data("5min"); m15=get_xau_data("15min")
        res=analyze_v2(m5, m15)
        if res[0] in ["BUY","SELL"]:
            signal, entry, sl, tp1, tp2, reason, score = res
            txt=f"*{signal} {score}/100*\nEntry ${entry:.2f}\nSL ${sl:.2f}\nTP1 ${tp1:.2f}\nTP2 ${tp2:.2f}\n_{reason}_"
        else:
            txt=f"⚪ NO TRADE\n{res[1]}"
        send_message(data["message"]["chat"]["id"], txt)
    return "ok",200

@app.route("/")
def home(): return "ritamariagold V2 STRONG ONLY running",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",10000)))
