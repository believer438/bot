from telebot import TeleBot
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

bot = TeleBot(TELEGRAM_TOKEN)

def send_telegram(message):
    """
    Envoie un message Telegram à ton chat configuré
    """
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            bot.send_message(CHAT_ID, message)
        except Exception as e:
            print(f"Erreur Telegram : {e}")
    else:
        print("⚠️ Token ou Chat ID manquant dans le fichier .env")
