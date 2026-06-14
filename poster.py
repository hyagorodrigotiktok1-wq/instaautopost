import os
import sys
import json
import time
import requests
from datetime import datetime, timezone


GRAPH_API = "https://graph.instagram.com/v21.0"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "posts.json")


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


def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        posts = json.load(f)

    accounts = get_accounts()
    if not accounts:
        print("No Instagram accounts configured. Set INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_USER_ID as secrets.")
        return

    now = datetime.now(timezone.utc)
    modified = False
    posted_count = 0

    for post in posts:
        if post.get("status") != "pending":
            continue

        scheduled = datetime.fromisoformat(post["scheduled_at"])
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        if scheduled > now:
            continue

        account_name = post.get("account", "default")
        if account_name not in accounts:
            post["status"] = "failed"
            post["error"] = f"Account '{account_name}' not found in secrets"
            modified = True
            print(f"[SKIP] {post['id']}: account '{account_name}' not configured")
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
            print(f"[OK]   {post['id']} -> media {media_id}")

        except Exception as e:
            post["status"] = "failed"
            post["error"] = str(e)
            print(f"[FAIL] {post['id']}: {e}")

        modified = True

    if modified:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {posted_count} posted, {sum(1 for p in posts if p['status']=='pending')} still pending.")


if __name__ == "__main__":
    main()
