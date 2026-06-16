import os
import json
import base64
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests

BRT = timezone(timedelta(hours=-3))
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
API_BASE = f"https://api.github.com/repos/{GITHUB_REPO}/contents"


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
            print("[WHATSAPP] Resumo semanal enviado")
            return True
        print(f"[WHATSAPP] Falha: {resp.status_code}")
        return False
    except Exception as e:
        print(f"[WHATSAPP] Erro: {e}")
        return False


def gh_fetch(path):
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    resp = requests.get(f"{API_BASE}/{path}", headers=headers, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        return json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    return None


def build_summary():
    now = datetime.now(BRT)
    week_ago = now - timedelta(days=7)
    week_start = week_ago.strftime("%d/%m")
    week_end = now.strftime("%d/%m")

    posts = gh_fetch("data/posts.json") or []
    analytics = gh_fetch("data/analytics.json") or {}

    posted_this_week = []
    for p in posts:
        if p.get("status") != "posted" or not p.get("posted_at"):
            continue
        try:
            posted_dt = datetime.fromisoformat(p["posted_at"].replace("Z", "+00:00")).astimezone(BRT)
            if posted_dt >= week_ago:
                posted_this_week.append(p)
        except (ValueError, KeyError):
            continue

    total_posted = len(posted_this_week)

    pending = [p for p in posts if p.get("status") == "pending"]
    failed = [p for p in posts if p.get("status") == "failed"]

    total_plays = 0
    total_likes = 0
    total_reach = 0
    total_comments = 0
    total_shares = 0
    total_saved = 0
    best_reel = None
    best_plays = 0

    for p in posted_this_week:
        pid = p.get("id", "")
        a = analytics.get(pid, {})
        plays = a.get("plays", 0)
        total_plays += plays
        total_likes += a.get("likes", 0)
        total_reach += a.get("reach", 0)
        total_comments += a.get("comments", 0)
        total_shares += a.get("shares", 0)
        total_saved += a.get("saved", 0)
        if plays > best_plays:
            best_plays = plays
            caption = (p.get("caption") or a.get("caption") or "Sem legenda")[:50]
            best_reel = {"caption": caption, "plays": plays, "likes": a.get("likes", 0)}

    def fmt(n):
        if n >= 1000:
            return f"{n/1000:.1f}k"
        return str(n)

    lines = [
        f"📊 *RESUMO SEMANAL*",
        f"_{week_start} a {week_end}_",
        "",
        f"📹 Reels postados: *{total_posted}*",
        f"⏳ Na fila: *{len(pending)}*",
    ]

    if failed:
        lines.append(f"❌ Falharam: *{len(failed)}*")

    if total_posted > 0:
        lines.extend([
            "",
            "📈 *METRICAS DA SEMANA*",
            f"▶️ Plays: *{fmt(total_plays)}*",
            f"❤️ Curtidas: *{fmt(total_likes)}*",
            f"💬 Comentarios: *{fmt(total_comments)}*",
            f"🔄 Compartilhamentos: *{fmt(total_shares)}*",
            f"👁️ Alcance: *{fmt(total_reach)}*",
            f"📌 Salvos: *{fmt(total_saved)}*",
        ])

        if best_reel:
            lines.extend([
                "",
                "🏆 *MELHOR REEL*",
                f'"{best_reel["caption"]}"',
                f"▶️ {fmt(best_reel['plays'])} plays | ❤️ {fmt(best_reel['likes'])} curtidas",
            ])
    else:
        lines.extend([
            "",
            "Nenhum reel postado essa semana.",
        ])

    return "\n".join(lines)


if __name__ == "__main__":
    print("=== Resumo Semanal ===")
    summary = build_summary()
    print(summary)
    print()
    send_whatsapp(summary)
