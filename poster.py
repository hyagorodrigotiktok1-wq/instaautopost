import os
import json
import time
import base64
import urllib.parse
import requests
from datetime import datetime, timezone, timedelta

try:
    from nacl.public import PublicKey, SealedBox
    HAS_NACL = True
except ImportError:
    HAS_NACL = False


GRAPH_API = "https://graph.instagram.com/v21.0"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATA_FILE = os.path.join(DATA_DIR, "posts.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")
ANALYTICS_FILE = os.path.join(DATA_DIR, "analytics.json")
MAX_LOGS = 50
MAX_RETRIES = 3
COOLDOWN_MINUTES = 120
NON_RETRYABLE = ["not found in secrets", "Duplicata detectada"]


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
            f"{GRAPH_API}/me",
            params={"fields": "id,username", "access_token": token},
            timeout=15,
        )
        if resp.status_code == 200:
            username = resp.json().get("username", account_name)
            print(f"[TOKEN] {account_name} (@{username}): OK")
            return True
        error_msg = ""
        try:
            error_msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            pass
        send_whatsapp(
            f"🔑 InstaAutoPost: Token da conta '{account_name}' INVALIDO!\n"
            f"Erro: {error_msg or resp.status_code}\n"
            f"Renove o token agora ou os posts vao falhar!"
        )
        print(f"[TOKEN] {account_name}: FALHOU ({resp.status_code}) {error_msg}")
        return False
    except Exception as e:
        print(f"[TOKEN] Erro ao verificar token de {account_name}: {e}")
        return False


# ===== AUTO-REFRESH TOKEN =====

