import os
import sys
import psutil
from core.config import BASE_DIR, symbol, default_leverage, default_quantity_usdt # Utilise BASE_DIR depuis config.py

lock_file = os.path.join(BASE_DIR, "bot.lock")

def is_another_bot_running(lock_file):
    current_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['pid'] == current_pid:
                continue
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                if lock_file in ' '.join(proc.info['cmdline']):
                    return True
        except Exception:
            continue
    return False

# Protection anti-double instance Telegram
if os.path.exists(lock_file):
    if is_another_bot_running(lock_file):
        print("⚠️ Une autre instance du bot (trading ou Telegram) est déjà en cours. Abandon.")
        sys.exit(0)
    else:
        # Aucun autre process, lock orphelin : on le supprime
        try:
            os.remove(lock_file)
            print("🟢 Fichier bot.lock orphelin supprimé automatiquement (Telegram).")
        except Exception as e:
            print(f"❌ Impossible de supprimer bot.lock : {e}")
            sys.exit(0)

# Crée le lock pour Telegram aussi
with open(lock_file, "w") as f:
    f.write("locked")

import atexit
def remove_lock():
    if os.path.exists(lock_file):
        os.remove(lock_file)
atexit.register(remove_lock)

import sys
import os
import threading
import json
import logging
import time
import traceback
# Ajout du chemin parent dans sys.path pour imports relatifs
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import telebot
from dotenv import load_dotenv
from core.state import state
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from types import SimpleNamespace
from core.utils import safe_round
from core.notifier import send_telegram
from binance.client import Client

# === Chargement des variables d’environnement (.env) ===
load_dotenv()

