import os
import re
import json
import time
import datetime as dt
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Set, Tuple, Optional

NEWS_LIST_URL = "https://bokuao.com/news/1/"
STATE_FILE = "state_news.json"

UA = "Mozilla/5.0 (compatible; BokuaoNewsDiscordNotifier/3.0)"

# Discord content limit
DISCORD_CONTENT_LIMIT = 2000  # content は 2000 まで

MAX_IMAGES_PER_POST = 10
MAX_IMAGE_BYTES = 7 * 1024 * 1024

# JPEGのみ（方針に合わせる）
ALLOWED_EXT = (".jpg", ".jpeg")

NEWS_WEBHOOK_URL = os.environ["BOKUAO_NEWS"]

NEWS_DATE_RE = re.compile(r"\b20\d{2}\.\d{2}\.\d{2}\b")
NEWS_CATEGORIES = {"OTHER", "NEWS", "EVENT", "MEDIA", "LIVE/EVENT"}  # 必要なら追加


# ---------- time ----------
def jst_today_yyyymmdd() -> str:
    jst = dt.timezone(dt.timedelta(hours=9))
    now = dt.datetime.now(tz=jst)
    return now.strftime("%Y.%m.%d")


# ---------- io ----------
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


# ---------- helpers ----------
def truncate(s: Optional[str], limit: int) -> str:
    if not s:
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


def _norm_comp(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[ \u3000]+", "", s)
    return s


def strip_leading_header_lines(lines: List[str], title: str, category: str, date: str) -> List[str]:
    """
    詳細ページ本文先頭に混入する見出し（タイトル/日付/カテゴリ等）を
    「先頭にある限り」剥がす（段落単位で扱う）。
    """
    t = _norm_comp(title)
    c = _norm_comp(category)
    d = _norm_comp(date)

    out = lines[:]
    while out:
        head_raw = (out[0] or "").strip()
        head = _norm_comp(head_raw)

        if not head:
            out.pop(0)
            continue

        # タイトル/カテゴリ/日付（完全一致）は除去
        if head == t or head == c or head == d:
            out.pop(0)
            continue

        # 日付単独行
        if NEWS_DATE_RE.fullmatch(head_raw):
            out.pop(0)
            continue

        # カテゴリ単独行（OTHER 等）
        if head_raw in NEWS_CATEGORIES:
            out.pop(0)
            continue

        # ありがちなノイズ
        if head in ("NEWS", "SHARE", "BACK", "SUPPORT"):
            out.pop(0)
            continue

        break

    # 先頭空行除去
    while out and not (out[0] or "").strip():
        out.pop(0)

    return out


def normalize_spaces(text: str) -> str:
    # 連続空白を詰める（日本語の見た目を崩さない範囲）
    return re.sub(r"[ \u3000]+", " ", (text or "")).strip()


# ---------- discord ----------
def webhook_post_json(payload: Dict) -> None:
    """
    Discord側が一時的に不調（5xx）でも落ちにくくする。
    """
    last_status = None
    last_body = None

    for attempt in range(1, 6):
        try:
            r = requests.post(NEWS_WEBHOOK_URL, json=payload, timeout=30)

            if 200 <= r.status_code < 300:
                return

            last_status = r.status_code
            last_body = (r.text or "")[:1200]
            print(f"[WEBHOOK] status={r.status_code} attempt={attempt} body={last_body}")

            if 500 <= r.status_code < 600:
                time.sleep(2.0 * attempt)
                continue

            r.raise_for_status()

        except requests.RequestException as e:
            print(f"[WEBHOOK] exception attempt={attempt}: {e}")
            time.sleep(2.0 * attempt)

    raise RuntimeError(f"Discord webhook failed after retries: status={last_status} body={last_body}")


def webhook_post_with_files(payload: Dict, files: List[Tuple[str, bytes]]) -> None:
    last_status = None
    last_body = None

    for attempt in range(1, 6):
        try:
            multipart = {f"files[{idx}]": (fname, data) for idx, (fname, data) in enumerate(files)}

            r = requests.post(
                NEWS_WEBHOOK_URL,
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files=multipart,
                timeout=60,
            )

            if 200 <= r.status_code < 300:
                return

            last_status = r.status_code
            last_body = (r.text or "")[:1200]
            print(f"[WEBHOOK] status={r.status_code} attempt={attempt} body={last_body}")

            if 500 <= r.status_code < 600:
                time.sleep(2.0 * attempt)
                continue

            r.raise_for_status()

        except requests.RequestException as e:
            print(f"[WEBHOOK] exception attempt={attempt}: {e}")
            time.sleep(2.0 * attempt)

    raise RuntimeError(f"Discord webhook(files) failed after retries: status={last_status} body={last_body}")


# ---------- scraping ----------
def download_images(urls: List[str]) -> List[Tuple[str, bytes]]:
    """
    JPEGのみダウンロードして (filename, bytes) を返す（大きすぎるものはスキップ）
    """
    out: List[Tuple[str, bytes]] = []
    for i, u in enumerate(urls, start=1):
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()

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
    out: List[Dict] = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)

    return out


