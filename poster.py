import os
import json
import time
import urllib.parse
import requests
from datetime import datetime, timezone, timedelta


GRAPH_API = "https://graph.instagram.com/v21.0"
FB_GRAPH_API = "https://graph.facebook.com/v21.0"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "posts.json")
LOGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs.json")
MAX_LOGS = 50


def get_accounts():
    accounts = {}

    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    uid = os.environ.get("INSTAGRAM_USER_ID")
    if token and uid:
        accounts["default"] = {"token": token, "user_id": uid}

    prefix = "INSTA_ACCOUNT_"
    for key in os.environ:
        if key.startswith(prefix) and key.endswith("_TOKEN"):
            name = key[len(prefix):-6].lower()
            uid_key = f"{prefix}{name.upper()}_USER_ID"
            uid = os.environ.get(uid_key)
            if uid:
                accounts[name] = {"token": os.environ[key], "user_id": uid}

    return accounts


def send_whatsapp(message):
    phone = os.environ.get("CALLMEBOT_PHONE")
    apikey = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not apikey:
        print("[WHATSAPP] Credenciais nao configuradas, pulando notificacao")
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


def check_token_health(account_name, token):
    try:
        resp = requests.get(
            f"{FB_GRAPH_API}/debug_token",
            params={"input_token": token, "access_token": token},
            timeout=15,
        )
        if resp.status_code != 200:
            send_whatsapp(
                f"⚠️ InstaAutoPost: Token da conta '{account_name}' pode estar invalido "
                f"(debug_token retornou {resp.status_code})"
            )
            return
        data = resp.json().get("data", {})
        expires_at = data.get("expires_at", 0)
        if expires_at == 0:
            return
        expires = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        days_left = (expires - now).days
        if days_left <= 7:
            brt_expiry = expires + timedelta(hours=-3)
            send_whatsapp(
                f"🔑 InstaAutoPost: Token da conta '{account_name}' expira em {days_left} dia(s)!\n"
                f"Expira em {brt_expiry.strftime('%d/%m/%Y %H:%M')} BRT.\n"
                f"Renove o token agora!"
            )
            print(f"[TOKEN] {account_name}: expira em {days_left} dias - alerta enviado")
        else:
            print(f"[TOKEN] {account_name}: OK ({days_left} dias restantes)")
    except Exception as e:
        print(f"[TOKEN] Erro ao verificar token de {account_name}: {e}")


def is_duplicate(post, posts):
    for p in posts:
        if p.get("id") == post.get("id"):
            continue
        if (p.get("status") == "posted"
                and p.get("video_url") == post.get("video_url")
                and p.get("account", "default") == post.get("account", "default")):
            return True
    return False


def create_reel(user_id, token, video_url, caption, cover_url=None, thumb_offset=None):
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": token,
        "share_to_feed": "true",
    }
    if cover_url:
        payload["cover_url"] = cover_url
    if thumb_offset is not None:
        payload["thumb_offset"] = str(thumb_offset)

    resp = requests.post(f"{GRAPH_API}/{user_id}/media", data=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def wait_for_container(container_id, token, max_wait=300):
    for _ in range(max_wait // 10):
        resp = requests.get(
            f"{GRAPH_API}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=15,
        )
        resp.raise_for_status()
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return True
        if status == "ERROR":
            detail = resp.json().get("status", "unknown error")
            raise RuntimeError(f"Container error: {detail}")
        time.sleep(10)
    raise TimeoutError("Video processing took too long")


def publish(user_id, token, container_id):
    resp = requests.post(
        f"{GRAPH_API}/{user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def save_log(entry):
    logs = []
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except (json.JSONDecodeError, IOError):
            logs = []
    logs.insert(0, entry)
    logs = logs[:MAX_LOGS]
    with open(LOGS_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        posts = json.load(f)

    accounts = get_accounts()
    if not accounts:
        print("No Instagram accounts configured.")
        return

    for name, acct in accounts.items():
        check_token_health(name, acct["token"])

    now = datetime.now(timezone.utc)
    modified = False
    posted_count = 0
    failed_count = 0
    skipped_count = 0
    log_details = []

    pending = [p for p in posts if p.get("status") == "pending"]
    pending.sort(key=lambda p: p["scheduled_at"])

    for post in pending:
        scheduled = datetime.fromisoformat(post["scheduled_at"])
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)

        if scheduled > now:
            skipped_count += 1
            log_details.append(f"[SKIP] {post['id']} agendado para depois")
            print(f"[SKIP] {post['id']} scheduled for later, next run will handle it.")
            continue

        account_name = post.get("account", "default")
        if account_name not in accounts:
            post["status"] = "failed"
            post["error"] = f"Account '{account_name}' not found in secrets"
            modified = True
            failed_count += 1
            log_details.append(f"[FAIL] {post['id']}: conta '{account_name}' nao configurada")
            print(f"[SKIP] {post['id']}: account '{account_name}' not configured")
            continue

        if is_duplicate(post, posts):
            post["status"] = "failed"
            post["error"] = "Duplicata detectada: mesmo video ja publicado nesta conta"
            modified = True
            failed_count += 1
            msg = f"[DEDUP] {post['id']}: video ja publicado nesta conta"
            log_details.append(msg)
            print(msg)
            continue

        acct = accounts[account_name]
        caption = post.get("caption", "")
        if post.get("hashtags"):
            caption += "\n\n" + post["hashtags"]

        try:
            print(f"[POST] {post['id']} -> @{account_name}")
            container_id = create_reel(
                acct["user_id"], acct["token"],
                post["video_url"], caption,
                post.get("cover_url"), post.get("thumb_offset"),
            )
            post["status"] = "processing"
            wait_for_container(container_id, acct["token"])
            media_id = publish(acct["user_id"], acct["token"], container_id)

            post["status"] = "posted"
            post["posted_at"] = now.isoformat()
            post["media_id"] = media_id
            posted_count += 1
            log_details.append(f"[OK] {post['id']} -> @{account_name} (media {media_id})")
            print(f"[OK]   {post['id']} -> media {media_id}")

        except Exception as e:
            post["status"] = "failed"
            post["error"] = str(e)
            failed_count += 1
            error_msg = str(e)[:200]
            log_details.append(f"[FAIL] {post['id']}: {error_msg}")
            print(f"[FAIL] {post['id']}: {e}")
            send_whatsapp(
                f"❌ InstaAutoPost FALHOU\n"
                f"Post: {post['id']}\n"
                f"Conta: @{account_name}\n"
                f"Erro: {error_msg}"
            )

        modified = True

    if modified:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False, indent=2)

    brt_now = now + timedelta(hours=-3)
    log_entry = {
        "timestamp": now.isoformat(),
        "timestamp_brt": brt_now.strftime("%d/%m/%Y %H:%M"),
        "posted": posted_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "total_pending": sum(1 for p in posts if p["status"] == "pending"),
        "details": log_details,
    }
    save_log(log_entry)

    print(f"\nDone. {posted_count} posted, {failed_count} failed, "
          f"{sum(1 for p in posts if p['status'] == 'pending')} still pending.")


if __name__ == "__main__":
    main()
