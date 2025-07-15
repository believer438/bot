import os
import time
import threading
import traceback
from binance.client import Client
from strategies.ema_cross import start_ema_ws_thread  # ✅ WebSocket EMA 5min
from binance.exceptions import BinanceAPIException, BinanceRequestException
from dotenv import load_dotenv
from core.state import state
from core.telegram_controller import stop_telegram_bot, send_telegram
from core.config import (
    symbol,                # <-- Import du symbole centralisé
    default_leverage,      # <-- Import du levier par défaut
    default_quantity_usdt, # <-- Import de la quantité par défaut
    stop_loss_pct,         # <-- Import du SL centralisé
    take_profit_pct,       # <-- Import du TP centralisé
    BASE_DIR,              # <-- Import du répertoire de base
    MODE_FILE,             # <-- Import du chemin mode.txt
    GAIN_ALERT_FILE        # <-- Import du chemin gain_alert.txt
)
from core.binance_client import client, check_position_open, change_leverage
from core.trade_interface import open_trade, close_position
from core.position_utils import sync_position
from core.trailing import update_trailing_sl_and_tp, wait_for_tp_or_exit
from core.utils import safe_round, retry_order
from core.trading_utils import calculate_quantity, log_trade, get_mode, get_leverage_from_file
import subprocess
import psutil  # module en haut de ton fichier (pip install psutil si besoin)
import math

# === Chargement des variables d’environnement (.env) ===
load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

# === VARIABLES GLOBALES ===
trailing_thread = None
tp_thread = None
stop_event = threading.Event()
last_bot_tp = None
last_bot_sl = None

# === Vérification que les Futures sont activés sur le compte ===
def check_futures_permissions():
    try:
        account_info = client.futures_account()
        if "canTrade" not in account_info or not account_info["canTrade"]:
            raise Exception("⚠ Les Futures ne sont pas activés sur ce compte Binance.")
        print("✅ Futures activés sur ce compte Binance.")
    except Exception as e:
        print("❌ Erreur de permission Futures :", e)
        raise

# === Synchronisation de l'horloge système avec Binance ===
def sync_time():
    try:
        server_time = client.get_server_time()["serverTime"] // 1000
        local_time = int(time.time())
        delta = server_time - local_time
        if abs(delta) > 2:
            print(f"⚠️ Décalage horaire détecté : {delta} secondes (Synchronisez l'horloge Windows !)")
        else:
            print("⏰ Heure locale synchronisée avec Binance.")
    except Exception as e:
        print("Erreur lors de la synchronisation de l'heure :", e)

def sync_windows_time():
    try:
        # Force la resynchronisation
        subprocess.run("w32tm /resync", shell=True, check=True)
        print("⏰ Synchronisation de l'heure Windows effectuée.")
    except Exception as e:
        print(f"Erreur lors de la synchronisation de l'heure Windows : {e}")
        send_telegram(f"⚠️ Erreur synchronisation heure Windows : {e}")
        