def extract_news_body_paragraphs(container: BeautifulSoup, title: str, category: str, date: str) -> str:
    """
    改行が不自然になる根本原因（span細切れ + get_text("\\n")）を回避するため、
    「段落(p)」単位で抽出する。

    - 段落内は span 分割されていても結合（separator=""）
    - <br> は改行として残すため、先に "\\n" に置換
    - 先頭の見出しブロック（タイトル/日付/カテゴリ）を段落単位で除去
    """
    # 本文が入っていそうなブロックを優先（なければ container 全体）
    body_root = container.select_one("div.txt[data-delighter]") or container.select_one("div.txt") or container

    paras: List[str] = []

    # pが取れるならp優先
    ps = body_root.select("p")
    if ps:
        for p in ps:
            # br を改行として残す
            for br in p.find_all("br"):
                br.replace_with("\n")

            # 段落内は結合（ここが重要）
            s = p.get_text("", strip=True)
            s = normalize_spaces(s)
            if s:
                paras.append(s)
    else:
        # pがないページ用のフォールバック（div等から段落っぽく拾う）
        # ただし get_text("\n") だと崩れやすいので、短いブロックだけ拾う
        blocks = body_root.select("div, section, article")
        for b in blocks:
            # brは残す
            for br in b.find_all("br"):
                br.replace_with("\n")
            s = b.get_text("", strip=True)
            s = normalize_spaces(s)
            if s and len(s) >= 10:
                paras.append(s)

        # 重複が出ることがあるので軽く重複排除
        dedup: List[str] = []
        seen = set()
        for x in paras:
            k = _norm_comp(x)
            if k in seen:
                continue
            seen.add(k)
            dedup.append(x)
        paras = dedup

    # 先頭見出し除去（段落単位）
    paras = strip_leading_header_lines(paras, title, category, date)

    # 段落区切りは空行（Discordで読みやすい）
    return "\n\n".join(paras).strip()


def parse_news_detail(detail_url: str, title: str, category: str, list_date: str) -> Dict:
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

    # ページ内の日付（保険）
    page_date = ""
    raw_text = container.get_text("\n", strip=True)
    for ln in (raw_text.split("\n")[:200]):
        m = NEWS_DATE_RE.search(ln)
        if m:
            page_date = m.group(0)
            break

    # 本文（段落ベース）
    body = extract_news_body_paragraphs(container, title=title, category=category, date=list_date or page_date)

    # 末尾ノイズを軽くカット（必要なら）
    body = cut_at_first_marker(body, ["SHARE", "BACK", "SUPPORT"])

    # 日付は一覧優先、なければページ内
    date = list_date or page_date or "（不明）"

    return {
        "url": detail_url,
        "date": date,
        "body": body,
        "images": image_urls,
    }


# ---------- posting (NO EMBED) ----------
def build_content_text(title: str, url: str, body: str, category: str, date: str) -> str:
    """
    embedなし：content で送る
    - 1行目：太字 + リンク
    - 本文
    - 最後に「category / date」
    """
    header = f"**[{title}]({url})**"
    footer = f"{category} / {date}"

    parts = [header]
    if body:
        parts.append(body)
    parts.append(footer)

    content = "\n\n".join(parts).strip()
    return truncate(content, DISCORD_CONTENT_LIMIT)


def post_news_item_text_then_images(item: Dict) -> None:
    """
    1通目：content（embedなし）
    2通目：画像だけ添付（最大10枚）
    """
    title = item.get("title") or "（タイトル不明）"
    category = item.get("category") or "（不明）"
    date = item.get("date") or ""
    url = item["url"]

    detail = parse_news_detail(url, title=title, category=category, list_date=date)

    content = build_content_text(
        title=title,
        url=url,
        body=(detail.get("body") or "").strip(),
        category=category,
        date=(detail.get("date") or date or "（不明）"),
    )

    payload1 = {
        "content": content,
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

        post_news_item_text_then_images(it)
        notified.add(it["url"])
        posted += 1

        time.sleep(1.0)

    state["notified_news_urls"] = sorted(notified)
    save_state(state)
    print(f"[NEWS] Done. today={today} posted={posted} skipped(already)={skipped}")


if __name__ == "__main__":
    main()
