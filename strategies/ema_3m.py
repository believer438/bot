import time
import threading
import pandas as pd
import traceback
from ta.trend import EMAIndicator
from core.config import symbol
from core.telegram_controller import send_telegram
from core.binance_client import client
from strategies.ema_cross import trade_on_external_signal  # ou adapte si différent

ema_window_short = 20
ema_window_long = 50
_last_signal = None

# Cooldown Telegram
TELEGRAM_COOLDOWN_SECONDS = 60
_telegram_last_sent = 0
_telegram_lock = threading.Lock()

_last_cross_kline_time = None

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

def get_5m_trend():
    try:
        klines_5m = client.get_klines(symbol=symbol, interval="5m", limit=ema_window_long + 10)
        closes_5m = [float(k[4]) for k in klines_5m]
        if len(closes_5m) < ema_window_long:
            print("Pas assez de bougies pour calculer les EMA 5m.")
            return None
        closes_series = pd.Series(closes_5m)
        ema20 = EMAIndicator(closes_series, window=20).ema_indicator()
        ema50 = EMAIndicator(closes_series, window=50).ema_indicator()
        if ema20.iloc[-1] > ema50.iloc[-1]:
            return "bullish"
        elif ema20.iloc[-1] < ema50.iloc[-1]:
            return "bearish"
        return None
    except Exception as e:
        print(f"❌ Erreur get_5m_trend : {e}")
        return None

def get_live_3m_ema_cross():
    try:
        klines = client.get_klines(symbol=symbol, interval="3m", limit=ema_window_long + 10)
        closes_3m = [float(k[4]) for k in klines]  # Utilise toutes les bougies, y compris la dernière
        if len(closes_3m) < ema_window_long:
            print("Pas assez de bougies pour calculer les EMA 3m.")
            return None, None
        closes_series = pd.Series(closes_3m)
        ema20 = EMAIndicator(closes_series, window=ema_window_short).ema_indicator()
        ema50 = EMAIndicator(closes_series, window=ema_window_long).ema_indicator()
        signal = detect_ema_cross(ema20, ema50)
        last_kline_time = int(klines[-1][0])  # timestamp de la dernière bougie (en cours)
        return signal, last_kline_time
    except Exception as e:
        print(f"❌ Erreur get_live_3m_ema_cross : {e}")
        if can_send_telegram():
            send_telegram(f"❌ Erreur EMA 3m : {e}")
        return None, None

def start_ema_3m_loop():
    global _last_signal, _last_cross_kline_time
    _, last_kline_time = get_live_3m_ema_cross()
    _last_cross_kline_time = last_kline_time

    def loop():
        global _last_signal, _last_cross_kline_time
        if can_send_telegram():
            send_telegram("⏰ Boucle EMA 3min + filtre 5min ACTIVÉE")
        while True:
            time.sleep(5)
            signal, cross_kline_time = get_live_3m_ema_cross()
            print(f"[DEBUG] Signal EMA 3m : {signal}, Timestamp : {cross_kline_time}")
            if not signal or cross_kline_time == _last_cross_kline_time:
                print("Aucun nouveau signal ou déjà traité.")
                continue
            trend_5m = get_5m_trend()
            print(f"[DEBUG] Tendance EMA 5m : {trend_5m}")
            # Confirmation stricte de la tendance EMA 5m
            if (signal == "bullish" and trend_5m == "bullish") or (signal == "bearish" and trend_5m == "bearish"):
                try:
                    trade_on_external_signal(signal, source="ema_3m_loop")
                    if can_send_telegram():
                        send_telegram(f"🚦 Trade {signal} confirmé par tendance 5m (EMA 3m)")
                    _last_signal = signal
                    _last_cross_kline_time = cross_kline_time
                except Exception as e:
                    print(f"❌ Erreur lors de la prise de position : {e}")
                    if can_send_telegram():
                        send_telegram(f"❌ Erreur trade EMA 3m : {e}")
                    # NE PAS mettre à jour _last_cross_kline_time ici pour pouvoir retenter

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
