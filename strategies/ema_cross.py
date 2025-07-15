import os
import time
import json
import threading
import traceback
import pandas as pd
from ta.trend import EMAIndicator
from websocket import WebSocketApp

from core.config import symbol, ema_interval, ema_lookback
from core.binance_client import client
from core.trade_interface import open_trade, close_position
from core.trading_utils import get_leverage_from_file
from core.state import state
from core.telegram_controller import send_telegram

# === États & Verrous ===
_last_signal_lock = threading.Lock()
_last_signal = None

price_lock = threading.Lock()
_ws_last_signal = None
closes = []
ws_alive = False  # ← état WebSocket

# === Cooldown Telegram ===
_telegram_cooldown_lock = threading.Lock()
_telegram_last_sent = 0
TELEGRAM_COOLDOWN_SECONDS = 60

def can_send_telegram():
    global _telegram_last_sent
    with _telegram_cooldown_lock:
        now = time.time()
        if now - _telegram_last_sent > TELEGRAM_COOLDOWN_SECONDS:
            _telegram_last_sent = now
            return True
        return False

# === Détection croisement EMA ===
def detect_ema_cross(ema_short, ema_long):
    if ema_short.iloc[-2] < ema_long.iloc[-2] and ema_short.iloc[-1] > ema_long.iloc[-1]:
        return "bullish"
    elif ema_short.iloc[-2] > ema_long.iloc[-2] and ema_short.iloc[-1] < ema_long.iloc[-1]:
        return "bearish"
    return None

def trade_on_external_signal(direction: str, source: str = "ema_ws_5m"):
    global _last_signal
    with _last_signal_lock:
        if state.position_open:
            close_position()
            time.sleep(1)
        open_trade(direction)
        if can_send_telegram():
            send_telegram(f"🚦 Trade {direction.upper()} ouvert par {source}")
        _last_signal = direction

# === WebSocket Binance EMA 5m ===
ema_window_short = 20
ema_window_long = 50
socket_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@kline_5m"

def ws_detect_ema_cross(ema_short, ema_long):
    if len(ema_short) < 2 or len(ema_long) < 2:
        return None
    if ema_short.iloc[-2] < ema_long.iloc[-2] and ema_short.iloc[-1] > ema_long.iloc[-1]:
        return "bullish"
    elif ema_short.iloc[-2] > ema_long.iloc[-2] and ema_short.iloc[-1] < ema_long.iloc[-1]:
        return "bearish"
    return None

def ws_on_message(ws, message):
    global closes, _ws_last_signal
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

            signal = ws_detect_ema_cross(ema20, ema50)
            if signal and signal != _ws_last_signal:
                trade_on_external_signal(signal, source="ema_ws_5m")
                _ws_last_signal = signal

    except Exception as e:
        print(f"❌ Erreur on_message : {e}")
        traceback.print_exc()
        if can_send_telegram():
            send_telegram(f"❌ Erreur WebSocket message : {e}")

def ws_on_open(ws):
    global ws_alive
    ws_alive = True
    print("✅ WebSocket EMA 5min connecté.")
    if can_send_telegram():
        send_telegram("✅ WebSocket EMA 5min connecté.")

def ws_on_error(ws, error):
    global ws_alive
    ws_alive = False
    print(f"❌ Erreur WebSocket : {error}")
    if can_send_telegram():
        send_telegram(f"❌ Erreur WebSocket EMA : {error}")

def ws_on_close(ws, close_status_code, close_msg):
    global ws_alive
    ws_alive = False
    print("🛑 WebSocket EMA 5min fermé.")
    if can_send_telegram():
        send_telegram("🛑 WebSocket EMA 5min fermé.")

def start_ema_ws_thread():
    def run_socket():
        ws = WebSocketApp(
            socket_url,
            on_open=ws_on_open,
            on_message=ws_on_message,
            on_error=ws_on_error,
            on_close=ws_on_close
        )
        ws.run_forever()

    t = threading.Thread(target=run_socket, daemon=True)
    t.start()
    return t

# === Timer backup EMA (secours) ===
def get_live_ema_cross():
    try:
        klines = client.get_klines(symbol=symbol, interval=ema_interval, limit=ema_lookback)
        closes_data = [float(k[4]) for k in klines[:-1]]
        last_price = float(client.get_symbol_ticker(symbol=symbol)['price'])
        closes_data.append(last_price)

        closes_series = pd.Series(closes_data)
        ema20 = EMAIndicator(closes_series, window=ema_window_short).ema_indicator()
        ema50 = EMAIndicator(closes_series, window=ema_window_long).ema_indicator()
        return detect_ema_cross(ema20, ema50)
    except Exception as e:
        print(f"❌ Erreur EMA Timer : {e}")
        if can_send_telegram():
            send_telegram(f"❌ Erreur EMA Timer : {e}")
        return None

def start_backup_timer_loop():
    def loop():
        global _last_signal
        timer_started = False
        while True:
            time.sleep(5)  # toutes les 5 secondes
            if not ws_alive:
                if not timer_started:
                    if can_send_telegram():
                        send_telegram("⏰ Timer de secours EMA 5min ACTIVÉ (WebSocket OFF)")
                    timer_started = True
                signal = get_live_ema_cross()
                with _last_signal_lock:
                    if signal and signal != _last_signal:
                        trade_on_external_signal(signal, source="ema_timer_backup")
                        if can_send_telegram():
                            send_telegram(f"⏰ Timer secours EMA 5min : Trade {signal} lancé")
                        _last_signal = signal
            else:
                timer_started = False  # Reset si le WebSocket revient
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t