# === Configuration du logging ===
logging.basicConfig(
    filename="logs/telegram_bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# === Chargement des variables de config à partir de .env ===
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
if TELEGRAM_CHAT_ID:
    TELEGRAM_CHAT_ID = int(TELEGRAM_CHAT_ID)

SYMBOL = os.getenv("SYMBOL", "ALGOUSDT")
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", 10))
DEFAULT_QUANTITY = float(os.getenv("DEFAULT_QUANTITY", 1.0))

# === Vérification des variables critiques ===
if not API_KEY or not API_SECRET:
    raise Exception("❌ BINANCE_API_KEY ou BINANCE_API_SECRET manquant dans .env")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise Exception("❌ TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant dans .env")

# === Initialisation du client Binance (API) ===
client = Client(API_KEY, API_SECRET)

# === Initialisation du bot Telegram ===
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# === Contexte utilisateur en mémoire ===
user_trade_context = {}

# === Prix initial (exemple) ===
try:
    price = float(client.get_symbol_ticker(symbol=symbol)["price"])
except Exception as e:
    logging.error(f"Erreur récupération prix initial: {e}")
    price = 0.0

# === Configuration du logging ===

def log_info(msg):
    print(msg)  # console
    logging.info(msg)

def log_error(msg):
    print(msg)  # console
    logging.error(msg)

# === Fonction pour demander à l'utilisateur ===
# Cette fonction envoie un message à l'utilisateur et attend sa réponse.
def ask_user(bot, chat_id, message_text, next_step_handler, timeout=30):
    msg = bot.send_message(chat_id, message_text)
    bot.register_next_step_handler(msg, next_step_handler)
    # Plus de rappel automatique
    return msg

# === Chargement et sauvegarde du contexte utilisateur ===
# Le contexte utilisateur est stocké dans un fichier JSON pour persister entre les sessions.
def load_user_trade_context():
    try:
        with open("context.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}
    
def save_user_trade_context():
    try:
        with open("context.json", "w") as f:
            json.dump(user_trade_context, f)
    except Exception as e:
        print(f"Erreur sauvegarde contexte : {e}")

# Charge le contexte au démarrage
user_trade_context = load_user_trade_context()

# === ALERTES DE GAINS ===
def read_gain_alert():
    try:
        with open("gain_alert.txt", "r") as f:
            return f.read().strip() == "on"
    except FileNotFoundError:
        return True  # par défaut activé

def write_gain_alert(value):
    with open("gain_alert.txt", "w") as f:
        f.write("on" if value else "off")

@bot.message_handler(commands=['gain_alert'])
def toggle_gain_alert(message):
    log_info(f"[GAIN_ALERT] Commande reçue de {message.chat.id} : {message.text}")
    current = read_gain_alert()
    new_state = not current
    write_gain_alert(new_state)
    state_msg = "✅ Alertes de gains ACTIVÉES ✅" if new_state else "🚫 Alertes de gains DÉSACTIVÉES 🚫"
    bot.reply_to(message, state_msg)

# === STATUS ===
@bot.message_handler(commands=['status'])
def status(message):
    log_info(f"[STATUS] Commande reçue de {message.chat.id} : {message.text}")
    try:
        with open("mode.txt", "r") as f:
            mode_value = f.read().strip().lower()
            if mode_value not in ["auto", "alert"]:
                mode_value = "auto"  # Valeur par défaut si contenu inattendu
    except Exception:
        mode_value = "auto"  # Valeur par défaut si fichier absent ou erreur
    mode_label = "AUTO" if mode_value == "auto" else "ALERTE"
    bot.reply_to(message, f"✅ SKY_TRADER est bien actif et en mode {mode_label}.")
# === FERMETURE MANUELLE ===
@bot.message_handler(commands=['close'])
def close(message):
    log_info(f"[CLOSE] Commande reçue de {message.chat.id} : {message.text}")
    with open("manual_close_request.txt", "w") as f:
        f.write("close")
    bot.send_message(message.chat.id, "🔴 Fermeture de la position en cours ...")

# === SHUTDOWN ===
# Permet d'arrêter le bot manuellement via Telegram
@bot.message_handler(commands=['shutdown'])
def shutdown(message):
    log_info(f"[SHUTDOWN] Commande reçue de {message.chat.id} : {message.text}")
    if message.chat.id == TELEGRAM_CHAT_ID:
        bot.send_message(message.chat.id, "🛑 Bot arrêté.")
        log_info(f"Bot arrêté manuellement par {message.chat.id}")
        os._exit(0)
    else:
        bot.send_message(message.chat.id, "❌ Permission refusée.")
        log_info(f"Tentative d'arrêt non autorisée par {message.chat.id}")

# === CHANGEMENT DE MODE ===
@bot.message_handler(commands=['mode'])
def mode(message):
    log_info(f"[MODE] Commande reçue de {message.chat.id} : {message.text}")
    try:
        mode_value = message.text.split(" ")[1].lower()
        if mode_value in ["auto", "alert"]:
            with open("mode.txt", "w") as f:
                f.write(mode_value)
            bot.reply_to(message, f"✅ SKY_TRADER passe en {mode_value.upper()}")
        else:
            bot.reply_to(message, "⚠ Mode inconnu. Utilisez /mode auto ou /mode alert.")
    except Exception:
        bot.reply_to(message, "⚠ Utilisation : /mode auto ou /mode alert")

# === AIDE ===
@bot.message_handler(commands=['help'])
def help(message):
    log_info(f"[HELP] Commande reçue de {message.chat.id} : {message.text}")
    help_msg = (
        "/status - Voir l'état du bot\n"
        "/close - Fermer la position\n"
        "/mode auto - Activer le mode automatique\n"
        "/mode alert - Activer le mode alerte\n"
        "/gain_alert - Activer/désactiver les alertes de gains\n"
        "/help - Affiche cette aide"
        "/menu - Afficher le menu principal\n"
        "/start - Démarrer le bot\n"
        "/shutdown - Arrêter le bot (admin seulement)\n"
    )
    bot.reply_to(message, help_msg)

# === MENU ===
def send_main_reply_keyboard(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("📊 Statut"), KeyboardButton("📈 Trader"))
    markup.row(KeyboardButton("🔄 Mode AUTO"), KeyboardButton("🔔 Mode ALERT"))
    markup.row(KeyboardButton("💰 Alertes de gains"), KeyboardButton("❓ Aide"))
    markup.row(KeyboardButton("🪙 Levier & Solde"), KeyboardButton("📚 Plus ➡️"))
    bot.send_message(chat_id, "📋 Menu principal :", reply_markup=markup)

def send_leverage_reply_keyboard(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("🪙 Levier"), KeyboardButton("💵 Quantity"))
    markup.row(KeyboardButton("⬅️ Retour"))
    bot.send_message(chat_id, "⚙️ Paramètres :", reply_markup=markup)

@bot.message_handler(commands=['start'])
def start(message):
    log_info(f"[START] Commande reçue de {message.chat.id} : {message.text}")
    send_main_reply_keyboard(message.chat.id)

@bot.message_handler(func=lambda m: m.text in [
    "📊 Statut", "📈 Trader", "🔄 Mode AUTO", "🔔 Mode ALERT",
    "💰 Alertes de gains", "❓ Aide", "🪙 Levier & Solde", "📚 Plus ➡️"
])
def handle_main_keyboard(message):
    if message.text == "📊 Statut":
        status(message)
    elif message.text == "📈 Trader":
        send_position_menu(message)
    elif message.text == "🔄 Mode AUTO":
        bot.send_message(message.chat.id, "Mode AUTO activé.")
        with open("mode.txt", "w") as f:
            f.write("auto")
    elif message.text == "🔔 Mode ALERT":
        bot.send_message(message.chat.id, "Mode ALERT activé.")
        with open("mode.txt", "w") as f:
            f.write("alert")
    elif message.text == "💰 Alertes de gains":
        toggle_gain_alert(message)
    elif message.text == "❓ Aide":
        help(message)
    elif message.text == "🪙 Levier & Solde":
        send_leverage_menu(message)  # Ouvre le menu levier
    elif message.text == "📚 Plus ➡️":
        send_more_menu(message)      # Ouvre le menu plus
    elif message.text == "⬅️ Retour":
        send_main_reply_keyboard(message.chat.id)

@bot.message_handler(func=lambda m: m.text in ["🪙 Levier", "💵 Quantity"])
def handle_leverage_keyboard(message):
    if message.text == "🪙 Levier":
        bot.send_message(message.chat.id, "Envoie-moi le nouveau levier :")
        bot.register_next_step_handler(message, save_leverage)
    elif message.text == "💵 Quantity":
        bot.send_message(message.chat.id, "Envoie-moi la nouvelle quantité en USDT :")
        bot.register_next_step_handler(message, save_quantity)

@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    data = call.data
    chat_id = call.message.chat.id

    try:
        bot.answer_callback_query(call.id)
    except telebot.apihelper.ApiTelegramException as e:
        if "query is too old" in str(e):
            print("⏱️ Bouton expiré")
            bot.send_message(chat_id, "⏱️ Ce bouton a expiré. Veuillez réessayer.")
        else:
            raise
    

    try:
        if data == "status":
            log_info(f"[CALLBACK] Bouton 'status' cliqué ")
            try:
                with open("mode.txt", "r") as f:
                    mode_value = f.read().strip()
            except Exception:
                mode_value = "inconnu"
            bot.send_message(chat_id, f"✅ SKY_TRADER actif en mode {mode_value.upper()}")

        elif data == "close":
            with open("manual_close_request.txt", "w") as f:
                f.write("close")
            bot.send_message(chat_id, "🔴 Fermeture de la position en cours ...")
        
        elif data == "mode_auto":
            try:
                with open("mode.txt", "w") as f:
                    f.write("auto")
                bot.send_message(chat_id, "✅ Mode AUTO activé.")
            except Exception as e:
                bot.send_message(chat_id, f"❌ Erreur écriture mode.txt : {e}")

        elif data == "mode_alert":
            try:
                with open("mode.txt", "w") as f:
                    f.write("alert")
                bot.send_message(chat_id, "✅ Mode ALERT activé.")
            except Exception as e:
                bot.send_message(chat_id, f"❌ Erreur écriture mode.txt : {e}")
        
        elif data == "gain_alert":
            current = read_gain_alert()
            new_state = not current
            write_gain_alert(new_state)
            msg = "✅ Alertes de gains ACTIVÉES ✅" if new_state else "🚫 Alertes de gains DÉSACTIVÉES 🚫"
            bot.send_message(chat_id, msg)

        elif data == "help":
            help_msg = (
                "/status - Voir l'état du bot\n"
                "/close - Fermer la position\n"
                "/mode auto - Activer le mode automatique\n"
                "/mode alert - Activer le mode alerte\n"
                "/gain_alert - Activer/désactiver les alertes de gains\n"
                "/help - Affiche cette aide"
            )
            bot.send_message(chat_id, help_msg)

        elif data == "leverage_menu":
            send_leverage_menu(call)
        elif data == "set_leverage":
            bot.send_message(chat_id, "Envoie-moi le nouveau levier :")
            bot.register_next_step_handler_by_chat_id(chat_id, save_leverage)
        elif data == "set_quantity":
            bot.send_message(chat_id, "Envoie-moi la nouvelle quantité en USDT :")
            bot.register_next_step_handler(call.message, save_quantity)

        elif data == "more":
            send_more_menu(call)
        elif data == "back_main":
            send_main_reply_keyboard(chat_id)
        elif data == "position_menu":
            send_position_menu(call)

        # === Traitement des actions spéciales ===
        elif data in ["open_bullish", "open_bearish"]:
            handle_trade_callbacks(call)

        elif data == "position":
            send_current_position(chat_id)

        elif data == "balance":
            send_balance(chat_id)

        elif data == "take_profit":
            send_take_profit(chat_id)

        elif data == "stop_loss":
            send_stop_loss(chat_id)

        else:
            bot.send_message(chat_id, "Commande inconnue.")

    except Exception as e:
        error_msg = f"❌ Erreur callback : {e}\n{traceback.format_exc()}"
        log_error(error_msg)
        bot.send_message(chat_id, f"❌ Erreur callback : {e}")

# Place la fonction ici, en dehors du handler
def send_leverage_menu(obj):
    chat_id = obj.chat.id if hasattr(obj, 'chat') else obj.message.chat.id
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("🪙 Levier", callback_data="set_leverage"),
        InlineKeyboardButton("💵 Quantité", callback_data="set_quantity")
    )
    markup.row(
        InlineKeyboardButton("⬅️ Retour", callback_data="back_main")
    )
    bot.send_message(chat_id, "⚙️ Parametre :", reply_markup=markup)

def send_more_menu(obj):
    chat_id = obj.chat.id if hasattr(obj, 'chat') else obj.message.chat.id
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("📈 Position", callback_data="position"),
        InlineKeyboardButton("💵 Balance", callback_data="balance")
    )
    markup.row(
        InlineKeyboardButton("🎯 Take Profit", callback_data="take_profit"),
        InlineKeyboardButton("🛡️ Stop Loss", callback_data="stop_loss")
    )
    markup.row(
        InlineKeyboardButton("⬅️ Retour", callback_data="back_main")
    )
    bot.send_message(chat_id, "📚 Menu Plus :", reply_markup=markup)