def get_price_precision(symbol):
    info = client.futures_exchange_info()
    for s in info['symbols']:
        if s['symbol'] == symbol:
            tick_size = float([f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER'][0]['tickSize'])
            return int(round(-math.log10(tick_size)))
    return 4  # Valeur par défaut si non trouvé

from core.trading_utils import get_leverage_from_file  # à importer si dans un fichier utils séparé

def auto_set_sl_tp(stop_event):
    while not stop_event.is_set():
        try:
            positions = client.futures_position_information(symbol=symbol)
            position_handled = False

            for pos in positions:
                amt = float(pos['positionAmt'])

                if amt != 0 and not position_handled:
                    position_handled = True

                    try:
                        # Récupération fiable du levier via futures_account()
                        account_info = client.futures_account()
                        current_leverage = default_leverage
                        for asset in account_info['positions']:
                            if asset['symbol'] == symbol:
                                current_leverage = int(asset.get('leverage', default_leverage))
                                break

                        # Application du levier
                        client.futures_change_leverage(symbol=symbol, leverage=current_leverage)

                    except Exception as e:
                        print(f"⚠️ Erreur application du levier : {e}")
                        send_telegram(f"⚠️ Erreur application du levier : {e}")

                    # ⬇️ Mise à jour des infos dans le state
                    state.position_open = True
                    entry_price = float(pos['entryPrice'])
                    state.current_entry_price = entry_price
                    direction = "bullish" if amt > 0 else "bearish"
                    state.current_direction = direction
                    qty = abs(amt)
                    state.current_quantity = qty

                    side_close = "SELL" if direction == "bullish" else "BUY"

                    # Calcul du TP et SL selon la direction
                    take_profit = entry_price * (1 + take_profit_pct if amt > 0 else 1 - take_profit_pct)
                    stop_price = entry_price * (1 - stop_loss_pct if amt > 0 else 1 + stop_loss_pct)

                    precision = get_price_precision(symbol)
                    stop_price = round(stop_price, precision)
                    take_profit = round(take_profit, precision)

                    orders = client.futures_get_open_orders(symbol=symbol)
                    sl_orders = [o for o in orders if o['type'] == "STOP_MARKET" and o['side'] == side_close and o.get('closePosition', False)]
                    tp_orders = [o for o in orders if o['type'] == "TAKE_PROFIT_MARKET" and o['side'] == side_close and o.get('closePosition', False)]

                    # Vérifie juste la présence d'au moins un SL/TP
                    has_sl = len(sl_orders) > 0
                    has_tp = len(tp_orders) > 0

                    print(f"Positions: {amt}, SL orders found: {len(sl_orders)}, TP orders found: {len(tp_orders)}")
                    print(f"Has SL: {has_sl}, Has TP: {has_tp}")

                    # Ne supprime rien, pose un SL/TP seulement si aucun n'existe
                    if not has_tp:
                        retry_order(lambda: client.futures_create_order(
                            symbol=symbol,
                            side=side_close,
                            type="TAKE_PROFIT_MARKET",
                            stopPrice=take_profit,
                            closePosition=True,
                            timeInForce="GTC"
                        ))
                        last_bot_tp = take_profit
                        send_telegram(f"🎯Take profit automatique à {take_profit}$")

                    if not has_sl:
                        retry_order(lambda: client.futures_create_order(
                            symbol=symbol,
                            side=side_close,
                            type="STOP_MARKET",
                            stopPrice=stop_price,
                            closePosition=True,
                            timeInForce="GTC"
                        ))
                        last_bot_sl = stop_price
                        send_telegram(f"🛡 Stop loss automatique à {stop_price}$")

            if not position_handled:
                state.position_open = False
                state.current_entry_price = None
                state.current_direction = None
                state.current_quantity = None

            time.sleep(3)

        except Exception as e:
            print(f"❌ Erreur dans auto_set_sl_tp : {e}")
            send_telegram(f"❌ Erreur dans auto_set_sl_tp : {e}")

        time.sleep(3)

def should_stop():
    stop_path = os.path.join(BASE_DIR, "stop.txt")
    return os.path.exists(stop_path)

def update_status(text):  # ✅ version avec try-except
    try:
        status_path = os.path.join(BASE_DIR, "status.txt")
        with open(status_path, "w") as f:
            f.write(text)
    except Exception as e:
        print(f"Erreur écriture status.txt : {e}")

def manual_close_requested():
    manual_close_path = os.path.join(BASE_DIR, "manual_close_request.txt")
    return os.path.exists(manual_close_path)

def reset_manual_close():
    manual_close_path = os.path.join(BASE_DIR, "manual_close_request.txt")
    if os.path.exists(manual_close_path):
        os.remove(manual_close_path)

def manual_close_watcher(stop_event):
    while not stop_event.is_set():
        if manual_close_requested():
            try:
                close_position()
            except Exception as e:
                send_telegram(f"❌ Erreur lors de la fermeture manuelle : {e}")
            reset_manual_close()
        time.sleep(0.1)

# === FONCTION DE SURVEILLANCE DE LA POSITION ===
# === FONCTION DE SURVEILLANCE DE LA POSITION ===
def monitor_position(stop_event):
    global last_bot_tp, last_bot_sl
    last_position_amt = None
    last_detected_tp = None
    last_detected_sl = None

    while True:
        try:
            positions = client.futures_position_information(symbol=symbol)
            for pos in positions:
                if float(pos['positionAmt']) != 0:
                    if not state.position_open:
                        send_telegram("⚠ Une position a été détectée ouverte manuellement sur Binance.")
                        state.position_open = True
                        state.current_entry_price = float(pos['entryPrice'])
                        state.current_direction = "bullish" if float(pos['positionAmt']) > 0 else "bearish"
                        state.current_quantity = abs(float(pos['positionAmt']))

                    # 🟡 Vérifie les ordres ouverts
                    orders = client.futures_get_open_orders(symbol=symbol)
                    tp = None
                    sl = None

                    for order in orders:
                        if order['type'] == "TAKE_PROFIT_MARKET":
                            tp = float(order['stopPrice'])
                        if order['type'] == "STOP_MARKET":
                            sl = float(order['stopPrice'])
                            
                    last_position_amt = float(pos['positionAmt'])
                    break  # Une seule position prise en compte
                else:
                    continue
            else:
                # Si aucune position détectée : reset
                if state.position_open:
                    state.reset_all()
                    last_detected_tp = None
                    last_detected_sl = None
                    last_position_amt = None

            time.sleep(5)

        except Exception as e:
            print("Erreur dans monitor_position :", e)
            time.sleep(10)
           
def is_another_bot_running(lock_file):
    """Vérifie si un autre process Python (hors le nôtre) tourne et a le lock."""
    current_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['pid'] == current_pid:
                continue
            # Vérifie si c'est un process Python qui utilise ce fichier
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                if lock_file in ' '.join(proc.info['cmdline']):
                    return True
        except Exception:
            continue
    return False

def run_bot():
    lock_file = os.path.join(BASE_DIR, "bot.lock")

    # Si le lock existe, vérifie s'il y a un autre bot actif
    if os.path.exists(lock_file):
        if is_another_bot_running(lock_file):
            print("⚠️ Une autre instance du bot est déjà en cours (process détecté). Abandon.")
            send_telegram("⚠️ Lancement annulé : une autre instance du bot est déjà en cours (process détecté).")
            return
        else:
            # Aucun autre process, lock orphelin : on le supprime
            try:
                os.remove(lock_file)
                print("🟢 Fichier bot.lock orphelin supprimé automatiquement.")
            except Exception as e:
                print(f"❌ Impossible de supprimer bot.lock : {e}")
                send_telegram(f"❌ Impossible de supprimer bot.lock : {e}")
                return

    # Vérifie à nouveau après suppression
    if os.path.exists(lock_file):
        print("⚠️ Une autre instance du bot est déjà en cours. Abandon.")
        send_telegram("⚠️ Lancement annulé : une autre instance du bot est déjà en cours.")
        return

    # Crée un fichier de verrouillage
    with open(lock_file, "w") as f:
        f.write("locked")

    try:
        send_telegram("🤖 Bot est bien lancé monsieur ...")
        update_status("ACTIF - En cours d'exécution ...")

        print("🔁 Lancement des threads de surveillance...")
        resilient_thread(monitor_position, stop_event)

        levier = get_leverage_from_file()
        if change_leverage(symbol, levier):
            print(f"✅ Levier mis à jour avec succès : {levier}x")
            send_telegram(f"✅ Levier mis à jour avec succès : {levier}x")  # <-- Ajout du message Telegram
        else:
            print(f"⚠️ Levier non mis à jour : {levier}x")
            send_telegram(f"⚠️ Levier non mis à jour : {levier}x")  # <-- Ajout du message Telegram

        backoff_time = 5
        max_backoff = 60
        print("🔄 Démarrage du bot de trading...")

        while not stop_event.is_set():
            try:
                if should_stop():
                    stop_event.set()
                    send_telegram("🛑 Fichier stop.txt détecté, arrêt du bot.")
                    update_status("ARRÊT - Fichier stop.txt détecté")
                    break

                time.sleep(5)

                if manual_close_requested() and state.position_open:
                    close_position()
                    reset_manual_close()
                    time.sleep(2)

                time.sleep(10)
                backoff_time = 5  # Réinitialise le délai si tout va bien

            except Exception as e:
                send_telegram(f"❌ Erreur principale : {e}")
                update_status(f"ERREUR - {str(e)}")
                traceback.print_exc()
                print(f"⏳ Erreur rencontrée, nouvelle tentative dans {backoff_time}s...")
                time.sleep(backoff_time)
                backoff_time = min(max_backoff, backoff_time * 2)

        update_status("ARRÊT - Terminé proprement")
        
# le fichier bot.lock

    finally:
        print("🔒 Arrêt du bot, suppression du fichier de verrouillage...")
        if os.path.exists(lock_file):
            os.remove(lock_file)
          
# === FONCTION PRINCIPALE DU BOT ===
def launch_bot():
    global trailing_thread, tp_thread

    try:
        if not change_leverage(symbol, default_leverage):
            send_telegram(f"❌ Bot arrêté : changement de levier échoué sur {symbol}")
            return

        # Stocker les threads dans les variables globales
        trailing_thread = resilient_thread(auto_set_sl_tp, stop_event)
        tp_thread = resilient_thread(manual_close_watcher, stop_event)
        resilient_thread(monitor_position, stop_event)  # Tu peux aussi stocker celui-ci si tu veux

        print("🔄 Lancement du bot de trading...")
        run_bot()
    except Exception as e:
        send_telegram(f"❌ Erreur critique lors du lancement du bot : {e}")
        traceback.print_exc()

# === FONCTIONS DE LECTURE DES VALEURS DYNAMIQUES ===
def get_dynamic_leverage():
    try:
        lev_path = os.path.join(BASE_DIR, "leverage.txt")
        with open(lev_path, "r") as f:
            lev = int(f.read().strip())
            return lev
    except Exception:
        return default_leverage

def get_dynamic_quantity():
    try:
        qty_path = os.path.join(BASE_DIR, "quantity.txt")
        with open(qty_path, "r") as f:
            qty = float(f.read().strip())
            return qty
    except Exception:
        return default_quantity_usdt

def retry_order(order_fn, max_retries=3, delay=2):
    for attempt in range(max_retries):
        try:
            return order_fn()
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                send_telegram(f"❌ Erreur lors de la pose d'un ordre (SL/TP) : {e}")
                print(f"❌ Erreur lors de la pose d'un ordre (SL/TP) : {e}")

def resilient_thread(target_fn, *args):  # ✅ Nouvelle version
    def wrapper():
        retries = 0
        while True:
            try:
                target_fn(*args)
                break
            except Exception as e:
                print(f"Thread {target_fn.__name__} crashé : {e}, relance dans 5s")
                retries += 1
                if retries > 10:
                    send_telegram(f"❌ Trop d'erreurs sur {target_fn.__name__}, thread arrêté.")
                    break
                time.sleep(5)
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return t

def stop_bot():
    print("🔴 Arrêt du bot demandé, signal d’arrêt envoyé aux threads...")
    stop_event.set()

    if trailing_thread and trailing_thread.is_alive():
        trailing_thread.join()
    if tp_thread and tp_thread.is_alive():
        tp_thread.join()
    
    # Arrêt propre du bot Telegram
    stop_telegram_bot()
    
    print("🟢 Tous les composants ont été arrêtés proprement.")


if __name__ == "__main__":
    sync_windows_time()
    launch_bot()