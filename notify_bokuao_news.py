import os
import re
import json
import time
import datetime as dt
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Set, Tuple

NEWS_LIST_URL = "https://bokuao.com/news/1/"
STATE_FILE = "state_news.json"

UA = "Mozilla/5.0 (compatible; BokuaoNewsDiscordNotifier/1.0)"

# Discordのcontent上限
DISCORD_CONTENT_LIMIT = 2000

# 1メッセージの添付は最大10枚が無難
MAX_IMAGES_PER_POST = 10

# 画像のサイズ上限（バイト）
MAX_IMAGE_BYTES = 7 * 1024 * 1024

# 画像URLフィルタ（拡張子ベース）
ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# NEWS用Webhook（newsチャンネル）
NEWS_WEBHOOK_URL = os.environ["BOKUAO_NEWS"]

NEWS_DATE_RE = re.compile(r"\b20\d{2}\.\d{2}\.\d{2}\b")


def jst_today_yyyymmdd() -> str:
    jst = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime.now(tz=jst)
    return now.strftime("%Y.%m.%d")


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


def truncate_for_discord(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


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


def cut_at_first_marker(text: str, markers: List[str]) -> str:
    idxs = [text.find(m) for m in markers if text.find(m) != -1]
    if not idxs:
        return text.rstrip()
    return text[:min(idxs)].rstrip()


def download_images(urls: List[str]) -> List[Tuple[str, bytes]]:
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

            filename = f"news_image_{i:02d}{ext}"
            out.append((filename, data))
        except Exception:
            continue
    return out


def webhook_post_json(payload: Dict) -> None:
    r = requests.post(NEWS_WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()


def webhook_post_with_files(payload: Dict, files: List[Tuple[str, bytes]]) -> None:
    multipart = {}
    for idx, (fname, data) in enumerate(files):
        multipart[f"files[{idx}]"] = (fname, data)

    r = requests.post(
        NEWS_WEBHOOK_URL,
        data={"payload_json": json.dumps(payload, ensure_ascii=False)},
        files=multipart,
        timeout=60,
    )
    r.raise_for_status()


def list_news_items_from_listpage() -> List[Dict]:
    """
    /news/1/ から {date, category, title, url} を抽出
    """
    html = fetch(NEWS_LIST_URL)
    soup = BeautifulSoup(html, "html.parser")

    items: List[Dict] = []
    for a in soup.select('a[href^="/news/detail/"], a[href*="/news/detail/"]'):
        href = a.get("href")
        if not href:
            continue
        abs_url = urljoin(NEWS_LIST_URL, href)

        txt = a.get_text(" ", strip=True)
        m = NEWS_DATE_RE.search(txt)
        if not m:
            continue

        date = m.group(0)
        rest = txt.replace(date, "").strip()
        parts = rest.split()

        category = parts[0] if parts else "（不明）"
        title = rest[len(category):].strip() if len(parts) >= 2 else rest

        items.append({
            "date": date,
            "category": category,
            "title": title or "（タイトル不明）",
            "url": abs_url,
        })

    # URL重複除去（順序維持）
    seen = set()
    out = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def parse_news_detail(detail_url: str) -> Dict:
    html = fetch(detail_url)
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(["header", "footer", "nav"]):
        tag.decompose()

    container = soup.find("main") or soup.find("article") or soup.body
    if container is None:
        container = soup

    # 画像
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
        abs_src = urljoin(detail_url, src)
        if abs_src.startswith(("http://", "https://")) and is_image_url(abs_src):
            image_urls.append(abs_src)
    image_urls = uniq_keep_order(image_urls)

    # テキスト
    text = container.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln]

    # 日付（ページ内）
    date = "（不明）"
    for ln in lines[:80]:
        m = NEWS_DATE_RE.search(ln)
        if m:
            date = m.group(0)
            break

    # ノイズっぽい末尾を削る（ページにより変動するので軽め）
    body = "\n".join(lines)
    body = cut_at_first_marker(body, ["SHARE", "BACK", "SUPPORT"])

    # 冒頭にカテゴリ/日付/タイトル等が混ざる場合があるので簡単に整形
    body_lines = [ln for ln in body.split("\n") if ln and ln not in ("NEWS", "SHARE", "BACK", "SUPPORT")]
    body_clean = "\n".join(body_lines).strip()

    return {
        "url": detail_url,
        "date": date,
        "body": body_clean,
        "images": image_urls,
    }


def post_news_item(item: Dict) -> None:
    detail = parse_news_detail(item["url"])

    title = item.get("title") or "（タイトル不明）"
    category = item.get("category") or "（不明）"
    date = item.get("date") or detail.get("date") or "（不明）"

    header = f"\n\n\n"
    url_text = f"<{item['url']}>"

    body = (detail.get("body") or "").strip()
    content = header + body + "\n\n" + url_text
    content = truncate_for_discord(content, DISCORD_CONTENT_LIMIT)

    payload = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }

    imgs = (detail.get("images") or [])[:MAX_IMAGES_PER_POST]
    files = download_images(imgs) if imgs else []

    if files:
        webhook_post_with_files(payload, files)
    else:
        webhook_post_json(payload)


def main() -> None:
    state = load_state()
    notified: Set[str] = set(state.get("notified_news_urls", []))

    today = jst_today_yyyymmdd()

    items = list_news_items_from_listpage()
    todays = [it for it in items if it.get("date") == today]

    if not todays:
        print(f"[NEWS] No items for today: {today}")
        return

    posted = 0
    skipped = 0

    for it in todays:
        if it["url"] in notified:
            skipped += 1
            continue

        post_news_item(it)
        notified.add(it["url"])
        posted += 1

        # レート制限回避
        time.sleep(1.0)

    state["notified_news_urls"] = sorted(notified)
    save_state(state)
    print(f"[NEWS] Done. today={today} posted={posted} skipped(already)={skipped}")


if __name__ == "__main__":
    main()