def send_position_menu(obj):
    chat_id = obj.chat.id if hasattr(obj, 'chat') else obj.message.chat.id
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("📈 P... HAUSSE", callback_data="open_bullish"),
        InlineKeyboardButton("📉 P... BAISSE", callback_data="open_bearish")
    )
    markup.row(
        InlineKeyboardButton("❌ CLOSE P... ", callback_data="close"),
        InlineKeyboardButton("⬅️ Retour", callback_data="back_main")
    )
    bot.send_message(chat_id, "📈 Menu Position :", reply_markup=markup)
# === Fonctions pour gérer les positions ===
def send_current_position(chat_id):
    from core.utils import safe_round, safe_float
    from core.binance_client import client
    try:
        pos = None
        positions = client.futures_position_information(symbol=symbol)
        pos = next((p for p in positions if float(p["positionAmt"]) != 0), None)

        if pos:
            sens = "Hausse (LONG)" if float(pos["positionAmt"]) > 0 else "Baisse (SHORT)"
            entry = safe_float(pos["entryPrice"]) or 0.0
            qty = abs(safe_float(pos["positionAmt"]) or 0.0)
            mark = safe_float(pos["markPrice"]) or 0.0
            pnl = safe_float(pos["unRealizedProfit"])
            pnl_str = f"{'🟢 Gain' if pnl >= 0 else '🔴 Perte'} : {safe_round(pnl)} $"
            # 🔍 Récupère l’effet de levier réellement appliqué à la position
            try:
                account_info = client.futures_account()
                lev = "inconnu"
                for asset in account_info['positions']:
                    if asset['symbol'] == symbol:
                        lev = int(asset.get('leverage', "inconnu"))
                        break
            except Exception:
                lev = "inconnu"

            montant_investi = safe_round(qty * entry)
            msg = (
                f"📈 Position ouverte :\n"
                f"Type : {sens}\n"
                f"Entrée : {safe_round(entry)}\n"
                f"Montant : {montant_investi} $\n"
                f"Prix actuel : {safe_round(mark)}\n"
                f"{pnl_str}\n"
                f"⚙️ Levier utilisé : x{lev}"
            )
        else:
            msg = "Aucune position ouverte."
    except Exception as e:
        msg = f"Erreur récupération position : {e}"
    bot.send_message(chat_id, msg)
    
