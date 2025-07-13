import os
import threading
import traceback
import pandas as pd
from ta.trend import EMAIndicator
from core.binance_client import client
from core.trade_interface import open_trade, close_position
from core.trading_utils import get_leverage_from_file
from core.state import state
from core.telegram_controller import send_telegram
import time
from core.config import symbol, ema_interval, ema_lookback

_last_signal_lock = threading.Lock()
_last_signal = None

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

def detect_ema_cross(ema_short, ema_long):
    if ema_short.iloc[-2] < ema_long.iloc[-2] and ema_short.iloc[-1] > ema_long.iloc[-1]:
        return "bullish"
    elif ema_short.iloc[-2] > ema_long.iloc[-2] and ema_short.iloc[-1] < ema_long.iloc[-1]:
        return "bearish"
    else:
        return None

def get_ema_cross():
    try:
        klines = client.get_klines(symbol=symbol, interval=ema_interval, limit=ema_lookback)
        closes = [float(k[4]) for k in klines]
        if len(closes) < 50:
            msg = f"⏳ Pas assez de données EMA ({len(closes)} < 50)"
            print(msg)
            if can_send_telegram():
                send_telegram(msg)
            return None
        closes_series = pd.Series(closes)
        ema20 = EMAIndicator(closes_series, window=20).ema_indicator()
        ema50 = EMAIndicator(closes_series, window=50).ema_indicator()
        # Vérifie que la dernière bougie vient juste de se former (timestamp ou tick)
        # Ici, on suppose que tu appelles cette fonction à chaque nouvelle bougie
        return detect_ema_cross(ema20, ema50)
    except Exception as e:
        err_msg = f"❌ Erreur get_ema_cross : {e}"
        print(err_msg)
        traceback.print_exc()
        if can_send_telegram():
            send_telegram(err_msg)
        return None

def get_live_ema_cross():
    try:
        klines = client.get_klines(symbol=symbol, interval=ema_interval, limit=ema_lookback)
        closes = [float(k[4]) for k in klines[:-1]]  # Exclut la dernière bougie non close
        last_price = float(client.get_symbol_ticker(symbol=symbol)['price'])  # Prix en temps réel
        closes.append(last_price)  # Ajoute le prix live

        closes_series = pd.Series(closes)
        ema20 = EMAIndicator(closes_series, window=20).ema_indicator()
        ema50 = EMAIndicator(closes_series, window=50).ema_indicator()

        return detect_ema_cross(ema20, ema50)
    except Exception as e:
        if can_send_telegram():
            send_telegram(f"❌ Erreur get_live_ema_cross : {e}")
        return None

def execute_ema_cross_strategy():
    global _last_signal
    try:
        cross_signal = get_live_ema_cross()
        with _last_signal_lock:
            if cross_signal is None or cross_signal == _last_signal:
                return
            if state.position_open:
                close_position()
                time.sleep(1)
            open_trade(cross_signal)
            if can_send_telegram():
                send_telegram(f"🚦 Trade {cross_signal.upper()} ouvert par croisement EMA 5min (Live)")
            _last_signal = cross_signal
    except Exception as e:
        if can_send_telegram():
            send_telegram(f"❌ Erreur execute_ema_cross_strategy : {e}")

# === BOUCLE CLASSIQUE À COMMENTER ===

# def ema_strategy_loop():
#     t = threading.current_thread()
#     while getattr(t, "do_run", True):
#         try:
#             execute_ema_cross_strategy()
#         except Exception as e:
#             print(f"❌ Erreur dans la boucle EMA strategy : {e}")
#         time.sleep(1)  # Passe de 10 à 1 seconde

# def start_ema_strategy_thread():
#     thread = threading.Thread(target=ema_strategy_loop, daemon=True)
#     thread.do_run = True
#     thread.start()
#     return thread

def trade_on_external_signal(direction: str, source: str = "multi-tf"):
    global _last_signal
    with _last_signal_lock:
        if state.position_open:
            close_position()  # Ferme la position existante avant d'ouvrir la nouvelle
            time.sleep(1)     # Petite pause pour s'assurer que la fermeture est prise en compte
        open_trade(direction)
        if can_send_telegram():
            send_telegram(f"🚦 Trade {direction.upper()} ouvert par {source}")
        _last_signal = direction
