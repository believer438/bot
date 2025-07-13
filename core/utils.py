import os
import threading
import traceback
import time
from core.notifier import send_telegram
from core.config import (
    BASE_DIR,
    QUANTITY_FILE,
    LEVERAGE_FILE,
    STATUS_FILE,
    TRADE_STATUS_FILE,
    default_leverage,
    default_quantity_usdt
)
# from core.state import state  # <-- À importer si tu veux utiliser l'état global dans ce fichier

# === Verrous globaux ===
status_lock = threading.Lock()
config_lock = threading.Lock()

# === Arrondi sécurisé ===
def safe_round(value, ndigits=4):
    try:
        if value is None:
            return None
        return round(float(value), ndigits)
    except Exception:
        return None

# === Conversion sécurisée en float ===
def safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None

# === Lecture de la quantité dynamique ===
def get_dynamic_quantity():
    try:
        with config_lock:
            with open(QUANTITY_FILE, "r") as f:
                qty = float(f.read().strip())
                return qty
    except Exception as e:
        print(f"❌ Erreur lecture quantity.txt : {e}")
        return default_quantity_usdt  # <-- Utilise la valeur par défaut centralisée

# === Lecture du levier dynamique ===
def get_dynamic_leverage():
    try:
        with config_lock:
            with open(LEVERAGE_FILE, "r") as f:
                leverage = int(f.read().strip())
                return leverage
    except Exception as e:
        print(f"❌ Erreur lecture leverage.txt : {e}")
        return default_leverage  # <-- Utilise la valeur par défaut centralisée

# === Retry d’un ordre Binance en cas d’échec temporaire ===
def retry_order(order_fn, max_retries=3, delay=2, label="ORDRE"):
    for attempt in range(max_retries):
        try:
            return order_fn()
        except KeyboardInterrupt:
            print("⛔ Interruption manuelle détectée. Annulation du retry.")
            raise
        except Exception as e:
            msg = f"⚠️ Tentative {attempt+1}/{max_retries} échouée : {type(e).__name__} - {e}"
            print(msg)
            send_telegram(msg)
            try:
                time.sleep(delay)
            except Exception as sleep_e:
                print(f"⚠️ Erreur pendant le sleep : {sleep_e}")
    raise Exception(f"❌ Toutes les tentatives pour {label} ont échoué.")

# === Mise à jour du statut du bot dans status.txt ===
def update_status(status_text):
    try:
        with status_lock:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                f.write(status_text)
    except Exception as e:
        print(f"❌ Erreur update_status : {e}")

# === Mise à jour du statut d’un trade (nom à ne pas confondre avec trading_utils.py) ===
def update_trade_status_file(entry_price, status):
    try:
        with open(TRADE_STATUS_FILE, "w", encoding="utf-8") as f:
            f.write(f"Entrée : {entry_price} | Statut : {status}")
    except Exception as e:
        print(f"❌ Erreur update_trade_status_file : {e}")

# === Lancement sécurisé d’un thread ===
def start_thread(target_fn, *args, **kwargs):
    """
    Démarre une fonction dans un thread daemon pour l'exécuter en arrière-plan.
    """
    try:
        thread = threading.Thread(target=target_fn, args=args, kwargs=kwargs, daemon=True)
        thread.start()
        return thread
    except Exception as e:
        err = traceback.format_exc()
        print(f"❌ Erreur lors du démarrage du thread {target_fn.__name__} : {e}\n{err}")
        return None
