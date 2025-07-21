import time
import json
import threading
import traceback
import pandas as pd
from ta.trend import EMAIndicator

from core.config import symbol, ema_interval, ema_lookback
from core.binance_client import client
from core.trade_interface import open_trade, close_position
from core.trading_utils import get_leverage_from_file
from core.state import state
from core.telegram_controller import send_telegram

# === États & Verrous ===
_last_signal_lock = threading.Lock()
_last_signal = None

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
    if len(ema_short) < 2 or len(ema_long) < 2:
        return None
    if ema_short.iloc[-2] < ema_long.iloc[-2] and ema_short.iloc[-1] > ema_long.iloc[-1]:
        return "bullish"
    elif ema_short.iloc[-2] > ema_long.iloc[-2] and ema_short.iloc[-1] < ema_long.iloc[-1]:
        return "bearish"
    return None

def trade_on_external_signal(direction: str, source: str = "   EMA_loop"):
    global _last_signal
    with _last_signal_lock:
        if state.position_open:
            close_position()
            time.sleep(1)
        open_trade(direction)
        if can_send_telegram():
            send_telegram(f"🚦 Trade {direction.upper()} ouvert par {source}")
        _last_signal = direction

_last_cross_kline_time = None

# === Vérifie croisement EMA via REST ===
def get_live_ema_cross():
    try:
        klines = client.get_klines(symbol=symbol, interval=ema_interval, limit=ema_lookback)
        closes_data = [float(k[4]) for k in klines]  # Utilise toutes les bougies, y compris la dernière
        closes_series = pd.Series(closes_data)
        ema20 = EMAIndicator(closes_series, window=20).ema_indicator()
        ema50 = EMAIndicator(closes_series, window=50).ema_indicator()
        signal = detect_ema_cross(ema20, ema50)
        last_kline_time = int(klines[-1][0])  # timestamp de la dernière bougie (en cours)
        return signal, last_kline_time
    except Exception as e:
        print(f"❌ Erreur EMA Check : {e}")
        if can_send_telegram():
            send_telegram(f"❌ Erreur EMA Check : {e}")
        return None, None

# === Boucle EMA toutes les 5 secondes ===
def start_ema_5m_loop():
    global _last_signal, _last_cross_kline_time
    # Initialisation pour ignorer les croisements passés
    _, last_kline_time = get_live_ema_cross()
    _last_cross_kline_time = last_kline_time

    def loop():
        global _last_signal, _last_cross_kline_time
        print("🟢 Boucle EMA 5m démarrée (vérification toutes les 5s)")
        while True:
            time.sleep(5)
            print("🔄 Vérification EMA 5m en cours...")

            signal, cross_kline_time = get_live_ema_cross()
            if not signal:
                continue

            # Si nouveau croisement sur une nouvelle bougie
            if cross_kline_time != _last_cross_kline_time:
                try:
                    trade_on_external_signal(signal, source="ema_timer_5m")
                    _last_cross_kline_time = cross_kline_time
                    _last_signal = signal
                    if can_send_telegram():
                        send_telegram(f"🚦 Nouveau croisement EMA 5min détecté : {signal.upper()}")
                        print(f"📢 Signal EMA 5min : {signal.upper()} détecté et envoyé.")
                except Exception as e:
                    print(f"❌ Erreur lors de la prise de position : {e}")
                    _last_cross_kline_time = cross_kline_time
            else:
                continue

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

