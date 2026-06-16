import os
import urllib.parse
import requests


def send_whatsapp(message):
    phone = os.environ.get("CALLMEBOT_PHONE")
    apikey = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not apikey:
        print("[WHATSAPP] Secrets nao configurados")
        return False
    try:
        encoded = urllib.parse.quote_plus(message)
        url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded}&apikey={apikey}"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            print("[WHATSAPP] Notificacao enviada")
            return True
        print(f"[WHATSAPP] Falha: {resp.status_code}")
        return False
    except Exception as e:
        print(f"[WHATSAPP] Erro: {e}")
        return False
