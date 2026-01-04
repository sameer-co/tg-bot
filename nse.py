import requests

# Your credentials here (keep private!)
TOKEN = "7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs"
CHAT_ID = "1950462171"

def ping_telegram_bot(token, chat_id, message="🏓 Pong! Bot ping test successful."):
    """
    Pings Telegram bot by sending a test message to chat_id.
    Returns True if sent successfully.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message
    }
    try:
        response = requests.post(url, data=payload)
        data = response.json()
        if data.get('ok'):
            print("✅ Ping sent successfully!")
            print(f"Message ID: {data['result']['message_id']}")
            return True
        else:
            print(f"❌ Ping failed. Error: {data.get('description', 'Unknown')}")
            return False
    except Exception as e:
        print(f"❌ Error sending ping: {e}")
        return False

if __name__ == "__main__":
    ping_telegram_bot(TOKEN, CHAT_ID)