def send_balance(chat_id):
    from core.binance_client import client
    try:
        balance = client.futures_account_balance()
        usdt = next((b for b in balance if b["asset"] == "USDT"), None)
        msg = f"💵 Solde USDT : {usdt['balance']} $" if usdt else "Impossible de récupérer le solde USDT."
    except Exception as e:
        log_error(f"[send_balance] Erreur récupération solde : {e}\n{traceback.format_exc()}")
        msg = f"Erreur récupération solde : {e}"
    bot.send_message(chat_id, msg)

def send_take_profit(chat_id):
    from core.binance_client import client
    try:
        pos = None
        positions = client.futures_position_information(symbol=SYMBOL)
        for p in positions:
            if float(p["positionAmt"]) != 0:
                pos = p
                break
        if not pos:
            msg = "Aucune position ouverte."
        else:
            pnl = float(pos["unRealizedProfit"])
            tp_orders = [o for o in client.futures_get_open_orders(symbol=SYMBOL) if o["type"] == "TAKE_PROFIT_MARKET"]
            if tp_orders:
                tp_price = tp_orders[0]["stopPrice"]
                msg = (
                    f"{'🟢 Marché gagnant' if pnl >= 0 else '🔴 Marché perdant'} de {round(abs(pnl), 4)} $\n"
                    f"Take Profit actuel : {tp_price}"
                )
            else:
                msg = f"{'🟢 Marché gagnant' if pnl >= 0 else '🔴 Marché perdant'} de {round(abs(pnl), 4)} $\nAucun Take Profit actif."
    except Exception as e:
        log_error(f"[send_take_profit] Erreur : {e}\n{traceback.format_exc()}")
        msg = f"Erreur récupération Take Profit : {e}"
    bot.send_message(chat_id, msg)

