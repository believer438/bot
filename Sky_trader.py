import os
import time
import csv
import numpy as np
import pandas as pd
from datetime import datetime
from binance.client import Client
from binance.enums import *
from binance.enums import ORDER_TYPE_TAKE_PROFIT_MARKET, ORDER_TYPE_STOP_MARKET
from ta.trend import EMAIndicator
from dotenv import load_dotenv
import requests
import datetime
import traceback
import threading

# === CHARGEMENT DES VARIABLES D'ENVIRONNEMENT ===
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === INITIALISATION BINANCE ===
client = Client(API_KEY, API_SECRET)

# === PARAMÈTRES DU BOT ===
symbol = "ALGOUSDT"
leverage = 10
quantity_usdt = 10
stop_loss_pct = 0.006          # SL initial 0.6%
take_profit_pct = 0.015        # TP initial 1.5%
interval = Client.KLINE_INTERVAL_5MINUTE
lookback = 100

# === FICHIER DE MODE (auto ou alert) ===
mode_file = "mode.txt"

# === FICHIER JOURNAL ===
log_file = "logs.csv"
if not os.path.exists(log_file):
    with open(log_file, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Date", "Direction", "Entry Price", "Stop Loss", "Take Profit", "Mode", "Status"])

position_open = False
current_direction = None
current_entry_price = None

# === THREADS GLOBAUX POUR SUIVI DES POSITIONS ===
trailing_thread = None
tp_thread = None

# === ENVOI DE MESSAGES TELEGRAM (amélioré pour éviter perte d’info) ===
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Erreur Telegram :", e)
        traceback.print_exc()

# === LIRE LE MODE DE FONCTIONNEMENT (auto ou alert) avec notification si défaut ===
def get_mode():
    try:
        with open(mode_file, "r") as file:
            mode = file.read().strip().lower()
            if mode not in ["auto", "alert"]:
                send_telegram("⚠️ Fichier mode.txt corrompu, passage en mode AUTO par défaut.")
                return "auto"
            return mode
    except Exception as e:
        send_telegram("⚠️ Fichier mode.txt absent, passage en mode AUTO par défaut.")
        return "auto"

# === DÉTECTION DU SIGNAL DE CROISEMENT EMA20/EMA50 ===
def get_ema_cross():
    klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
    closes = [float(k[4]) for k in klines]
    if len(closes) < 50:
        return None
    closes_series = pd.Series(closes)
    ema20 = EMAIndicator(closes_series, window=20).ema_indicator()
    ema50 = EMAIndicator(closes_series, window=50).ema_indicator()
    if ema20.iloc[-2] < ema50.iloc[-2] and ema20.iloc[-1] > ema50.iloc[-1]:
        return "bullish"
    elif ema20.iloc[-2] > ema50.iloc[-2] and ema20.iloc[-1] < ema50.iloc[-1]:
        return "bearish"
    return None

# === CALCUL DE LA QUANTITÉ DE L'ORDRE SELON LE PRIX D'ENTRÉE (vérification ajoutée) ===
def calculate_quantity(entry_price):
    # Récupère le solde USDT disponible sur le compte Futures
    balance = client.futures_account_balance()
    usdt_balance = 0
    for asset in balance:
        if asset['asset'] == 'USDT':
            usdt_balance = float(asset['balance'])
            break
    # Utilise 100% du solde disponible
    qty = round((usdt_balance * leverage) / entry_price, 1)
    if qty <= 0:
        send_telegram("❌ Quantité calculée nulle ou négative, aucun ordre envoyé.")
        raise ValueError("Quantité calculée nulle ou négative")
    return qty

# === ENREGISTREMENT DANS LE JOURNAL DES TRADES ===
def log_trade(direction, entry_price, sl, tp, mode, status="OUVERT"):
    try:
        with open(log_file, mode="a", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                direction.upper(),
                round(entry_price, 4),
                round(sl, 4),
                round(tp, 4),
                mode.upper(),
                status
            ])
    except Exception as e:
        print("Erreur écriture log :", e)
        
