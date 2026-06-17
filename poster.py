import os
import json
import time
import base64
import requests
from datetime import datetime, timezone, timedelta
from utils import send_whatsapp

try:
    from nacl.public import PublicKey, SealedBox
    HAS_NACL = True
except ImportError:
    HAS_NACL = False


GRAPH_API = "https://graph.facebook.com/v21.0"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATA_FILE = os.path.join(DATA_DIR, "posts.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")
ANALYTICS_FILE = os.path.join(DATA_DIR, "analytics.json")
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.json")
MAX_LOGS = 50
MAX_RETRIES = 3
COOLDOWN_MINUTES = 30
ARCHIVE_DAYS = 30
NON_RETRYABLE = ["not found in secrets", "Duplicata detectada"]
REPLIES_FILE = os.path.join(DATA_DIR, "replied_comments.json")
MAX_REPLIES_PER_CYCLE = 15
REPLY_COOLDOWN_SECONDS = 4


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


def check_token_health(account_name, token, user_id=None):
    try:
        endpoint = f"{GRAPH_API}/{user_id}" if user_id else f"{GRAPH_API}/me"
        resp = requests.get(
            endpoint,
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
    if resp.status_code != 200:
        err_body = resp.text[:500]
        print(f"[CREATE_REEL] Erro {resp.status_code}: {err_body}")
        try:
            api_msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            api_msg = ""
        raise RuntimeError(f"[{resp.status_code}] {api_msg or err_body}")
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
    if resp.status_code != 200:
        err_body = resp.text[:500]
        print(f"[PUBLISH] Erro {resp.status_code}: {err_body}")
        try:
            api_msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            api_msg = ""
        raise RuntimeError(f"[{resp.status_code}] {api_msg or err_body}")
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
            print(f"[INSIGHTS] {media_id}: HTTP {resp.status_code} - {resp.text[:200]}")
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

    old_keys = []
    for key, entry in analytics.items():
        if entry.get("posted_at"):
            try:
                dt = datetime.fromisoformat(entry["posted_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    old_keys.append(key)
            except ValueError:
                pass
    for key in old_keys:
        del analytics[key]
        updated = True
    if old_keys:
        print(f"[ANALYTICS] {len(old_keys)} entradas antigas removidas")

    fresh_cutoff = now - timedelta(days=7)
    posted = [p for p in posts if p.get("status") == "posted" and p.get("media_id")]
    for post in posted:
        posted_at = None
        if post.get("posted_at"):
            posted_at = datetime.fromisoformat(post["posted_at"])
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
        if not posted_at or posted_at < cutoff:
            continue

        if posted_at < fresh_cutoff and post["id"] in analytics:
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
                "hashtags": post.get("hashtags", ""),
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

    return analytics


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
    missed = []
    for p in posts:
        if p.get("status") != "pending":
            continue
        if p.get("missed_alert_sent"):
            continue
        try:
            scheduled = datetime.fromisoformat(p["scheduled_at"])
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        hours_overdue = (now - scheduled).total_seconds() / 3600
        if hours_overdue > 1:
            missed.append(p)
    if missed:
        lines = [f"⚠️ ALERTA: {len(missed)} post(s) atrasado(s)!\n"]
        for p in missed[:5]:
            hours = int((now - datetime.fromisoformat(p["scheduled_at"]).replace(tzinfo=timezone.utc)).total_seconds() / 3600)
            lines.append(f"• {p['id']} (@{p.get('account', 'default')}) - {hours}h atrasado")
        if len(missed) > 5:
            lines.append(f"... e mais {len(missed) - 5}")
        lines.append("\nVerifique se o cron-job.org esta funcionando!")
        send_whatsapp("\n".join(lines))
        for p in missed:
            p["missed_alert_sent"] = True
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


# ===== ARCHIVE =====

def archive_old_posts(posts, now):
    cutoff = now - timedelta(days=ARCHIVE_DAYS)
    to_archive = []
    to_keep = []
    for p in posts:
        if p.get("status") in ("posted", "failed"):
            ts = p.get("posted_at") or p.get("scheduled_at")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        to_archive.append(p)
                        continue
                except ValueError:
                    pass
        to_keep.append(p)

    if not to_archive:
        return

    archive = []
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                archive = json.load(f)
        except (json.JSONDecodeError, IOError):
            archive = []

    existing_ids = {a["id"] for a in archive if "id" in a}
    for p in to_archive:
        if p.get("id") not in existing_ids:
            archive.append(p)

    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)

    posts.clear()
    posts.extend(to_keep)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

    print(f"[ARCHIVE] {len(to_archive)} posts arquivados (total no arquivo: {len(archive)})")


# ===== AUTO-REPLY COMMENTS =====

import random
import re

REPLY_RULES = [
    {
        "keywords": ["parabéns", "parabens", "incrível", "incrivel", "sensacional", "top demais",
                      "perfeito", "maravilhoso", "espetacular", "fenomenal", "brilhante"],
        "replies": [
            "Muito obrigado pelo apoio! 🙏",
            "Valeu demais! Compartilha pra mais gente ver! 🔥",
            "Obrigado! Esse apoio faz toda a diferença! 💪",
            "Tmj! Fica ligado que tem muito mais por vir! 🚀",
        ],
    },
    {
        "keywords": ["verdade", "concordo", "exato", "exatamente", "isso mesmo", "com certeza",
                      "é isso", "eh isso", "isso aí", "fato", "realidade"],
        "replies": [
            "Exatamente! Compartilha pra mais gente acordar! 🔥",
            "É isso aí! O povo precisa saber! 💪",
            "Pura verdade! Manda pros grupos! 📢",
            "Tmj! Juntos somos mais fortes! 🇧🇷",
        ],
    },
    {
        "keywords": ["absurdo", "vergonha", "revoltante", "inadmissível", "inadmissivel",
                      "indignação", "indignacao", "nojo", "palhaçada", "palhacada", "escândalo",
                      "escandalo", "que lixo", "ridiculo", "ridículo"],
        "replies": [
            "O povo precisa reagir! Compartilha! 🔥",
            "É revoltante mesmo! Mas a informação é nossa arma! 💪",
            "Por isso não podemos ficar calados! Compartilha! 📢",
            "Inacreditável né? Mas é a realidade! Manda pra todo mundo! 🇧🇷",
        ],
    },
    {
        "keywords": ["segue", "seguindo", "novo seguidor", "novo inscrito", "acabei de seguir",
                      "comecei a seguir"],
        "replies": [
            "Seja bem-vindo(a)! Ativa o sininho pra não perder nada! 🔔",
            "Tmj! Bem-vindo(a) à família! 🙏",
            "Valeu por seguir! Compartilha com os amigos! 🔥",
        ],
    },
    {
        "keywords": ["compartilhei", "mandei", "enviei", "repostei", "passei pra frente",
                      "compartilhando", "já mandei"],
        "replies": [
            "Isso aí! Quanto mais gente souber, melhor! 🔥",
            "Valeu demais! É assim que a informação chega longe! 💪",
            "Obrigado por espalhar! Juntos fazemos a diferença! 🙏",
        ],
    },
    {
        "keywords": ["kkk", "kkkk", "kkkkk", "haha", "hahaha", "😂", "🤣", "rsrs",
                      "morrendo", "chorando de rir"],
        "replies": [
            "😂😂😂",
            "Rir pra não chorar né! 😂",
            "A realidade é tão absurda que vira piada! 🤣",
            "😂🔥",
        ],
    },
    {
        "keywords": ["🔥", "💪", "👏", "❤️", "♥️", "🙌", "👍", "🇧🇷"],
        "replies": [
            "🔥🔥🔥",
            "💪🇧🇷",
            "Tmj! 🔥",
            "Valeu! 🙏🔥",
        ],
    },
    {
        "keywords": ["?", "como", "quando", "onde", "porque", "por que", "quem", "qual",
                      "o que", "será", "sera"],
        "replies": [
            "Boa pergunta! Vamos abordar isso em breve por aqui! 🔔",
            "Fica ligado que a gente vai aprofundar esse tema! 📢",
            "Ótima questão! Ativa o sininho pra não perder! 🔔",
        ],
    },
]

GENERIC_REPLIES = [
    "Valeu pelo comentário! 🙏",
    "Tmj! 🔥",
    "Obrigado pelo engajamento! 💪",
    "💪🔥",
    "Compartilha! 📢",
]

SKIP_PATTERNS = [
    r"@\w+",
    r"https?://",
    r"compre\s",
    r"ganhe\s+dinheiro",
    r"link\s+na\s+bio",
    r"sigam?\s+@",
    r"dm\b",
    r"promoç",
    r"promoc",
]


def load_replied():
    if os.path.exists(REPLIES_FILE):
        try:
            with open(REPLIES_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
    return set()


def save_replied(replied_ids):
    recent = list(replied_ids)[-5000:]
    with open(REPLIES_FILE, "w", encoding="utf-8") as f:
        json.dump(recent, f)


def should_skip_comment(text):
    text_lower = text.lower().strip()
    if len(text_lower) < 2:
        return True
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


def pick_reply(comment_text):
    text_lower = comment_text.lower()
    for rule in REPLY_RULES:
        if any(kw in text_lower for kw in rule["keywords"]):
            return random.choice(rule["replies"])
    return random.choice(GENERIC_REPLIES)


def get_recent_media_ids(posts, max_age_days=7):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    result = []
    for p in posts:
        if p.get("status") != "posted" or not p.get("media_id"):
            continue
        if p.get("posted_at"):
            try:
                dt = datetime.fromisoformat(p["posted_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except ValueError:
                continue
        result.append((p["media_id"], p.get("account", "default")))
    return result


def fetch_comments(media_id, token):
    try:
        resp = requests.get(
            f"{GRAPH_API}/{media_id}/comments",
            params={
                "fields": "id,text,timestamp,username",
                "access_token": token,
                "limit": 50,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("data", [])
    except Exception as e:
        print(f"[COMMENTS] Erro ao buscar comentarios de {media_id}: {e}")
        return []


def reply_to_comment(comment_id, token, message):
    try:
        resp = requests.post(
            f"{GRAPH_API}/{comment_id}/replies",
            data={"message": message, "access_token": token},
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        print(f"[REPLY] Falha {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[REPLY] Erro: {e}")
        return False


def auto_reply_comments(posts, accounts):
    replied_ids = load_replied()
    media_list = get_recent_media_ids(posts, max_age_days=3)

    if not media_list:
        print("[COMMENTS] Nenhum reel recente para verificar comentarios")
        return 0

    total_replied = 0
    for media_id, account_name in media_list:
        if total_replied >= MAX_REPLIES_PER_CYCLE:
            break
        if account_name not in accounts:
            continue
        token = accounts[account_name]["token"]
        comments = fetch_comments(media_id, token)

        for comment in comments:
            if total_replied >= MAX_REPLIES_PER_CYCLE:
                break
            cid = comment.get("id")
            text = comment.get("text", "")
            username = comment.get("username", "")

            if cid in replied_ids:
                continue
            if should_skip_comment(text):
                replied_ids.add(cid)
                continue

            reply_text = pick_reply(text)
            print(f"[REPLY] @{username}: \"{text[:60]}\" -> \"{reply_text}\"")

            if reply_to_comment(cid, token, reply_text):
                total_replied += 1
                print(f"[REPLY] OK ({total_replied}/{MAX_REPLIES_PER_CYCLE})")
            replied_ids.add(cid)
            time.sleep(REPLY_COOLDOWN_SECONDS)

    save_replied(replied_ids)
    if total_replied:
        print(f"[COMMENTS] {total_replied} comentario(s) respondido(s) neste ciclo")
    else:
        print(f"[COMMENTS] Nenhum comentario novo para responder")
    return total_replied


# ===== MAIN =====

def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        posts = json.load(f)

    accounts = get_accounts()
    if not accounts:
        print("No Instagram accounts configured.")
        return

    # 1. Check token health — skip accounts with invalid tokens
    healthy_accounts = set()
    for name, acct in accounts.items():
        if check_token_health(name, acct["token"], acct["user_id"]):
            healthy_accounts.add(name)

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

        if account_name not in healthy_accounts:
            skipped_count += 1
            log_details.append(f"[SKIP] {post['id']}: token de @{account_name} invalido")
            print(f"[SKIP] {post['id']}: token de @{account_name} invalido, pulando")
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
    analytics = update_analytics(posts, accounts)

    # 5. Detect viral reels
    if analytics:
        check_viral_reels(analytics)
        with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
            json.dump(analytics, f, ensure_ascii=False, indent=2)

    # 6. Auto-reply to comments
    replies_count = auto_reply_comments(posts, accounts)

    # 7. Check for missed/overdue posts
    check_missed_posts(posts, now)

    # 8. Archive old posts
    archive_old_posts(posts, now)

    # 9. Save execution log
    brt_now = now + timedelta(hours=-3)
    log_entry = {
        "timestamp": now.isoformat(),
        "timestamp_brt": brt_now.strftime("%d/%m/%Y %H:%M"),
        "posted": posted_count,
        "failed": failed_count,
        "retried": retried_count,
        "skipped": skipped_count,
        "total_pending": sum(1 for p in posts if p["status"] == "pending"),
        "replies": replies_count,
        "details": log_details,
    }
    save_log(log_entry)

    if posted_count > 0:
        pending = sum(1 for p in posts if p["status"] == "pending")
        send_whatsapp(
            f"✅ InstaAutoPost: {posted_count} post(s) publicado(s)\n"
            f"Falhas: {failed_count} | Pendentes: {pending}\n"
            f"Respostas: {replies_count} comentario(s)"
        )

    print(f"\nDone. {posted_count} posted, {failed_count} failed, "
          f"{retried_count} retrying, "
          f"{sum(1 for p in posts if p['status'] == 'pending')} still pending.")


if __name__ == "__main__":
    main()
