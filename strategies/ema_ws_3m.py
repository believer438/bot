import json
import threading
import websocket
import pandas as pd
import time
import traceback
from ta.trend import EMAIndicator
from core.config import symbol
from core.telegram_controller import send_telegram
from core.binance_client import client
from strategies.ema_cross import trade_on_external_signal

socket_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@kline_3m"
ema_window_short = 20
ema_window_long = 50
price_lock = threading.Lock()
closes = []
_last_signal = None

# Cooldown Telegram (optionnel)
TELEGRAM_COOLDOWN_SECONDS = 60
_telegram_last_sent = 0
_telegram_lock = threading.Lock()

# État WebSocket 3m
ws_alive = False

def can_send_telegram():
    global _telegram_last_sent
    with _telegram_lock:
        now = time.time()
        if now - _telegram_last_sent > TELEGRAM_COOLDOWN_SECONDS:
            _telegram_last_sent = now
            return True
        return False

def get_5m_trend():
    try:
        klines = client.get_klines(symbol=symbol, interval="5m", limit=60)
        closes_5m = [float(k[4]) for k in klines]
        closes_series = pd.Series(closes_5m)
        ema20_5m = EMAIndicator(closes_series, window=20).ema_indicator()
        ema50_5m = EMAIndicator(closes_series, window=50).ema_indicator()
        if ema20_5m.iloc[-1] > ema50_5m.iloc[-1]:
            return "bullish"
        elif ema20_5m.iloc[-1] < ema50_5m.iloc[-1]:
            return "bearish"
        else:
            return None
    except Exception as e:
        print(f"❌ Erreur get_5m_trend : {e}")
        return None

def detect_ema_cross(ema_short, ema_long):
    if len(ema_short) < 2 or len(ema_long) < 2:
        return None
    if ema_short.iloc[-2] < ema_long.iloc[-2] and ema_short.iloc[-1] > ema_long.iloc[-1]:
        return "bullish"
    elif ema_short.iloc[-2] > ema_long.iloc[-2] and ema_short.iloc[-1] < ema_long.iloc[-1]:
        return "bearish"
    return None

def on_message(ws, message):
    global closes, _last_signal
    try:
        data = json.loads(message)
        candle = data['k']
        close_price = float(candle['c'])

        with price_lock:
            if len(closes) >= ema_window_long:
                closes.pop(0)
            closes.append(close_price)

            if len(closes) < ema_window_long:
                return

            closes_series = pd.Series(closes)
            ema20 = EMAIndicator(closes_series, window=ema_window_short).ema_indicator()
            ema50 = EMAIndicator(closes_series, window=ema_window_long).ema_indicator()

            signal = detect_ema_cross(ema20, ema50)
            if not signal or signal == _last_signal:
                return

            # Filtre par tendance 5min
            trend_5m = get_5m_trend()
            if trend_5m is None:
                return

            if signal == "bullish" and trend_5m == "bullish":
                if can_send_telegram():
                    send_telegram("🚦 Signal 3min : Croisement haussier confirmé par tendance 5min haussière (WebSocket)")
                trade_on_external_signal("bullish", source="ws_3m+trend_5m")
                _last_signal = "bullish"
            elif signal == "bearish" and trend_5m == "bearish":
                if can_send_telegram():
                    send_telegram("🚦 Signal 3min : Croisement baissier confirmé par tendance 5min baissière (WebSocket)")
                trade_on_external_signal("bearish", source="ws_3m+trend_5m")
                _last_signal = "bearish"

    except Exception as e:
        print(f"❌ Erreur on_message (3m): {e}")
        traceback.print_exc()
        if can_send_telegram():
            send_telegram(f"❌ Erreur WebSocket 3m : {e}")

def on_open(ws):
    global ws_alive
    ws_alive = True
    print("✅ WebSocket EMA 3min connecté.")
    if can_send_telegram():
        send_telegram("✅ WebSocket EMA 3min connecté.")

def on_error(ws, error):
    global ws_alive
    ws_alive = False
    print(f"❌ Erreur WebSocket 3m : {error}")
    if can_send_telegram():
        send_telegram(f"❌ Erreur WebSocket EMA 3m : {error}")

def on_close(ws, close_status_code, close_msg):
    global ws_alive
    ws_alive = False
    print("🛑 WebSocket EMA 3min fermé.")
    if can_send_telegram():
        send_telegram("🛑 WebSocket EMA 3min fermé.")

def start_websocket_3m_thread():
    def run_socket():
        ws = websocket.WebSocketApp(
            socket_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        ws.run_forever()

    t = threading.Thread(target=run_socket, daemon=True)
    t.start()
    return t

# === Timer de secours 3m ===

def get_live_3m_ema_cross():
    try:
        klines = client.get_klines(symbol=symbol, interval="3m", limit=ema_window_long + 10)
        closes_3m = [float(k[4]) for k in klines[:-1]]  # exclure dernière bougie incomplète
        last_price = float(client.get_symbol_ticker(symbol=symbol)['price'])
        closes_3m.append(last_price)

        closes_series = pd.Series(closes_3m)
        ema20 = EMAIndicator(closes_series, window=ema_window_short).ema_indicator()
        ema50 = EMAIndicator(closes_series, window=ema_window_long).ema_indicator()
        return detect_ema_cross(ema20, ema50)
    except Exception as e:
        print(f"❌ Erreur get_live_3m_ema_cross : {e}")
        if can_send_telegram():
            send_telegram(f"❌ Erreur Timer secours 3m : {e}")
        return None

def start_backup_timer_3m_loop():
    global _last_signal
    def loop():
        timer_started = False
        while True:
            time.sleep(5)
            if not ws_alive:
                if not timer_started:
                    send_telegram("⏰ Timer de secours EMA 3min ACTIVÉ (WebSocket OFF)")
                    timer_started = True
                signal = get_live_3m_ema_cross()
                if not signal or signal == _last_signal:
                    continue
                trend_5m = get_5m_trend()
                if trend_5m is None:
                    continue
                if (signal == "bullish" and trend_5m == "bullish") or (signal == "bearish" and trend_5m == "bearish"):
                    trade_on_external_signal(signal, source="timer_secours_3m")
                    if can_send_telegram():
                        send_telegram(f"⏰ Timer secours 3m : Trade {signal} confirmé par tendance 5m")
                    _last_signal = signal
            else:
                timer_started = False  # Reset si le WebSocket revient
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t