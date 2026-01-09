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

UA = "Mozilla/5.0 (compatible; BokuaoNewsDiscordNotifier/2.2)"

# Discord embed limits
EMBED_TITLE_LIMIT = 256
EMBED_DESC_LIMIT_SAFE = 4000  # 末尾追記などを考慮した安全側
EMBED_DESC_LIMIT_HARD = 4096  # 最終ガード

MAX_IMAGES_PER_POST = 10
MAX_IMAGE_BYTES = 7 * 1024 * 1024

# JPEGのみ
ALLOWED_EXT = (".jpg", ".jpeg")

NEWS_WEBHOOK_URL = os.environ["BOKUAO_NEWS"]

NEWS_DATE_RE = re.compile(r"\b20\d{2}\.\d{2}\.\d{2}\b")
NEWS_CATEGORIES = {"OTHER", "NEWS", "EVENT", "MEDIA"}


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
    # 比較用：空白除去
    s = (s or "").strip()
    s = re.sub(r"[ \u3000]+", "", s)
    return s


def strip_leading_header_lines(desc: str, title: str, category: str, date: str) -> str:
    """
    news詳細ページ本文先頭に混入する見出しブロック（タイトル/日付/カテゴリ等）を、
    先頭にある限り剥がす。
    """
    t = _norm_comp(title)
    c = _norm_comp(category)
    d = _norm_comp(date)

    lines = (desc or "").split("\n")

    while lines:
        head_raw = lines[0].strip()
        head = _norm_comp(head_raw)

        if not head:
            lines.pop(0)
            continue

        # タイトル/カテゴリ/日付（完全一致）は除去
        if head == t or head == c or head == d:
            lines.pop(0)
            continue

        # 日付単独行
        if NEWS_DATE_RE.fullmatch(head_raw):
            lines.pop(0)
            continue

        # カテゴリ単独行（OTHER 等）
        if head_raw in NEWS_CATEGORIES:
            lines.pop(0)
            continue

        # ありがちなノイズ
        if head in ("NEWS", "SHARE", "BACK", "SUPPORT"):
            lines.pop(0)
            continue

        break

    # 先頭空行除去
    while lines and not lines[0].strip():
        lines.pop(0)

    return "\n".join(lines).strip()


def normalize_newlines_jp(text: str) -> str:
    """
    不自然な「1文字ずつ改行」や短すぎる行の連続を、ある程度結合して読みやすくする。
    - 1〜2文字程度の行が続く場合は同一段落として結合
    - ただし記号だけの行や見出しっぽいものは極力温存
    """
    lines = [ln.rstrip() for ln in (text or "").split("\n")]
    out: List[str] = []
    buf: List[str] = []

    def flush_buf():
        if not buf:
            return
        # バッファは結合（空白なしで連結）
        out.append("".join(buf).strip())
        buf.clear()

    for ln in lines:
        s = ln.strip()
        if not s:
            flush_buf()
            out.append("")  # 段落区切り
            continue

        # 行が極端に短い（1〜2文字）ならバッファへ
        if len(s) <= 2:
            buf.append(s)
            continue

        # バッファが溜まっていて、今の行が普通の長さなら先に吐く
        flush_buf()
        out.append(s)

    flush_buf()

    # 余分な空行圧縮（3連続以上→2連続）
    compact: List[str] = []
    empty_run = 0
    for ln in out:
        if ln == "":
            empty_run += 1
            if empty_run <= 2:
                compact.append("")
        else:
            empty_run = 0
            compact.append(ln)

    return "\n".join(compact).strip()


# ---------- discord ----------
def webhook_post_json(payload: Dict) -> None:
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

            # 5xxはリトライ、他は即raise
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

    # テキスト（いったん行で抜く）
    text = container.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln.strip()]

    # ページ内の日付（保険）
    date = "（不明）"
    for ln in lines[:120]:
        m = NEWS_DATE_RE.search(ln)
        if m:
            date = m.group(0)
            break

    body = "\n".join(lines)
    body = cut_at_first_marker(body, ["SHARE", "BACK", "SUPPORT"])
    body = "\n".join([ln for ln in body.split("\n") if ln.strip()]).strip()

    return {
        "url": detail_url,
        "date": date,
        "body": body,
        "images": image_urls,
    }


# ---------- posting ----------
def build_embed_description(detail_body: str, title: str, category: str, date: str) -> str:
    # まず安全側でtruncate（ここで余裕を確保）
    desc = truncate((detail_body or "").strip(), EMBED_DESC_LIMIT_SAFE)

    # 先頭見出し除去
    desc = strip_leading_header_lines(desc, title, category, date)

    # 改行整形（1文字改行抑制）
    desc = normalize_newlines_jp(desc)

    # 末尾にカテゴリ/日付を追記（太字なし）
    if desc:
        desc = desc.rstrip() + f"\n\n{category} / {date}"
    else:
        desc = f"{category} / {date}"

    # 最終ガード：必ず4096以内
    desc = truncate(desc, EMBED_DESC_LIMIT_HARD)
    return desc


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
    embed_desc = build_embed_description(detail.get("body") or "", title, category, date)

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
