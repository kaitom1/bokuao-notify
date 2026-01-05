import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Set, Tuple

LIST_URL = "https://bokuao.com/blog/list/1/0/"
STATE_FILE = "state.json"

UA = "Mozilla/5.0 (compatible; BokuaoDiscordNotifier/6.1)"

# 1メッセージの添付は最大10枚が無難
MAX_IMAGES_PER_POST = 10

# 画像のサイズ上限（バイト）
MAX_IMAGE_BYTES = 7 * 1024 * 1024

# 画像URLフィルタ（拡張子ベース）
ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# Discord embed description は 4096 まで（安全側で4000）
EMBED_DESC_LIMIT = 4000

# 対象メンバー → Webhook URL（環境変数から取得）
WEBHOOKS_BY_AUTHOR: Dict[str, str] = {
    "金澤亜美": os.environ["AMI_KANAZAWA"],
    "早﨑すずき": os.environ["SUZUKI_HAYASAKI"],
    "安納蒼衣": os.environ["AOI_ANNO"],
}


def norm(s: str) -> str:
    """空白除去 + 代表的な異体字ゆれ（﨑→崎）を正規化"""
    s = (s or "").strip()
    s = re.sub(r"[ \u3000]+", "", s)
    s = s.replace("﨑", "崎")
    return s


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


def cut_before_date(lines: List[str]) -> Tuple[List[str], str]:
    """
    lines の中から最初の日付行(YYYY.MM.DD)を探し、
    それ以前を削除して本文linesを返す。
    戻り値: (本文lines, 日付文字列)
    """
    for i, ln in enumerate(lines):
        m = re.search(r"\b20\d{2}\.\d{2}\.\d{2}\b", ln)
        if m:
            return lines[i + 1 :], m.group(0)
    return lines, "（不明）"


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
        if is_image_url(abs_src):
            image_urls.append(abs_src)

    image_urls = uniq_keep_order(image_urls)

    # テキスト抽出
    text = container.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln]

    # 筆者名（推定）— 日付カット前のlinesから拾う
    author = "（不明）"
    for ln in lines[:120]:
        if 2 <= len(ln) <= 20 and (" " in ln or "　" in ln) and "BLOG" not in ln:
            author = ln
            break

    # 日付より前を削除（本文は日付の次の行から開始）
    lines, date = cut_before_date(lines)

    body = "\n".join(lines)

    # 本文終端でカット（「MEMBER CONTENTS」以降を除外）
    body = cut_at_first_marker(body, ["MEMBER CONTENTS"])

    # 本文末尾にフッター
    footer_line = f"{author} / {date}"
    if footer_line not in body:
        body = body.rstrip() + "\n\n" + footer_line

    # タイトル（ページtitleは長くなりやすいが、とりあえず維持）
    title = "（タイトル不明）"
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)

    return {
        "url": post_url,
        "author": author,
        "date": date,
        "title": title,
        "body": body,
        "images": image_urls,
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
    """
    Discord webhook に添付ファイル付きで送る。
    payload は payload_json として渡す。
    """
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


def post_to_discord_embed_then_images(webhook_url: str, post: Dict) -> None:
    """
    1通目：Embedで本文
    2通目：画像だけを添付でまとめて送る（Embed外）
    """
    # --- 1通目：本文（Embed） ---
    embed_title = post.get("title") or "（タイトル不明）"
    if len(embed_title) > 256:
        embed_title = embed_title[:253] + "…"

    desc = (post.get("body") or "").strip()
    if len(desc) > EMBED_DESC_LIMIT:
        desc = desc[: EMBED_DESC_LIMIT - 1] + "…"

    embed = {
        "title": embed_title,
        "url": post["url"],
        "description": desc,
    }

    payload1 = {
        "content": "",  # クリック可能に。プレビュー暴れ防止で <> に
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    webhook_post_json(webhook_url, payload1)

    # レート制限回避
    time.sleep(0.9)

    # --- 2通目：画像（添付） ---
    image_urls: List[str] = (post.get("images") or [])[:MAX_IMAGES_PER_POST]
    if not image_urls:
        return

    files = download_images(image_urls)
    if not files:
        return

    payload2 = {
        "content": "",  # 画像だけ送る
        "allowed_mentions": {"parse": []},
    }

    webhook_post_with_files(webhook_url, payload2, files)


def main() -> None:
    state = load_state()
    notified_by_author: Dict[str, List[str]] = state.get("notified_by_author", {})

    # 対象作者（正規化） -> webhook_url
    targets_norm: Dict[str, str] = {norm(k): v for k, v in WEBHOOKS_BY_AUTHOR.items()}

    # このrunで「作者ごとに1件だけ」送る
    pending: Dict[str, Dict] = {}

    for url in list_detail_urls():
        post = parse_post(url)
        author_key = norm(post["author"])

        if author_key not in targets_norm:
            continue
        if author_key in pending:
            continue

        notified_list = set(notified_by_author.get(author_key, []))
        if url in notified_list:
            continue

        pending[author_key] = post

        if len(pending) == len(targets_norm):
            break

    if not pending:
        print("No new target-author posts.")
        return

    for author_key, post in pending.items():
        webhook_url = targets_norm[author_key]

        post_to_discord_embed_then_images(webhook_url, post)

        notified_list = set(notified_by_author.get(author_key, []))
        notified_list.add(post["url"])
        notified_by_author[author_key] = sorted(notified_list)

        print(f"Posted: {post['url']} -> {post['author']}")
        time.sleep(1.0)

    state["notified_by_author"] = notified_by_author
    save_state(state)


if __name__ == "__main__":
    main()