def refresh_token(account_name, token):
    app_id = os.environ.get("FB_APP_ID")
    app_secret = os.environ.get("FB_APP_SECRET")
    if not app_id or not app_secret:
        return None
    try:
        resp = requests.get(
            "https://graph.facebook.com/v21.0/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": token,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            new_token = resp.json().get("access_token")
            if new_token and new_token != token:
                print(f"[REFRESH] {account_name}: token renovado!")
                return new_token
            print(f"[REFRESH] {account_name}: token ainda valido, sem renovacao")
        else:
            err = resp.json().get("error", {}).get("message", str(resp.status_code))
            print(f"[REFRESH] {account_name}: falha - {err}")
        return None
    except Exception as e:
        print(f"[REFRESH] {account_name}: erro - {e}")
        return None


def update_github_secret(secret_name, secret_value):
    if not HAS_NACL:
        print("[GITHUB] pynacl nao instalado, impossivel atualizar secret automaticamente")
        return False
    gh_token = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not gh_token or not repo:
        print("[GITHUB] GH_PAT ou GITHUB_REPOSITORY nao configurado")
        return False
    try:
        headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        key_data = resp.json()

        public_key = PublicKey(base64.b64decode(key_data["key"]))
        sealed = SealedBox(public_key)
        encrypted = base64.b64encode(sealed.encrypt(secret_value.encode())).decode()

        resp = requests.put(
            f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
            headers=headers,
            json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
            timeout=15,
        )
        resp.raise_for_status()
        print(f"[GITHUB] Secret '{secret_name}' atualizado automaticamente")
        return True
    except Exception as e:
        print(f"[GITHUB] Erro ao atualizar '{secret_name}': {e}")
        return False


# ===== POSTING =====

def is_duplicate(post, posts):
    for p in posts:
        if p.get("id") == post.get("id"):
            continue
        if (p.get("status") == "posted"
                and p.get("video_url") == post.get("video_url")
                and p.get("account", "default") == post.get("account", "default")):
            return True
    return False


def is_retryable(error_str):
    return not any(pat in error_str for pat in NON_RETRYABLE)


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


# ===== ANALYTICS =====

def fetch_insights(media_id, token):
    metrics = "ig_reels_aggregated_all_plays_count,likes,comments,shares,reach,saved"
    try:
        resp = requests.get(
            f"{GRAPH_API}/{media_id}/insights",
            params={"metric": metrics, "access_token": token},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        result = {}
        for item in data:
            name = item["name"]
            if name == "ig_reels_aggregated_all_plays_count":
                name = "plays"
            values = item.get("values", [{}])
            result[name] = values[0].get("value", 0) if values else 0
        return result
    except Exception as e:
        print(f"[INSIGHTS] Erro para {media_id}: {e}")
        return None


def update_analytics(posts, accounts):
    analytics = {}
    if os.path.exists(ANALYTICS_FILE):
        try:
            with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                analytics = json.load(f)
        except (json.JSONDecodeError, IOError):
            analytics = {}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    updated = False

    posted = [p for p in posts if p.get("status") == "posted" and p.get("media_id")]
    for post in posted:
        posted_at = None
        if post.get("posted_at"):
            posted_at = datetime.fromisoformat(post["posted_at"])
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
        if not posted_at or posted_at < cutoff:
            continue

        account_name = post.get("account", "default")
        if account_name not in accounts:
            continue

        token = accounts[account_name]["token"]
        insights = fetch_insights(post["media_id"], token)
        if insights:
            brt = now + timedelta(hours=-3)
            analytics[post["id"]] = {
                "media_id": post["media_id"],
                "account": account_name,
                "caption": (post.get("caption", ""))[:100],
                "posted_at": post.get("posted_at", ""),
                **insights,
                "updated_at": now.isoformat(),
                "updated_brt": brt.strftime("%d/%m/%Y %H:%M"),
            }
            updated = True
            print(f"[INSIGHTS] {post['id']}: {insights}")

    if updated:
        with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
            json.dump(analytics, f, ensure_ascii=False, indent=2)


# ===== COOLDOWN =====

def get_last_post_time(posts, account_name):
    times = []
    for p in posts:
        if p.get("status") == "posted" and p.get("posted_at") and p.get("account", "default") == account_name:
            try:
                dt = datetime.fromisoformat(p["posted_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                times.append(dt)
            except ValueError:
                pass
    return max(times) if times else None


def check_cooldown(posts, account_name, now):
    last = get_last_post_time(posts, account_name)
    if not last:
        return True, 0
    elapsed = (now - last).total_seconds() / 60
    if elapsed < COOLDOWN_MINUTES:
        return False, int(COOLDOWN_MINUTES - elapsed)
    return True, 0


# ===== VIRAL ALERT =====

def check_viral_reels(analytics):
    entries = list(analytics.values())
    if len(entries) < 3:
        return
    plays = [e.get("plays", 0) for e in entries if e.get("plays", 0) > 0]
    if not plays:
        return
    avg_plays = sum(plays) / len(plays)
    if avg_plays == 0:
        return
    for entry in entries:
        p = entry.get("plays", 0)
        if p >= avg_plays * 3 and not entry.get("viral_alerted"):
            caption = (entry.get("caption", "Sem legenda"))[:50]
            account = entry.get("account", "default")
            send_whatsapp(
                f"🔥 REEL VIRAL DETECTADO!\n"
                f"Conta: @{account}\n"
                f'"{caption}"\n'
                f"▶️ {p} plays (media: {int(avg_plays)})\n"
                f"Responda os comentarios AGORA para amplificar o alcance!"
            )
            entry["viral_alerted"] = True
            print(f"[VIRAL] Alerta enviado: {caption} ({p} plays, media {int(avg_plays)})")


# ===== SILENT FAILURE ALERT =====

def check_missed_posts(posts, now):
    brt = now + timedelta(hours=-3)
    missed = []
    for p in posts:
        if p.get("status") != "pending":
            continue
        try:
            scheduled = datetime.fromisoformat(p["scheduled_at"])
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        hours_overdue = (now - scheduled).total_seconds() / 3600
        if hours_overdue > 1:
            missed.append({"id": p["id"], "account": p.get("account", "default"), "hours": int(hours_overdue)})
    if missed:
        lines = [f"⚠️ ALERTA: {len(missed)} post(s) atrasado(s)!\n"]
        for m in missed[:5]:
            lines.append(f"• {m['id']} (@{m['account']}) - {m['hours']}h atrasado")
        if len(missed) > 5:
            lines.append(f"... e mais {len(missed) - 5}")
        lines.append("\nVerifique se o cron-job.org esta funcionando!")
        send_whatsapp("\n".join(lines))
        print(f"[ALERT] {len(missed)} posts atrasados detectados")


# ===== LOGS =====

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


# ===== MAIN =====

def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        posts = json.load(f)

    accounts = get_accounts()
    if not accounts:
        print("No Instagram accounts configured.")
        return

    # 1. Check token health
    for name, acct in accounts.items():
        check_token_health(name, acct["token"])

    # 2. Auto-refresh tokens
    if os.environ.get("FB_APP_ID") and os.environ.get("FB_APP_SECRET"):
        for name, acct in accounts.items():
            new_token = refresh_token(name, acct["token"])
            if new_token:
                secret_name = ("INSTAGRAM_ACCESS_TOKEN" if name == "default"
                               else f"INSTA_ACCOUNT_{name.upper()}_TOKEN")
                if update_github_secret(secret_name, new_token):
                    acct["token"] = new_token
                    send_whatsapp(
                        f"🔄 InstaAutoPost: Token da conta '{name}' renovado automaticamente! "
                        f"Novo prazo: +60 dias."
                    )
                else:
                    send_whatsapp(
                        f"⚠️ InstaAutoPost: Token da conta '{name}' foi renovado mas NAO foi possivel "
                        f"salvar automaticamente. Atualize o secret manualmente!"
                    )
    else:
        print("[REFRESH] FB_APP_ID/FB_APP_SECRET nao configurados, auto-refresh desativado")

    # 3. Post pending reels
    now = datetime.now(timezone.utc)
    modified = False
    posted_count = 0
    failed_count = 0
    retried_count = 0
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
            continue

        account_name = post.get("account", "default")
        if account_name not in accounts:
            post["status"] = "failed"
            post["error"] = f"Account '{account_name}' not found in secrets"
            modified = True
            failed_count += 1
            log_details.append(f"[FAIL] {post['id']}: conta '{account_name}' nao configurada")
            continue

        if is_duplicate(post, posts):
            post["status"] = "failed"
            post["error"] = "Duplicata detectada: mesmo video ja publicado nesta conta"
            modified = True
            failed_count += 1
            log_details.append(f"[DEDUP] {post['id']}: video ja publicado")
            continue

        can_post, wait_min = check_cooldown(posts, account_name, now)
        if not can_post:
            skipped_count += 1
            log_details.append(f"[COOLDOWN] {post['id']}: aguardando {wait_min}min para @{account_name}")
            print(f"[COOLDOWN] {post['id']}: faltam {wait_min}min de intervalo para @{account_name}")
            continue

        acct = accounts[account_name]
        caption = post.get("caption", "")
        if post.get("hashtags"):
            caption += "\n\n" + post["hashtags"]

        retry_count = post.get("retry_count", 0)
        try:
            print(f"[POST] {post['id']} -> @{account_name}" +
                  (f" (tentativa {retry_count + 1})" if retry_count else ""))
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
            post.pop("retry_count", None)
            post.pop("error", None)
            posted_count += 1
            log_details.append(f"[OK] {post['id']} -> @{account_name} (media {media_id})")
            print(f"[OK]   {post['id']} -> media {media_id}")

        except Exception as e:
            error_str = str(e)
            if is_retryable(error_str) and retry_count < MAX_RETRIES - 1:
                post["status"] = "pending"
                post["retry_count"] = retry_count + 1
                post["error"] = f"Tentativa {retry_count + 1}/{MAX_RETRIES}: {error_str[:200]}"
                retried_count += 1
                log_details.append(f"[RETRY] {post['id']}: tentativa {retry_count + 1}")
                print(f"[RETRY] {post['id']}: tentativa {retry_count + 1} - {e}")
            else:
                post["status"] = "failed"
                post["error"] = error_str
                failed_count += 1
                log_details.append(f"[FAIL] {post['id']}: {error_str[:200]}")
                print(f"[FAIL] {post['id']}: {e}")
                send_whatsapp(
                    f"❌ InstaAutoPost FALHOU\n"
                    f"Post: {post['id']}\n"
                    f"Conta: @{account_name}\n"
                    f"Erro: {error_str[:200]}"
                )

        modified = True

    if modified:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False, indent=2)

    # 4. Fetch analytics for posted reels
    update_analytics(posts, accounts)

    # 5. Detect viral reels
    if os.path.exists(ANALYTICS_FILE):
        try:
            with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                analytics = json.load(f)
            check_viral_reels(analytics)
            with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
                json.dump(analytics, f, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, IOError):
            pass

    # 6. Check for missed/overdue posts
    check_missed_posts(posts, now)

    # 7. Save execution log
    brt_now = now + timedelta(hours=-3)
    log_entry = {
        "timestamp": now.isoformat(),
        "timestamp_brt": brt_now.strftime("%d/%m/%Y %H:%M"),
        "posted": posted_count,
        "failed": failed_count,
        "retried": retried_count,
        "skipped": skipped_count,
        "total_pending": sum(1 for p in posts if p["status"] == "pending"),
        "details": log_details,
    }
    save_log(log_entry)

    print(f"\nDone. {posted_count} posted, {failed_count} failed, "
          f"{retried_count} retrying, "
          f"{sum(1 for p in posts if p['status'] == 'pending')} still pending.")


if __name__ == "__main__":
    main()
