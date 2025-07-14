import os
import csv
import time
import datetime
import traceback
import threading

from core.binance_client import client
from core.utils import safe_round
from core.telegram_controller import send_telegram
from core.config import (
    symbol,
    stop_loss_pct,
    take_profit_pct,
    BASE_DIR,
    LOG_DIR,
    MODE_FILE,
    LEVERAGE_FILE,
    QUANTITY_FILE
)

log_dir = LOG_DIR  # <-- centralisé
log_file = os.path.join(log_dir, "logs.csv")
mode_file = MODE_FILE
leverage_file = LEVERAGE_FILE
quantity_file = QUANTITY_FILE

# === Création du dossier logs s'il n'existe pas ===
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# === Verrous globaux pour accès thread-safe aux fichiers ===
log_lock = threading.Lock()
config_lock = threading.Lock()  # protège lecture/écriture fichiers config (mode, levier, quantité)

def get_mode() -> str:
    """
    Lit le mode de fonctionnement (auto/alert) depuis mode.txt.
    En cas d'erreur ou fichier absent, crée en mode 'auto' et notifie Telegram.
    """
    try:
        with config_lock:
            if not os.path.exists(mode_file):
                with open(mode_file, "w") as f:
                    f.write("auto")
                send_telegram("⚠️ mode.txt créé automatiquement en mode AUTO")
                return "auto"

            with open(mode_file, "r") as file:
                mode = file.read().strip().lower()
                if mode not in ["auto", "alert"]:
                    send_telegram("⚠️ mode.txt corrompu, passage en mode AUTO")
                    return "auto"
                return mode

    except Exception as e:
        err = traceback.format_exc()
        send_telegram(f"⚠️ Erreur get_mode() : {e}\n{err}")
        return "auto"


def get_leverage_from_file(filepath=leverage_file, default_leverage=10) -> int:
    """
    Lit le levier dans leverage.txt.
    Valide que c'est un entier entre 1 et 125.
    Envoie un warning si levier > 50 (risque).
    """
    try:
        with config_lock:
            with open(filepath, "r") as f:
                content = f.read().strip()
                if not content:
                    raise ValueError("Fichier levier vide")
                lev = int(content)
                if lev < 1 or lev > 125:
                    raise ValueError("Levier hors limites (1-125)")
                if lev > 50:
                    send_telegram(f"⚠️ Attention : levier élevé détecté ({lev})")
                return lev
    except Exception as e:
        err = traceback.format_exc()
        send_telegram(f"⚠️ Erreur lecture levier : {e}\n{err}")
        print(f"⚠️ Erreur lecture levier : {e} → Levier par défaut : {default_leverage}")
        return default_leverage

def get_quantity_from_file(filepath=quantity_file, default_quantity=1.0) -> float:
    """
    Lit la quantité dans quantity.txt.
    Valide que c'est un float > 0.1 (minimum Binance).
    """
    try:
        with config_lock:
            with open(filepath, "r") as f:
                content = f.read().strip()
                if not content:
                    raise ValueError("Fichier quantité vide")
                qty = float(content)
                if qty < 0.1:
                    send_telegram(f"⚠️ Quantité trop faible détectée ({qty}), minimum 0.1")
                if qty <= 0:
                    raise ValueError("Quantité invalide (<=0)")
                return qty
    except Exception as e:
        err = traceback.format_exc()
        send_telegram(f"⚠️ Erreur lecture quantité : {e}\n{err}")
        print(f"⚠️ Erreur lecture quantité : {e} → Quantité par défaut : {default_quantity}")
        return default_quantity

def calculate_quantity(entry_price: float, quantity_usdt: float, leverage: int) -> float:
    """
    Calcule la quantité ALGO à trader en fonction du prix d'entrée, quantité USDT et levier.
    Arrondi à 1 décimale (conforme Binance).
    Lève une erreur si quantité trop faible.
    """
    qty = (quantity_usdt * leverage) / entry_price
    qty = safe_round(qty, 1)
    if qty < 0.1:
        raise ValueError("❌ Quantité trop petite pour Binance Futures (min 0.1 ALGO)")
    return qty

def log_trade(direction: str, entry_price: float, sl: float, tp: float, mode: str, status="OUVERT", gain: float = None):
    """
    Journalise un trade dans logs.csv de façon thread-safe.
    Crée le fichier et les entêtes s'il n'existe pas.
    """
    try:
        with log_lock:
            if not os.path.exists(log_file):
                with open(log_file, mode="w", newline="") as file:
                    writer = csv.writer(file)
                    writer.writerow(["Date", "Direction", "Entry Price", "Stop Loss", "Take Profit", "Mode", "Status", "Gain $"])

            with open(log_file, mode="a", newline="") as file:
                writer = csv.writer(file)
                writer.writerow([
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    direction.upper(),
                    safe_round(entry_price, 4),
                    safe_round(sl, 4),
                    safe_round(tp, 4),
                    mode.upper(),
                    status,
                    safe_round(gain, 4) if gain is not None else ""
                ])
    except Exception as e:
        err = traceback.format_exc()
        send_telegram(f"❌ Erreur lors de la journalisation : {e}\n{err}")
        print("❌ Erreur lors de la journalisation :", e)

def update_trade_status(entry_price: float, new_status: str, direction: str = None, date_str: str = None):
    """
    Met à jour le statut d'un trade dans logs.csv.
    Recherche par prix d'entrée + direction + date (si fournie) pour éviter confusions.
    """
    try:
        with log_lock:
            lines = []
            with open(log_file, "r", newline="") as file:
                reader = csv.reader(file)
                headers = next(reader)
                for row in reader:
                    # Comparaison sécurisée des critères pour identifier le bon trade
                    price_match = abs(float(row[2]) - safe_round(entry_price, 4)) < 1e-6
                    direction_match = (direction is None or row[1].upper() == direction.upper())
                    date_match = (date_str is None or row[0].startswith(date_str))
                    if price_match and direction_match and row[6] == "OUVERT" and date_match:
                        row[6] = new_status
                    lines.append(row)

            with open(log_file, "w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(headers)
                writer.writerows(lines)
    except Exception as e:
        err = traceback.format_exc()
        send_telegram(f"❌ Erreur update_trade_status : {e}\n{err}")
        print("❌ Erreur update_trade_status :", e)

def check_position_open() -> bool:
    """
    Vérifie s’il y a une position ouverte sur Binance Futures pour le symbole global.
    """
    try:
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            if float(pos['positionAmt']) != 0:
                return True
        return False
    except Exception as e:
        err = traceback.format_exc()
        send_telegram(f"❌ Erreur check_position_open : {e}\n{err}")
        print("❌ Erreur check_position_open :", e)
        return False

def retry_order(order_function, max_attempts=5, initial_delay=0.2):
    """
    Relance un ordre en cas d’échec avec backoff exponentiel.
    Protège le sleep en try/except pour éviter blocage.
    Envoie un message Telegram à chaque échec.
    """
    delay = initial_delay
    for attempt in range(max_attempts):
        try:
            return order_function()
        except Exception as e:
            msg = f"⚠️ Tentative {attempt+1}/{max_attempts} échouée : {e}"
            print(msg)
            send_telegram(msg)
            try:
                time.sleep(delay)
            except Exception as sleep_e:
                print(f"⚠️ Erreur pendant sleep : {sleep_e}")
            delay *= 2  # Double le délai à chaque tentative
    raise Exception("❌ Toutes les tentatives d’ordre ont échoué.")