def send_stop_loss(chat_id):
    from core.binance_client import client
    from core.utils import safe_float
    try:
        positions = client.futures_position_information(symbol=SYMBOL)
        pos = next((p for p in positions if float(p["positionAmt"]) != 0), None)
        if not pos:
            bot.send_message(chat_id, "Aucune position ouverte.")
            return
        entry = float(pos["entryPrice"])
        mark = float(pos["markPrice"])
        sens = 1 if float(pos["positionAmt"]) > 0 else -1
        pnl_pct = ((mark - entry) / entry) * 100 * sens

        # Chercher le stop loss actif
        sl_orders = [o for o in client.futures_get_open_orders(symbol=SYMBOL) if o["type"] == "STOP_MARKET"]
        if sl_orders:
            tp_price = sl_orders[0]["stopPrice"]
            msg = (
                f"{'🟢 Position gagnante' if pnl_pct >= 0 else '🔴 Position perdante'} de {round(pnl_pct, 2)} %\n"
                f"Stop Loss actuel : {tp_price}\n"
                "Veux-tu changer de stop loss ?\n"
                "Réponds 1 pour OUI, 2 pour NON."
            )
        else:
            msg = (
                f"{'🟢 Position gagnante' if pnl_pct >= 0 else '🔴 Position perdante'} de {round(pnl_pct, 2)} %\n"
                "Aucun Stop Loss actif.\n"
                "Veux-tu en placer un ?\n"
                "Réponds 1 pour OUI, 2 pour NON."
            )
        bot.send_message(chat_id, msg)
        bot.register_next_step_handler_by_chat_id(chat_id, handle_sl_change, pos)
    except Exception as e:
        log_error(f"[send_stop_loss] Erreur : {e}\n{traceback.format_exc()}")
        bot.send_message(chat_id, f"Erreur récupération Stop Loss : {e}")

