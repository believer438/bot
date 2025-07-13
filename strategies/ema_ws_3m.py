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
    print("✅ WebSocket EMA 3min connecté.")
    if can_send_telegram():
        send_telegram("✅ WebSocket EMA 3min connecté.")

def on_error(ws, error):
    print(f"❌ Erreur WebSocket 3m : {error}")
    if can_send_telegram():
        send_telegram(f"❌ Erreur WebSocket EMA 3m : {error}")

def on_close(ws, close_status_code, close_msg):
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