# === TRAILING STOP LOSS PROGRESSIF ===
def get_trailing_sl(entry_price, current_price, direction):
    gain_pct = (current_price - entry_price) / entry_price if direction == "bullish" else (entry_price - current_price) / entry_price
    sl_level = None
    levels = [
        (0.005, 0.002),  # à 0.5% gain, SL à 0.2%
        (0.006, 0.003),
        (0.010, 0.005),
        (0.012, 0.006),
        (0.015, 0.010),
    ]
    for threshold, sl in levels:
        if gain_pct >= threshold:
            sl_level = sl
    if gain_pct >= 0.015:
        extra_gain = gain_pct - 0.015
        steps = int(extra_gain / 0.005)
        sl_level = 0.010 + steps * 0.005
    if sl_level:
        if direction == "bullish":
            return round(entry_price * (1 + sl_level), 4)
        else:
            return round(entry_price * (1 - sl_level), 4)
    return None

# === TRAILING TAKE PROFIT PROGRESSIF ===
def get_trailing_tp(entry_price, current_price, direction, current_tp_pct):
    gain_pct = (current_price - entry_price) / entry_price if direction == "bullish" else (entry_price - current_price) / entry_price
    tp_levels = [
        (0.012, 0.02),    # à 1.2% gain, TP à 2.0%
        (0.018, 0.025),   # à 1.8% gain, TP à 2.5%
    ]
    new_tp_pct = take_profit_pct
    for threshold, new_tp in tp_levels:
        if gain_pct >= threshold:
            new_tp_pct = new_tp
    if gain_pct > 0.018:
        extra_gain = gain_pct - 0.018
        steps = int(extra_gain / 0.005)
        new_tp_pct = 0.025 + steps * 0.005
    if new_tp_pct > current_tp_pct:
        return new_tp_pct
    return None

# === MISE À JOUR TRAILING SL ET TP EN CONTINU ===
def update_trailing_sl_and_tp(direction, entry_price):
    try:
        max_gain_pct_notified = 0 
        current_sl = None
        current_tp_pct = take_profit_pct 
        while True:
            price = float(client.futures_mark_price(symbol=symbol)["markPrice"])

            # Calcul du gain en %
            gain_pct = (price - entry_price) / entry_price * 100 if direction == "bullish" else (entry_price - price) / entry_price * 100

            # 🔔 Notifications par paliers
            next_threshold = max_gain_pct_notified + 1
            if gain_pct >= next_threshold:
                max_gain_pct_notified = next_threshold
                send_telegram(f"📊 Gain de +{next_threshold:.0f}% atteint sur position {direction.upper()} (Prix actuel : {price}$)")
                
        # Calcul du gain en % (réel sans effet de levier)
        gain_pct = (price - entry_price) / entry_price * 100 if direction == "bullish" else (entry_price - price) / entry_price * 100

        # Envoie une notification une seule fois quand gain atteint 1%
        if gain_pct >= 1 and not hasattr(update_trailing_sl_and_tp, "notified_1_percent"):
            send_telegram(f"📈 Position {direction.upper()} a atteint +1% de gain sur le marché (soit {gain_pct:.2f}%)")
            update_trailing_sl_and_tp.notified_1_percent = True
    
        try:
            current_price = float(client.futures_mark_price(symbol=symbol)["markPrice"])

            # Mise à jour du SL
            new_sl = get_trailing_sl(entry_price, current_price, direction)
            if new_sl and (current_sl is None or (direction == "bullish" and new_sl > current_sl) or (direction == "bearish" and new_sl < current_sl)):
                client.futures_create_order(
                    symbol=symbol,
                    side=SIDE_SELL if direction == "bullish" else SIDE_BUY,
                    type=ORDER_TYPE_STOP_MARKET,
                    stopPrice=new_sl,
                    closePosition=True,
                    timeInForce="GTC"
                )
                current_sl = new_sl
                send_telegram(f"🔄 Stop Loss mis à jour à {new_sl}$")

            # Mise à jour du TP
            new_tp_pct = get_trailing_tp(entry_price, current_price, direction, current_tp_pct)
            if new_tp_pct and ((direction == "bullish" and new_tp_pct > current_tp_pct) or (direction == "bearish" and new_tp_pct < current_tp_pct)):
                new_tp_price = round(entry_price * (1 + new_tp_pct), 4) if direction == "bullish" else round(entry_price * (1 - new_tp_pct), 4)
                client.futures_create_order(
                    symbol=symbol,
                    side=SIDE_SELL if direction == "bullish" else SIDE_BUY,
                    type=ORDER_TYPE_TAKE_PROFIT_MARKET,
                    stopPrice=new_tp_price,
                    closePosition=True,
                    timeInForce="GTC"
                )
                current_tp_pct = new_tp_pct
                send_telegram(f"🔄 Take Profit mis à jour à {new_tp_price}$")

            time.sleep(15)
            
            
        except Exception as e:
            print("Erreur trailing SL/TP :", e)
            time.sleep(15)
    except Exception as e:
        print("Erreur générale dans update_trailing_sl_and_tp :", e)
        time.sleep(15)

