import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Optional, Set, Tuple

LIST_URL = "https://bokuao.com/blog/list/1/0/"
STATE_FILE = "state.json"

UA = "Mozilla/5.0 (compatible; BokuaoDiscordNotifier/4.0)"

# Discordの添付は1メッセージ最大10ファイルが無難
MAX_IMAGES_PER_POST = 10

# 画像ダウンロードのサイズ上限（バイト）
# Discordの上限はサーバ/環境で変わるので、保守的に 7MB 程度にしておく
MAX_IMAGE_BYTES = 7 * 1024 * 1024

# 画像URLフィルタ（拡張子ベース。必要なら緩めてください）
ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# 対象メンバー → Webhook URL（環境変数から取得）
# ※ ここでキー（表示名）は「空白なし」の表記で統一しておくのが安全
WEBHOOKS_BY_AUTHOR: Dict[str, str] = {
    "金澤亜美": os.environ["AMI_KANAZAWA"],
    "早崎すずき": os.environ["SUZUKI_HAYASAKI"],
}


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


def is_image_url(url: str) -> bool:
    p = urlparse(url)
    path = (p.path or "").lower()
    return path.endswith(ALLOWED_EXT)


def uniq_keep_order(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


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

    # 画像：本文領域の全imgを収集（lazy-load対応）
    image_urls: List[str] = []
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
        if not abs_src.startswith(("http://", "https://")):
            continue

        # 拡張子でフィルタ（必要なら is_image_url を外す/緩める）
        if is_image_url(abs_src):
            image_urls.append(abs_src)

    image_urls = uniq_keep_order(image_urls)

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

    # 筆者名（推定）
    author = None
    for ln in lines[:120]:
        if 2 <= len(ln) <= 20 and (" " in ln or "　" in ln) and "BLOG" not in ln:
            author = ln
            break

    body = "\n".join(lines)

    # 本文終端でカット（「またね」までにしたい）
    body = cut_at_first_marker(body, ["MEMBER CONTENTS"])

    # 本文末尾にフッター（希望：B）
    footer_line = f"{author or '（不明）'} / {date or '（不明）'}"
    if footer_line not in body:
        body = body.rstrip() + "\n\n" + footer_line

    return {
        "url": post_url,
        "author": author or "（不明）",
        "date": date or "（不明）",
        "title": title or "（タイトル不明）",
        "body": body,
        "images": image_urls,  # ← 全画像
    }


def download_images(urls: List[str]) -> List[Tuple[str, bytes]]:
    """画像URLをダウンロードして (filename, bytes) の配列を返す（大きすぎるものはスキップ）"""
    out: List[Tuple[str, bytes]] = []

    for i, u in enumerate(urls, start=1):
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()

            data = r.content
            if len(data) > MAX_IMAGE_BYTES:
                continue

            path = urlparse(u).path
            ext = os.path.splitext(path)[1].lower() or ".jpg"
            if ext not in ALLOWED_EXT:
                ext = ".jpg"

            filename = f"image_{i:02d}{ext}"
            out.append((filename, data))
        except Exception:
            continue

    return out


def webhook_post_json(webhook_url: str, payload: Dict) -> None:
    r = requests.post(webhook_url, json=payload, timeout=30)
    r.raise_for_status()


def webhook_post_with_files(webhook_url: str, payload: Dict, files: List[Tuple[str, bytes]]) -> None:
    """Discord webhook に添付ファイル付きで送る（Embed外で画像が確実に表示される）"""
    multipart = {}
    for idx, (fname, data) in enumerate(files):
        multipart[f"files[{idx}]"] = (fname, data)

    r = requests.post(
        webhook_url,
        data={"payload_json": json.dumps(payload, ensure_ascii=False)},
        files=multipart,
        timeout=60,
    )
    r.raise_for_status()


def post_to_discord(webhook_url: str, post: Dict) -> None:
    """
    1通目：本文（Embed）
    2通目：画像だけ（Embed外）を添付でまとめて送る
    """
    embed_title = post["title"]
    if len(embed_title) > 120:
        embed_title = embed_title[:117] + "…"

    embed = {
        "title": embed_title,
        "url": post["url"],
        "description": post["body"][:4000],  # Embed description上限対策
    }

    payload1 = {
        "content": post["url"],
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    webhook_post_json(webhook_url, payload1)

    time.sleep(0.8)

    # 2通目：画像（添付）
    image_urls: List[str] = post.get("images", [])
    if not image_urls:
        return

    image_urls = image_urls[:MAX_IMAGES_PER_POST]

    files = download_images(image_urls)
    if not files:
        return

    payload2 = {
        "content": "",  # 画像だけ表示したいので空
        "allowed_mentions": {"parse": []},
    }
    webhook_post_with_files(webhook_url, payload2, files)


def main() -> None:
    # stateは「作者ごと」に通知済みURLを保持（運用で分かりやすい）
    state = load_state()
    notified_by_author: Dict[str, List[str]] = state.get("notified_by_author", {})

    # 比較用に正規化した author->webhook を作る
    targets_norm = {norm(k): v for k, v in WEBHOOKS_BY_AUTHOR.items()}

    for url in list_detail_urls():
        post = parse_post(url)

        author_key = norm(post["author"])
        if author_key not in targets_norm:
            continue

        webhook_url = targets_norm[author_key]

        # 作者ごとの通知済みを確認
        notified_list = set(notified_by_author.get(author_key, []))
        if url in notified_list:
            continue

        # 送信
        post_to_discord(webhook_url, post)

        # 保存
        notified_list.add(url)
        notified_by_author[author_key] = sorted(notified_list)
        state["notified_by_author"] = notified_by_author
        save_state(state)

        print(f"Posted: {url} -> {post['author']}")
        return

    print("No new target-author posts.")


if __name__ == "__main__":
    main()
