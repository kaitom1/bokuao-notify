import os
import re
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import List, Dict, Optional, Set

LIST_URL = "https://bokuao.com/blog/list/1/0/"
STATE_FILE = "state.json"

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TARGET_AUTHOR = "金澤亜美"

UA = "Mozilla/5.0 (compatible; BokuaoDiscordNotifier/2.0)"


def norm(s: str) -> str:
    """空白（半角/全角）を除去して比較用に正規化"""
    return re.sub(r"[ \u3000]+", "", (s or "").strip())


def fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def list_detail_urls() -> List[str]:
    """一覧ページから detail URL を取得（重複除去・順序維持）"""
    html = fetch(LIST_URL)
    soup = BeautifulSoup(html, "html.parser")

    urls: List[str] = []
    seen: Set[str] = set()

    for a in soup.select('a[href^="/blog/detail/"], a[href*="/blog/detail/"]'):
        href = a.get("href")
        if not href:
            continue
        abs_url = urljoin(LIST_URL, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        urls.append(abs_url)

    return urls


def cut_at_first_marker(text: str, markers: List[str]) -> str:
    """最初に出現した marker 以降を削除"""
    idxs = [text.find(m) for m in markers if text.find(m) != -1]
    if not idxs:
        return text.rstrip()
    return text[:min(idxs)].rstrip()


def parse_post(post_url: str) -> Dict:
    html = fetch(post_url)
    soup = BeautifulSoup(html, "html.parser")

    # ノイズ除去
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(["header", "footer", "nav"]):
        tag.decompose()

    container = soup.find("main") or soup.find("article") or soup.body
    if container is None:
        container = soup

    # 画像（最初の1枚 / lazy-load対応）
    img_url: Optional[str] = None
    for img in container.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy")
        )
        if not src:
            continue

        abs_src = urljoin(post_url, src)
        if abs_src.startswith("http://") or abs_src.startswith("https://"):
            img_url = abs_src
            break

    # テキスト抽出
    text = container.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln]

    # 日付
    date = None
    m = re.search(r"\b20\d{2}\.\d{2}\.\d{2}\b", text)
    if m:
        date = m.group(0)

    # タイトル
    title = None
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)

    # 筆者名
    author = None
    for ln in lines[:120]:
        if 2 <= len(ln) <= 20 and (" " in ln or "　" in ln) and "BLOG" not in ln:
            author = ln
            break

    body = "\n".join(lines)

    # 本文終端でカット
    body = cut_at_first_marker(body, ["MEMBER CONTENTS"])

    # 本文末尾にフッター（B方式）
    footer_line = f"{author or '（不明）'} / {date or '（不明）'}"
    if footer_line not in body:
        body = body.rstrip() + "\n\n" + footer_line

    return {
        "url": post_url,
        "author": author or "（不明）",
        "date": date or "（不明）",
        "title": title or "（タイトル不明）",
        "body": body,
        "image": img_url,
    }


def post_to_discord(post: Dict) -> None:
    embed_title = post["title"]
    if len(embed_title) > 120:
        embed_title = embed_title[:117] + "…"

    embed = {
        "title": embed_title,
        "url": post["url"],
        "description": post["body"][:4000],  # Embed上限対策
    }

    if post.get("image"):
        embed["image"] = {"url": post["image"]}

    payload = {
        "content": post["url"],
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()


def main() -> None:
    state = load_state()
    notified: Set[str] = set(state.get("notified_urls", []))

    for url in list_detail_urls():
        if url in notified:
            continue

        post = parse_post(url)

        if norm(post["author"]) != norm(TARGET_AUTHOR):
            continue

        post_to_discord(post)

        notified.add(url)
        state["notified_urls"] = sorted(notified)
        save_state(state)

        print("Posted:", url)
        return

    print("No new target-author posts.")


if __name__ == "__main__":
    main()