# === MISE À JOUR DU JOURNAL QUAND POSITION FERMÉE ===
def update_trade_status(entry_price, new_status):
    try:
        lines = []
        with open(log_file, "r", newline="") as file:
            reader = csv.reader(file)
            headers = next(reader)
            for row in reader:
                if row[2] == str(round(entry_price, 4)) and row[6] == "OUVERT":
                    row[6] = new_status
                lines.append(row)
        with open(log_file, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            writer.writerows(lines)
    except Exception as e:
        print("Erreur MAJ log :", e)

# === SURVEILLANCE DU TAKE PROFIT ===
def wait_for_tp_or_exit(direction, entry_price, tp):
    try:
        while True:
            price = float(client.futures_mark_price(symbol=symbol)["markPrice"])
            if (direction == "bullish" and price >= tp) or (direction == "bearish" and price <= tp):
                update_trade_status(entry_price, "FERMÉ - TP")
                send_telegram(f"✅ Take Profit atteint à {price}$")
                break
            time.sleep(15)
    except Exception as e:
        print("❌ Erreur TP Check :", e)

# === OUVERTURE D'UNE POSITION (gestion des threads et logs détaillés) ===

def open_trade(direction):
    global position_open, current_direction, current_entry_price, trailing_thread, tp_thread

    if position_open:
        return

    price = float(client.get_symbol_ticker(symbol=symbol)["price"])
    try:
        qty = calculate_quantity(price)
    except Exception as e:
        print("Erreur quantité :", e)
        traceback.print_exc()
        return

    side = SIDE_BUY if direction == "bullish" else SIDE_SELL
    stop_price = round(price * (1 - stop_loss_pct), 4) if direction == "bullish" else round(price * (1 + stop_loss_pct), 4)
    take_profit = round(price * (1 + take_profit_pct), 4) if direction == "bullish" else round(price * (1 - take_profit_pct), 4)

    try:
        mode = get_mode()
        if mode == "alert":
            msg = f"⚠ Mode ALERTE: Signal {direction.upper()} détecté sur {symbol} à {price}$"
            send_telegram(msg)
            print(msg)
            return

        client.futures_create_order(symbol=symbol, side=side, type=ORDER_TYPE_MARKET, quantity=qty)
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if direction == "bullish" else SIDE_BUY,
            type=ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=take_profit,
            closePosition=True,
            timeInForce="GTC"
        )
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if direction == "bullish" else SIDE_BUY,
            type=ORDER_TYPE_STOP_MARKET,
            stopPrice=stop_price,
            closePosition=True,
            timeInForce="GTC"
        )

        msg = f"✅ POSITION OUVERTE : {direction.upper()} sur {symbol} à {price}$\nTP: {take_profit}$ | SL: {stop_price}$ | Levier: x{leverage}"
        send_telegram(msg)
        print(msg)

        log_trade(direction, price, stop_price, take_profit, mode)

        position_open = True
        current_direction = direction
        current_entry_price = price

        # Arrêt des anciens threads si existants
        if trailing_thread and trailing_thread.is_alive():
            trailing_thread.do_run = False
        if tp_thread and tp_thread.is_alive():
            tp_thread.do_run = False

        # Lancement des nouveaux threads
        trailing_thread = threading.Thread(target=update_trailing_sl_and_tp, args=(direction, price), daemon=True)
        tp_thread = threading.Thread(target=wait_for_tp_or_exit, args=(direction, price, take_profit), daemon=True)
        trailing_thread.start()
        tp_thread.start()

    except Exception as e:
        err_msg = f"❌ Erreur lors de la création de la position : {e}"
        print(err_msg)
        traceback.print_exc()
        send_telegram(err_msg)

