import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext
from dotenv import load_dotenv
import telebot

load_dotenv()

# Récupération des variables d'environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # clé du bot
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # id du chat (pas utilisé ici mais peut servir)
MODE_FILE = "mode.txt"
GAIN_ALERT_FILE = "gain_alert.txt"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

def read_gain_alert():
    try:
        with open(GAIN_ALERT_FILE, "r") as f:
            return f.read().strip() == "on"
    except FileNotFoundError:
        return True  # par défaut activé

def write_gain_alert(value):
    with open(GAIN_ALERT_FILE, "w") as f:
        f.write("on" if value else "off")

def toggle_gain_alert(update: Update, context: CallbackContext):
    current = read_gain_alert()
    new_state = not current
    write_gain_alert(new_state)
    state_msg = "✅ Alertes de gains ACTIVÉES ✅" if new_state else "🚫 Alertes de gains DÉSACTIVÉES 🚫"
    update.message.reply_text(state_msg)

def read_mode():
    try:
        with open(MODE_FILE, "r") as f:
            mode = f.read().strip()
        if mode not in ["auto", "alert"]:
            return "auto"  # mode par défaut
        return mode
    except FileNotFoundError:
        return "auto"

def write_mode(mode):
    with open(MODE_FILE, "w") as f:
        f.write(mode)

def start(update: Update, context: CallbackContext):
    keyboard = [
        [
            InlineKeyboardButton("🤖 Mode Auto (trade + alertes)", callback_data="auto"),
            InlineKeyboardButton("🔔 Mode Alerte uniquement", callback_data="alert"),
        ],
        [
            InlineKeyboardButton("❌ Fermer la position", callback_data="close_position")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text("👇 *Choisis le mode d'exécution du bot* :", reply_markup=reply_markup, parse_mode="Markdown")

def button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    mode = query.data

    if mode == "close_position":
        with open("manual_close_request.txt", "w") as f:
            f.write("yes")
        query.edit_message_text("🔴 Demande de FERMETURE MANUELLE envoyée au bot.")
        print("Demande de fermeture manuelle envoyée.")  # Logging
        return

    if mode not in ["auto", "alert"]:
        query.edit_message_text(text="Mode inconnu, essaie encore.")
        print(f"Mode inconnu reçu : {mode}")  # Logging
        return

    write_mode(mode)
    print(f"Mode changé en : {mode}")  # Logging

    if mode == "auto":
        reply_text = "✅ Mode TRADE AUTOMATIQUE + alertes activé."
    else:
        reply_text = "⚠ Mode ALERTE uniquement activé."

    query.edit_message_text(text=reply_text)

@bot.message_handler(commands=['status'])
def status(message):
    # Ici, tu dois lire l'état depuis un fichier ou une variable partagée
    msg = "Commande /status reçue (à personnaliser selon ton système de partage d'état)"
    bot.reply_to(message, msg)

@bot.message_handler(commands=['close'])
def close(message):
    with open("manual_close_request.txt", "w") as f:
        f.write("close")
    bot.reply_to(message, "Demande de fermeture manuelle envoyée au bot de trading.")

@bot.message_handler(commands=['mode'])
def mode(message):
    try:
        mode_value = message.text.split(" ")[1].lower()
        if mode_value in ["auto", "alert"]:
            with open("mode.txt", "w") as f:
                f.write(mode_value)
            bot.reply_to(message, f"Mode changé en {mode_value.upper()}")
        else:
            bot.reply_to(message, "Mode inconnu. Utilisez /mode auto ou /mode alert.")
    except Exception:
        bot.reply_to(message, "Utilisation : /mode auto ou /mode alert")

@bot.message_handler(commands=['help'])
def help(message):
    help_msg = (
        "/status - Voir l'état du bot\n"
        "/close - Fermer la position en cours\n"
        "/mode auto - Passer en mode automatique\n"
        "/mode alert - Passer en mode alerte\n"
        "/help - Afficher cette aide"
    )
    bot.reply_to(message, help_msg)

def main():
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("gain_alert", toggle_gain_alert))  # Ajout ici
    dispatcher.add_handler(CallbackQueryHandler(button))

    print("Bot Telegram lancé.")  # Logging
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
    bot.polling()