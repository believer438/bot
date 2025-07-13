import json
import threading
import websocket
import pandas as pd
import time
import traceback
from ta.trend import EMAIndicator
from core.state import state
from core.telegram_controller import send_telegram
from core.config import symbol
from strategies.ema_cross import trade_on_external_signal

# === CONFIGURATION ===
socket_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@kline_5m"
ema_window_short = 20
ema_window_long = 50
price_lock = threading.Lock()
closes = []
_last_signal = None

# === TELEGRAM COOLDOWN ===
TELEGRAM_COOLDOWN_SECONDS = 60
_telegram_last_sent = 0
_telegram_lock = threading.Lock()

def can_send_telegram():
    global _telegram_last_sent
    with _telegram_lock:
        now = time.time()
        if now - _telegram_last_sent > TELEGRAM_COOLDOWN_SECONDS:
            _telegram_last_sent = now
            return True
        return False

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
            if signal and signal != _last_signal:
                trade_on_external_signal(signal, source="ema_ws_5m")
                _last_signal = signal

    except Exception as e:
        print(f"❌ Erreur on_message : {e}")
        traceback.print_exc()
        if can_send_telegram():
            send_telegram(f"❌ Erreur WebSocket message : {e}")

def on_open(ws):
    print("✅ WebSocket EMA 5min connecté.")
    if can_send_telegram():
        send_telegram("✅ WebSocket EMA 5min connecté.")

def on_error(ws, error):
    print(f"❌ Erreur WebSocket : {error}")
    if can_send_telegram():
        send_telegram(f"❌ Erreur WebSocket EMA : {error}")

def on_close(ws, close_status_code, close_msg):
    print("🛑 WebSocket EMA 5min fermé.")
    if can_send_telegram():
        send_telegram("🛑 WebSocket EMA 5min fermé.")

def start_ema_ws_thread():
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
