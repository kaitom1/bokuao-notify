import os
import re
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import List, Dict, Optional, Set

LIST_URL = "https://bokuao.com/blog/list/1/0/"
STATE_FILE = "state.json"

# Discord Webhook URL は GitHub Secrets から渡す
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# ここを対象者にする（空白ゆれ対応は norm() で吸収）
TARGET_AUTHOR = "金澤亜美"

UA = "Mozilla/5.0 (compatible; BokuaoDiscordNotifier/1.0)"


def norm(s: str) -> str:
    """空白（半角/全角）を除去して比較できるように正規化"""
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
    """一覧ページから detail URL を収集（重複除去、出現順を維持）"""
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

    # 一覧の表示順（通常は新しい順）で返す
    return urls


def parse_post(post_url: str) -> Dict:
    """
    記事ページから情報を抽出:
    - author / date / title / excerpt / image
    ノイズ（noscript等）を消して抽出。
    """
    html = fetch(post_url)
    soup = BeautifulSoup(html, "html.parser")

    # ノイズ除去（重要：noscript が「JavaScript無効」文言の原因）
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(["header", "footer", "nav"]):
        tag.decompose()

    container = soup.find("main") or soup.find("article") or soup.body
    if container is None:
        container = soup

    # 画像（1枚だけ）
    img_url: Optional[str] = None
    for img in container.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        abs_src = urljoin(post_url, src)
        if "bokuao.com" in abs_src:
            img_url = abs_src
            break

    # テキスト
    text = container.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln]

    # 日付（例: 2026.01.05）
    date = None
    m = re.search(r"\b20\d{2}\.\d{2}\.\d{2}\b", text)
    if m:
        date = m.group(0)

    # タイトル（titleタグ優先）
    title = None
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)

    # 筆者：ページ上部近辺の短い行から「氏名っぽい」ものを推定
    # ただし、最終的な判定は norm(author) == norm(TARGET_AUTHOR) で行う。
    author = None
    for ln in lines[:120]:
        # 例: "金澤 亜美" のような表記を想定
        # 短めで、空白が含まれる日本語氏名を拾う
        if 2 <= len(ln) <= 20 and (" " in ln or "　" in ln) and "BLOG" not in ln:
            author = ln
            break

    # 抜粋（最大400文字）
    body = "\n".join(lines)

    return {
    "url": post_url,
    "author": author or "（不明）",
    "date": date or "（不明）",
    "title": title or "（タイトル不明）",
    "body": body,
    "image": img_url,
}

　　 # 本文の終了（共通UI）でカットする：本文は「またね」までにしたい
END_MARKERS = ["MEMBER CONTENTS"]

def cut_at_first_marker(text: str, markers):
    idxs = [text.find(m) for m in markers if text.find(m) != -1]
    if not idxs:
        return text
    return text[:min(idxs)].rstrip()

body = cut_at_first_marker(body, END_MARKERS)


def post_to_discord(post: Dict) -> None:
    """
    Discord Webhook へ送信（Embed使用）
    """
    # タイトル欄は Discord で見やすいように整形
    # titleはページtitleが長いことがあるので、必要なら短縮
    embed_title = post["title"]
    if len(embed_title) > 120:
        embed_title = embed_title[:117] + "…"

    embed = {
    "title": embed_title,
    "url": post["url"],
    "description": post["body"][:4000],  # 本文をembedに入れる場合（長いなら分割送信方式推奨）
    "footer": {"text": f"{post['author']} / {post['date']}"},
}

    if post.get("image"):
        embed["image"] = {"url": post["image"]}

    payload = {
        "content": post["url"],  # URLも明示（Discordの自動展開も期待できる）
        "embeds": [embed],
        "allowed_mentions": {"parse": []},  # @everyone 等の誤爆防止
    }

    r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()


def main() -> None:
    state = load_state()
    notified: Set[str] = set(state.get("notified_urls", []))

    # 一覧から候補を取り、未通知のものだけ新しい順で処理
    candidates = list_detail_urls()

    # 新しい順の一覧を想定して、未通知を上から探す
    # 「金澤亜美」の最初の未通知記事が見つかったらそれを通知して終了（1日1回想定）
    for url in candidates:
        if url in notified:
            continue

        post = parse_post(url)

        # 筆者判定（空白ゆれを吸収）
        if norm(post["author"]) != norm(TARGET_AUTHOR):
            # 対象外。必要ならここでスキップだけして、次候補へ。
            # （対象者以外のURLは state に記録しない＝将来対象者の記事を探すため）
            continue

        # 対象者の記事だったら通知
        post_to_discord(post)

        # 通知済みとして保存
        notified.add(url)
        state["notified_urls"] = sorted(notified)  # 見やすさ重視でソート
        save_state(state)

        print("Posted:", url)
        return

    print("No new target-author posts.")


if __name__ == "__main__":
    main()
if __name__ == "__main__":
    main()
