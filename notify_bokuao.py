import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Set, Tuple
from datetime import datetime, timezone, timedelta

LIST_URLS = [
    "https://bokuao.com/blog/list/1/0/?writer=0&page=1",
    "https://bokuao.com/blog/list/1/0/?writer=0&page=2",
    "https://bokuao.com/blog/list/1/0/?writer=0&page=3",
]
STATE_FILE = "state.json"

UA = "Mozilla/5.0 (compatible; BokuaoDiscordNotifier/7.0)"

MAX_IMAGES_PER_POST = 10
MAX_IMAGE_BYTES = 7 * 1024 * 1024

# JPEGのみ（URL拡張子）
ALLOWED_EXT = (".jpg", ".jpeg")

# Discord embed description は 4096 まで（安全側で4000）
EMBED_DESC_LIMIT = 4000


# 対象メンバー → Webhook URL（環境変数から取得）
WEBHOOKS_BY_AUTHOR: Dict[str, str] = {
    "金澤亜美": os.environ["AMI_KANAZAWA"],
    "早﨑すずき": os.environ["SUZUKI_HAYASAKI"],
    "安納蒼衣": os.environ["AOI_ANNO"],
    "塩釜菜那": os.environ["NANA_SHIOGAMA"],
    "萩原心花": os.environ["KOKOKA_HAGIWARA"],
    "工藤唯愛": os.environ["YUA_KUDO"],
    "須永心海": os.environ["MIUNA_SUNAGA"],
    "吉本此那": os.environ["COCONA_YOSHIMOTO"],
    "八重樫美伊咲": os.environ["MIISA_YAEGASHI"],
    "八木仁愛": os.environ["TOA_YAGI"],
    "西森杏弥": os.environ["AYA_NISHIMORI"],
    "宮腰友里亜": os.environ["YURIA_MIYAKOSHI"],
    "青木宙帆": os.environ["YUHO_AOKI"],
    "岩本理瑚": os.environ["RIKO_IWAMOTO"],
    "秋田莉杏": os.environ["RIAN_AKITA"],
    "伊藤ゆず": os.environ["YUZU_ITO"],
    "長谷川稀未": os.environ["HITOMI_HASEGAWA"],
    "柳堀花怜": os.environ["KAREN_YANAGIHORI"],
    "杉浦英恋": os.environ["EREN_SUGIURA"],
    "今井優希": os.environ["YUKI_IMAI"],
}


def target_date_by_jst_window() -> str:
    """
    表示日付ベース：
      JST 06:00-23:59 -> 今日
      JST 00:00-05:59 -> 昨日
    """
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    target = now - timedelta(days=1) if now.hour < 6 else now
    return target.strftime("%Y.%m.%d")


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
    """
    複数の一覧ページから detail URL を取得
    （重複除去・新しい順を維持）
    """
    urls: List[str] = []
    seen: Set[str] = set()

    for list_url in LIST_URLS:
        html = fetch(list_url)
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.select('a[href^="/blog/detail/"], a[href*="/blog/detail/"]'):
            href = a.get("href")
            if not href:
                continue

            abs_url = urljoin(list_url, href)
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

    # 画像：本文領域の全imgを収集（lazy-load対応、プレースホルダー回避、JPEGのみ）
    image_urls: List[str] = []
    for img in container.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy")
            or img.get("src")  # 最後
        )
        if not src:
            continue

        if src.strip().lower().startswith("data:image/"):
            continue

        abs_src = urljoin(post_url, src)
        if not abs_src.startswith(("http://", "https://")):
            continue

        low = abs_src.lower()
        if any(x in low for x in ("transparent", "spacer", "blank", "pixel", "placeholder")):
            continue

        if is_image_url(abs_src):
            image_urls.append(abs_src)

    image_urls = uniq_keep_order(image_urls)

    # テキスト抽出
    text = container.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln]

    # 筆者名（推定）
    author = "（不明）"
    for ln in lines[:120]:
        if 2 <= len(ln) <= 20 and (" " in ln or "　" in ln) and "BLOG" not in ln:
            author = ln
            break

    # 日付より前を削除（本文は日付の次の行から開始）
    lines, date = cut_before_date(lines)

    body = "\n".join(lines)
    body = cut_at_first_marker(body, ["MEMBER CONTENTS"])

    footer_line = f"{author} / {date}"
    if footer_line not in body:
        body = body.rstrip() + "\n\n" + footer_line

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
    """JPEG画像のみをダウンロードして (filename, bytes) を返す（大きすぎるものはスキップ）"""
    out: List[Tuple[str, bytes]] = []

    for i, u in enumerate(urls, start=1):
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()

            content_type = (r.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/jpeg"):
                continue

            data = r.content
            if len(data) > MAX_IMAGE_BYTES:
                continue

            filename = f"image_{i:02d}.jpg"
            out.append((filename, data))

        except Exception:
            continue

    return out


def webhook_post_json(webhook_url: str, payload: Dict) -> None:
    r = requests.post(webhook_url, json=payload, timeout=30)
    r.raise_for_status()


def webhook_post_with_files(webhook_url: str, payload: Dict, files: List[Tuple[str, bytes]]) -> None:
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
    1通目：Embedで本文（URLはembed.url、contentは空）
    2通目：画像だけ添付（最大10枚）
    """
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
        "content": "",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    webhook_post_json(webhook_url, payload1)

    time.sleep(0.9)

    image_urls: List[str] = (post.get("images") or [])[:MAX_IMAGES_PER_POST]
    if not image_urls:
        return

    files = download_images(image_urls)
    if not files:
        return

    payload2 = {
        "content": "",
        "allowed_mentions": {"parse": []},
    }
    webhook_post_with_files(webhook_url, payload2, files)


def main() -> None:
    state = load_state()
    notified_by_author: Dict[str, List[str]] = state.get("notified_by_author", {})

    targets_norm: Dict[str, str] = {norm(k): v for k, v in WEBHOOKS_BY_AUTHOR.items()}

    target_date = target_date_by_jst_window()
    print("Target date (JST window) =", target_date)

    to_send: List[Tuple[str, Dict]] = []  # (author_key, post)

    for url in list_detail_urls():
        post = parse_post(url)

        # 表示日付が target_date の記事だけ
        if post.get("date") != target_date:
            continue

        author_key = norm(post.get("author"))
        if author_key not in targets_norm:
            continue

        notified_set = set(notified_by_author.get(author_key, []))
        if url in notified_set:
            continue

        to_send.append((author_key, post))

    if not to_send:
        print("No new target-author posts for target_date.")
        return

    # 送信（一覧順のまま：新しい順を維持）
    for author_key, post in to_send:
        webhook_url = targets_norm[author_key]

        post_to_discord_embed_then_images(webhook_url, post)

        notified_set = set(notified_by_author.get(author_key, []))
        notified_set.add(post["url"])
        notified_by_author[author_key] = sorted(notified_set)

        print(f"Posted: {post['url']} -> {post['author']}")
        time.sleep(1.0)

    state["notified_by_author"] = notified_by_author
    save_state(state)


if __name__ == "__main__":
    main()
