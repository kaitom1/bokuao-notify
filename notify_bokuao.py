import os
import re
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

LIST_URL = "https://bokuao.com/blog/list/1/0/"
STATE_FILE = "state.json"

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
UA = "Mozilla/5.0 (compatible; BlogNotifier/1.0)"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text

def get_latest_post_url():
    html = fetch(LIST_URL)
    soup = BeautifulSoup(html, "html.parser")

    # 一覧ページ内の /blog/detail/xxxxx を先頭から探す（最新が先に出る想定）
    a = soup.select_one('a[href^="/blog/detail/"], a[href*="/blog/detail/"]')
    if not a:
        raise RuntimeError("最新記事リンクが見つかりませんでした（HTML構造変更の可能性）。")
    return urljoin(LIST_URL, a.get("href"))

def parse_post(post_url):
    html = fetch(post_url)
    soup = BeautifulSoup(html, "html.parser")

    # 画像（1枚だけ）
    img_url = None
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        abs_src = urljoin(post_url, src)
        if "bokuao.com" in abs_src:
            img_url = abs_src
            break

    # テキスト抽出（抜粋用）
    text = soup.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln]

    # 日付っぽい表記（例: 2026.01.04）
    date = None
    m = re.search(r"\b20\d{2}\.\d{2}\.\d{2}\b", text)
    if m:
        date = m.group(0)

    # 筆者名の簡易推定（過検出しうるので、必要なら後で精密化）
    author = None
    for ln in lines[:80]:
        if 2 <= len(ln) <= 20 and (" " in ln or "　" in ln) and "BLOG" not in ln:
            author = ln
            break

    # 抜粋（最大400文字）
    body = "\n".join(lines)
    excerpt = body[:400] + ("…" if len(body) > 400 else "")

    return {
        "url": post_url,
        "author": author or "（不明）",
        "date": date or "（不明）",
        "excerpt": excerpt,
        "image": img_url,
    }

def post_to_discord(post):
    embed = {
        "title": f"{post['author']} / {post['date']}",
        "url": post["url"],
        "description": post["excerpt"],
    }
    if post["image"]:
        embed["image"] = {"url": post["image"]}

    payload = {
        "content": post["url"],
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()

def main():
    state = load_state()
    last_url = state.get("last_url")

    latest_url = get_latest_post_url()
    if latest_url == last_url:
        print("No update.")
        return

    post = parse_post(latest_url)
    post_to_discord(post)

    state["last_url"] = latest_url
    save_state(state)
    print("Posted:", latest_url)

if __name__ == "__main__":
    main()