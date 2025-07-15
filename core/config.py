import os

from dotenv import load_dotenv
load_dotenv()  # <-- Ajoute ceci tout en haut, AVANT tout os.getenv

# ⚙️ Paramètres principaux de trading
symbol = os.getenv("SYMBOL", "ALGOUSDT")         # 🔁 Paire de trading
default_leverage = int(os.getenv("LEVERAGE", 10))  # 📈 Levier par défaut
default_quantity_usdt = float(os.getenv("QUANTITY_USDT", 2))  # 💰 Quantité USDT par trade

# 📊 Paramètres de stratégie
stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", 0.008))      # % Stop Loss
take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", 0.015))  # % Take Profit

# 🕒 Paramètres de temps
strategy_interval = int(os.getenv("STRATEGY_INTERVAL", 10))   # Intervalle d'exécution de la stratégie (secondes)
trailing_check_interval = int(os.getenv("TRAILING_INTERVAL", 5))  # Intervalle de vérification du trailing (secondes)

# 📁 Chemins de fichiers
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
MODE_FILE = os.path.join(BASE_DIR, "mode.txt")
LEVERAGE_FILE = os.path.join(BASE_DIR, "leverage.txt")
QUANTITY_FILE = os.path.join(BASE_DIR, "quantity.txt")
GAIN_ALERT_FILE = os.path.join(BASE_DIR, "gain_alert.txt")
MANUAL_CLOSE_FILE = os.path.join(BASE_DIR, "manual_close_request.txt")
CONTEXT_FILE = os.path.join(BASE_DIR, "context.json")
STATUS_FILE = os.path.join(BASE_DIR, "status.txt")
TRADE_STATUS_FILE = os.path.join(BASE_DIR, "trade_status.txt")

# 🔒 Sécurité & accès
admin_chat_id_env = os.getenv("TELEGRAM_CHAT_ID")
if admin_chat_id_env is None:
    raise Exception("❌ TELEGRAM_CHAT_ID manquant dans .env ou les variables d'environnement.")
admin_chat_id = int(admin_chat_id_env)

# 🛠️ Divers
max_retry_order = int(os.getenv("MAX_RETRY_ORDER", 3))  # Nombre max de retry pour un ordre
retry_delay = int(os.getenv("RETRY_DELAY", 2))          # Délai entre les retry (secondes)

# === Paramètres EMA Cross centralisés ===
ema_interval = os.getenv("EMA_INTERVAL", "5m")
ema_lookback = int(os.getenv("EMA_LOOKBACK", 100))
