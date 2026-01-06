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

UA = "Mozilla/5.0 (compatible; BokuaoNewsDiscordNotifier/2.1)"

# Discord limits
EMBED_TITLE_LIMIT = 256
EMBED_DESC_LIMIT = 4000  # 4096未満の安全側

MAX_IMAGES_PER_POST = 10
MAX_IMAGE_BYTES = 7 * 1024 * 1024

# JPEGのみ
ALLOWED_EXT = (".jpg", ".jpeg")

NEWS_WEBHOOK_URL = os.environ["BOKUAO_NEWS"]
NEWS_DATE_RE = re.compile(r"\b20\d{2}\.\d{2}\.\d{2}\b")

# ニュースカテゴリ表記（必要なら追加）
NEWS_CATEGORIES = {"OTHER", "NEWS", "EVENT", "MEDIA"}


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


def truncate(s: str, limit: int) -> str:
    if s is None:
        return ""
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
    return text[: min(idxs)].rstrip()


def download_images(urls: List[str]) -> List[Tuple[str, bytes]]:
    """
    JPEGのみダウンロードして (filename, bytes) を返す（大きすぎるものはスキップ）
    """
    out: List[Tuple[str, bytes]] = []
    for i, u in enumerate(urls, start=1):
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()

            # Content-Type が jpeg でないものは落とす（拡張子偽装対策）
            ctype = (r.headers.get("Content-Type") or "").lower()
            if not ctype.startswith("image/jpeg"):
                continue

            data = r.content
            if len(data) > MAX_IMAGE_BYTES:
                continue

            filename = f"news_image_{i:02d}.jpg"
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

        items.append(
            {
                "date": date,
                "category": category,
                "title": title or "（タイトル不明）",
                "url": abs_url,
            }
        )

    # URL重複除去（順序維持）
    seen = set()
    out = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def strip_leading_header_lines(desc: str, title: str, category: str, date: str) -> str:
    """
    news詳細ページ本文先頭に混入する
    「タイトル(複数回)/日付/カテゴリ」などの見出しブロックを、先頭にある限り剥がす。
    """
    def norm(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"[ \u3000]+", "", s)
        return s

    t = norm(title)
    c = norm(category)
    d = norm(date)

    lines = (desc or "").split("\n")

    while lines:
        head_raw = lines[0].strip()
        head = norm(head_raw)

        if not head:
            lines.pop(0)
            continue

        # タイトル/カテゴリ/日付が先頭にある限り除去
        if head == t or head == c or head == d:
            lines.pop(0)
            continue

        # 日付単独行
        if NEWS_DATE_RE.fullmatch(head_raw):
            lines.pop(0)
            continue

        # カテゴリ行（OTHER等）
        if head_raw in NEWS_CATEGORIES:
            lines.pop(0)
            continue

        # ありがちな見出しノイズ
        if head in ("NEWS", "SHARE", "BACK", "SUPPORT"):
            lines.pop(0)
            continue

        break

    # 先頭空行除去
    while lines and not lines[0].strip():
        lines.pop(0)

    return "\n".join(lines).strip()


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

    # 画像（JPEGのみ）
    image_urls: List[str] = []
    for img in container.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy")
            or img.get("src")
        )
        if not src:
            continue

        if src.strip().lower().startswith("data:image/"):
            continue

        abs_src = urljoin(detail_url, src)
        if abs_src.startswith(("http://", "https://")) and is_image_url(abs_src):
            image_urls.append(abs_src)
    image_urls = uniq_keep_order(image_urls)

    # テキスト
    text = container.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln]

    # ページ内の日付（保険）
    date = "（不明）"
    for ln in lines[:80]:
        m = NEWS_DATE_RE.search(ln)
        if m:
            date = m.group(0)
            break

    body = "\n".join(lines)
    body = cut_at_first_marker(body, ["SHARE", "BACK", "SUPPORT"])
    body_clean = "\n".join([ln for ln in body.split("\n") if ln]).strip()

    return {
        "url": detail_url,
        "date": date,
        "body": body_clean,
        "images": image_urls,
    }


def post_news_item_embed_then_images(item: Dict) -> None:
    """
    1通目：Embed（title/url/description）
    2通目：画像だけ添付（最大10枚）
    """
    detail = parse_news_detail(item["url"])

    title = item.get("title") or "（タイトル不明）"
    category = item.get("category") or "（不明）"
    date = item.get("date") or detail.get("date") or "（不明）"

    embed_title = truncate(title, EMBED_TITLE_LIMIT)

    # 本文作成 → 先頭見出しブロック削除 → 末尾にカテゴリ/日付を太字で追記
    embed_desc = truncate((detail.get("body") or "").strip(), EMBED_DESC_LIMIT)
    embed_desc = strip_leading_header_lines(embed_desc, title, category, date)
    embed_desc = embed_desc.rstrip() + f"\n\n{category} / {date}"

    embed = {
        "title": embed_title,
        "url": item["url"],
        "description": embed_desc,
    }

    payload1 = {
        "content": "",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    webhook_post_json(payload1)

    time.sleep(0.9)

    imgs = (detail.get("images") or [])[:MAX_IMAGES_PER_POST]
    if not imgs:
        return

    files = download_images(imgs)
    if not files:
        return

    payload2 = {
        "content": "",
        "allowed_mentions": {"parse": []},
    }
    webhook_post_with_files(payload2, files)


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

        post_news_item_embed_then_images(it)
        notified.add(it["url"])
        posted += 1

        time.sleep(1.0)

    state["notified_news_urls"] = sorted(notified)
    save_state(state)
    print(f"[NEWS] Done. today={today} posted={posted} skipped(already)={skipped}")


if __name__ == "__main__":
    main()