def handle_sl_change(message, pos):
    text = message.text.strip()
    main_buttons = [
        "📊 Statut", "📈 Trader", "🔄 Mode AUTO", "🔔 Mode ALERT",
        "💰 Alertes de gains", "❓ Aide", "🪙 Levier & Solde", "📚 Plus ➡️"
    ]
    # Si l'utilisateur clique sur un bouton du menu principal
    if text in main_buttons:
        handle_main_keyboard(message)
        return
    
    # Si l'utilisateur répond "1" ou "2"
    if message.text.strip() == "1":
        entry = float(pos["entryPrice"])
        mark = float(pos["markPrice"])
        sens = 1 if float(pos["positionAmt"]) > 0 else -1
        pnl_pct = ((mark - entry) / entry) * 100 * sens
        if pnl_pct <= 0:
            bot.reply_to(message, "Impossible : la position n'est pas gagnante.")
            return
        bot.reply_to(message, f"La position est gagnante de {round(pnl_pct,2)}%. À combien de % veux-tu sécuriser tes gains ? (ex: 0.2)")
        bot.register_next_step_handler(message, set_new_sl, pos)
    elif message.text.strip() == "2":
        bot.reply_to(message, "Aucun changement effectué.")
    else:
        log_error(f"[handle_sl_change] Valeur non prise en charge : {message.text.strip()}")
        bot.reply_to(message, "Valeur non prise en charge.")
        ask_user(bot, message.chat.id, "Réponds 1 pour OUI, 2 pour NON.", lambda m: handle_sl_change(m, pos))


def set_new_sl(message, pos):
    try:
        # Remplace la virgule par un point pour accepter les deux formats
        percent_str = message.text.strip().replace(',', '.')
        percent = float(percent_str)
        entry = float(pos["entryPrice"])
        mark = float(pos["markPrice"])
        sens = 1 if float(pos["positionAmt"]) > 0 else -1
        pnl_pct = ((mark - entry) / entry) * 100 * sens

        # Cas valeur positive : autorisé seulement si position gagnante
        if percent > 0:
            if pnl_pct <= 0:
                bot.reply_to(message, "Impossible : la position n'est pas gagnante, tu ne peux pas placer un SL positif.")
                return
            new_sl = entry * (1 + sens * percent / 100)
        else:
            # SL à -X% de perte (toujours autorisé)
            new_sl = entry * (1 + sens * percent / 100)

        client.futures_create_order(
            symbol=SYMBOL,
            side="SELL" if sens == 1 else "BUY",
            type="STOP_MARKET",
            stopPrice=round(new_sl, 4),
            closePosition=True,
            timeInForce="GTC"
        )

        bot.reply_to(
            message,
            f"✅ Nouveau Stop Loss placé à {round(new_sl, 4)} ({percent}% {'gain' if percent > 0 else 'perte'})."
        )
    except Exception as e:
        log_error(f"[set_new_sl] Erreur : {e}\n{traceback.format_exc()}")
        bot.reply_to(message, "❌ Valeur invalide, réessaie avec un nombre (ex: -0.6 ou 0.2).")
        ask_user(bot, message.chat.id, "À combien de % veux-tu placer ton stop loss ? (ex: -0.6 ou 0.2)", lambda m: set_new_sl(m, pos))
# Fonctions pour sauvegarder les valeurs
def save_leverage(message):
    # ...imports...
    text = message.text.strip()
    main_buttons = [
        "📊 Statut", "📈 Trader", "🔄 Mode AUTO", "🔔 Mode ALERT",
        "💰 Alertes de gains", "❓ Aide", "🪙 Levier & Solde", "📚 Plus ➡️"
    ]
    if text in main_buttons:
        handle_main_keyboard(message)
        return
    try:
        lev = int(message.text.strip())
        with open("leverage.txt", "w") as f:
            f.write(str(lev))
        bot.reply_to(message, f"✅ Levier changé à x{lev}. Il sera utilisé au prochain trade.")
    except Exception as e:
        log_error(f"[save_leverage] Erreur avec entrée '{message.text.strip()}' : {e}\n{traceback.format_exc()}")
        bot.reply_to(message, "❌ Valeur de levier invalide. Réessaie avec un nombre entier.")

def save_quantity(message):
    # ...imports...
    text = message.text.strip()
    main_buttons = [
        "📊 Statut", "📈 Trader", "🔄 Mode AUTO", "🔔 Mode ALERT",
        "💰 Alertes de gains", "❓ Aide", "🪙 Levier & Solde", "📚 Plus ➡️"
    ]
    if text in main_buttons:
        handle_main_keyboard(message)
        return
    try:
        qty = float(message.text.strip())
        with open("quantity.txt", "w") as f:
            f.write(str(qty))
        bot.reply_to(message, f"✅ Quantité changée à {qty} USDT. Elle sera utilisée au prochain trade.")
    except Exception as e:
        log_error(f"[save_quantity] Erreur avec entrée '{message.text.strip()}' : {e}\n{traceback.format_exc()}")
        bot.reply_to(message, "❌ Valeur de quantité invalide. Réessaie avec un nombre.")