# === FERME LAPOSITION AU PROCHAIN CROISEMENT ===

def close_position():
    global position_open, current_direction, current_entry_price, trailing_thread, tp_thread
    try:
        if not position_open:
            return

        side = SIDE_SELL if current_direction == "bullish" else SIDE_BUY
        price = float(client.get_symbol_ticker(symbol=symbol)["price"])
        qty = calculate_quantity(price)
        client.futures_create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )
        send_telegram(f"⚠ Position fermée avant nouveau croisement à {price}$")
        print(f"Position fermée avant nouveau croisement à {price}$")

        update_trade_status(current_entry_price, "FERMÉ - FERME MANUELLEMENT")

        # Arrêt des threads de suivi
        if trailing_thread and trailing_thread.is_alive():
            trailing_thread.do_run = False
        if tp_thread and tp_thread.is_alive():
            tp_thread.do_run = False

        position_open = False
        current_direction = None
        current_entry_price = None

    except Exception as e:
        err_msg = f"❌ Erreur fermeture position : {e}"
        print(err_msg)
        traceback.print_exc()
        send_telegram(err_msg)

# === ARRÊT PROPRE DU BOT PAR FICHIER stop.txt OU CTRL+C ===
def should_stop():
    return os.path.exists("stop.txt")

def run_bot():
    global position_open, current_direction
    print("🔁 Bot en cours d’exécution...")
    client.futures_change_leverage(symbol=symbol, leverage=leverage)
    last_signal = None
    last_candle_time = None

    try:
        while True:
            if should_stop():
                send_telegram("🛑 Fichier stop.txt détecté, arrêt du bot.")
                print("Arrêt demandé par stop.txt")
                break

            try:
                if manual_close_requested() and position_open:
                    close_position()
                    reset_manual_close()
                    send_telegram("🔴 Position fermée manuellement depuis Telegram.")
                    position_open = False
                    time.sleep(2)

                klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=2)
                current_candle = klines[-1]
                current_candle_time = current_candle[0]

                signal = get_ema_cross()

                if signal and (signal != last_signal or last_candle_time != current_candle_time):
                    print(f"Signal détecté : {signal} sur la bougie 5m {datetime.datetime.fromtimestamp(current_candle_time / 1000)}")

                    if position_open:
                        close_position()
                        time.sleep(3)

                    open_trade(signal)
                    last_signal = signal
                    last_candle_time = current_candle_time

                elif signal is None:
                    last_signal = None

                time.sleep(60)

            except Exception as e:
                err_msg = f"❌ Erreur du bot : {e}"
                print(err_msg)
                traceback.print_exc()
                send_telegram(err_msg)
                time.sleep(60)
    except KeyboardInterrupt:
        send_telegram("🛑 Bot arrêté manuellement par l'utilisateur (CTRL+C).")
        print("Arrêt manuel du bot.")

def manual_close_requested():
    return os.path.exists("manual_close_request.txt")

def reset_manual_close():
    if os.path.exists("manual_close_request.txt"):
        os.remove("manual_close_request.txt")

if __name__ == "__main__":
    run_bot()