# Fonction pour lire la quantité
def read_quantity():
    try:
        with open("quantity.txt", "r") as f:
            return float(f.read().strip())
    except Exception:
        return default_quantity_usdt  # valeur par défaut

# Fonction pour lire le levier
def read_leverage():
    try:
        with open("leverage.txt", "r") as f:
            return int(f.read().strip())
    except Exception:
        return default_leverage  # valeur par défaut

user_trade_context = {}  # stocke le contexte par chat_id

@bot.callback_query_handler(func=lambda call: call.data in ['open_bullish', 'open_bearish'])
def handle_trade_callbacks(call):
    from core.state import state

    chat_id = call.message.chat.id

    if state.position_open:
        bot.send_message(chat_id, "⚠ Une position est déjà ouverte. Fermez-la avant d’en ouvrir une autre.")
        return

    direction = "bullish" if call.data == "open_bullish" else "bearish"
    user_trade_context[chat_id] = {"direction": direction}
    save_user_trade_context()


    ask_user(bot, chat_id, "💬 Envoie-moi la quantité en USDT (ex: 5) :", receive_quantity)



def receive_quantity(message):
    chat_id = message.chat.id
    text = message.text.strip()
    main_buttons = [
        "📊 Statut", "📈 Trader", "🔄 Mode AUTO", "🔔 Mode ALERT",
        "💰 Alertes de gains", "❓ Aide", "🪙 Levier & Solde", "📚 Plus ➡️"
    ]
    # Ajoute cette vérification !
    if text in main_buttons:
        handle_main_keyboard(message)
        return
    try:
        quantity = float(text)
        if quantity <= 0:
            raise ValueError("Quantité invalide")
    except Exception as e:
        log_error(f"[receive_quantity] Erreur avec entrée '{text}' du chat {chat_id} : {e}\n{traceback.format_exc()}")
        bot.reply_to(message, "❌ Quantité invalide. Envoie un nombre positif.")
        ask_user(bot, chat_id, "💬 Envoie-moi la quantité en USDT (ex: 5) :", receive_quantity)
        return

    user_trade_context[chat_id]["quantity"] = quantity
    save_user_trade_context()
    ask_user(bot, chat_id, "💬 Envoie-moi le levier (ex: 10) :", receive_leverage)


def receive_leverage(message):
    from core.binance_client import client
    from core.trade_interface import open_trade
    chat_id = message.chat.id
    text = message.text.strip()
    main_buttons = [
        "📊 Statut", "📈 Trader", "🔄 Mode AUTO", "🔔 Mode ALERT",
        "💰 Alertes de gains", "❓ Aide", "🪙 Levier & Solde", "📚 Plus ➡️"
    ]
    if text in main_buttons:
        handle_main_keyboard(message)
        return
    try:
        leverage = int(text)
        if leverage <= 0:
            raise ValueError("Levier invalide")
    except Exception as e:
        log_error(f"[receive_leverage] Erreur avec entrée '{text}' du chat {chat_id} : {e}\n{traceback.format_exc()}")
        bot.reply_to(message, "❌ Levier invalide. Envoie un nombre entier positif.")
        ask_user(bot, chat_id, "💬 Envoie-moi le levier (ex: 10) :", receive_leverage)
        return

    context = user_trade_context.pop(chat_id, None)
    save_user_trade_context()
    if not context or "quantity" not in context:
        bot.send_message(chat_id, "❌ Erreur interne : quantité manquante ou contexte de trading introuvable. Recommence la procédure.")
        return

    direction = context["direction"]
    capital = context["quantity"]

    try:
        open_trade(direction, quantity=capital, leverage=leverage)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Erreur ouverture position : {e}")

# Pour démarrer le bot
def start_bot():
    print("🤖 Bot Telegram démarré...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)

def stop_telegram_bot():
    print("🔴 Arrêt du bot Telegram demandé...")
    bot.stop_polling()
    print("🟢 Bot Telegram arrêté.")


if __name__ == "__main__":
    start_bot()
