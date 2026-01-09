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

UA = "Mozilla/5.0 (compatible; BokuaoNewsDiscordNotifier/3.1)"

# Discord limits
DISCORD_CONTENT_LIMIT = 2000          # message content
EMBED_TITLE_LIMIT = 256              # embed title
EMBED_DESC_LIMIT_HARD = 4096         # Discord hard limit (embed.description)
EMBED_DESC_LIMIT_SOFT = 4000         # 運用ルール：ここを超えたら「2通目以降」に回す（embedは最大4000相当）

MAX_IMAGES_PER_POST = 10
MAX_IMAGE_BYTES = 7 * 1024 * 1024

# JPEGのみ
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
    「先頭にある限り」剥がす（段落単位）。
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

        if head == t or head == c or head == d:
            out.pop(0)
            continue

        if NEWS_DATE_RE.fullmatch(head_raw):
            out.pop(0)
            continue

        if head_raw in NEWS_CATEGORIES:
            out.pop(0)
            continue

        if head in ("NEWS", "SHARE", "BACK", "SUPPORT"):
            out.pop(0)
            continue

        break

    while out and not (out[0] or "").strip():
        out.pop(0)

    return out


def normalize_spaces(text: str) -> str:
    # 連続空白を詰める（日本語の見た目を崩さない範囲）
    return re.sub(r"[ \u3000]+", " ", (text or "")).strip()


def chunk_text_by_paragraph(text: str, limit: int) -> List[str]:
    """
    content(2000)制限対策：段落境界（空行）を優先して分割。
    それでも単一段落が長すぎる場合は強制分割。
    """
    text = (text or "").strip()
    if not text:
        return []

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            chunks.append(buf)
            buf = ""

    for p in paras:
        candidate = (buf + ("\n\n" if buf else "") + p).strip()
        if len(candidate) <= limit:
            buf = candidate
            continue

        # いったん確定
        flush()

        # 段落単体がlimit超なら強制分割
        while len(p) > limit:
            chunks.append(p[:limit])
            p = p[limit:]
        buf = p

    flush()
    return chunks


def split_for_embed(body: str, embed_body_limit: int) -> Tuple[str, str]:
    """
    body を (embedに入れる本文, 残り) に分割。
    なるべく段落境界で切る。最悪は文字で切る。
    """
    body = (body or "").strip()
    if not body:
        return "", ""

    if len(body) <= embed_body_limit:
        return body, ""

    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    acc: List[str] = []
    cur = 0

    for p in paras:
        add = len(p) + (2 if acc else 0)  # "\n\n"
        if cur + add <= embed_body_limit:
            acc.append(p)
            cur += add
        else:
            break

    if acc:
        primary = "\n\n".join(acc).strip()
        rest = body[len(primary):].lstrip()
        rest = rest.lstrip("\n").lstrip()
        return primary, rest

    # 1段落目から長すぎる場合
    return body[:embed_body_limit], body[embed_body_limit:]


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

            # 5xxはリトライ
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
    span細切れ + get_text("\\n") で「1文字改行」になるのを避けるため、段落(p)単位で抽出。
    - 段落内は separator="" で結合
    - <br> は改行として残す
    """
    body_root = container.select_one("div.txt[data-delighter]") or container.select_one("div.txt") or container

    paras: List[str] = []
    ps = body_root.select("p")

    if ps:
        for p in ps:
            for br in p.find_all("br"):
                br.replace_with("\n")
            s = p.get_text("", strip=True)
            s = normalize_spaces(s)
            if s:
                paras.append(s)
    else:
        # pが無いページ用フォールバック（取り過ぎを避けつつ拾う）
        blocks = body_root.select("div, section, article")
        for b in blocks:
            for br in b.find_all("br"):
                br.replace_with("\n")
            s = b.get_text("", strip=True)
            s = normalize_spaces(s)
            if s and len(s) >= 10:
                paras.append(s)

        # 軽い重複排除
        dedup: List[str] = []
        seen = set()
        for x in paras:
            k = _norm_comp(x)
            if k in seen:
                continue
            seen.add(k)
            dedup.append(x)
        paras = dedup

    paras = strip_leading_header_lines(paras, title, category, date)
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

    # images (JPEG only)
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

    # page date fallback
    page_date = ""
    raw_text = container.get_text("\n", strip=True)
    for ln in (raw_text.split("\n")[:200]):
        m = NEWS_DATE_RE.search(ln)
        if m:
            page_date = m.group(0)
            break

    body = extract_news_body_paragraphs(container, title=title, category=category, date=list_date or page_date)
    body = cut_at_first_marker(body, ["SHARE", "BACK", "SUPPORT"])

    date = list_date or page_date or "（不明）"

    return {
        "url": detail_url,
        "date": date,
        "body": body,
        "images": image_urls,
    }


# ---------- posting (EMBED + overflow to 2nd message(s)) ----------
def build_embed_and_overflow_messages(
    title: str, url: str, body: str, category: str, date: str
) -> Tuple[Dict, List[str]]:
    """
    1通目：Embed（descriptionは最大 EMBED_DESC_LIMIT_SOFT まで）
    4000を超えた分は「2通目以降」のcontentメッセージとして送る。
    """
    embed_title = truncate(title, EMBED_TITLE_LIMIT)

    footer_line = f"{category} / {date}".strip()
    body = (body or "").strip()

    # embed末尾に footer_line を入れるので、その分を先に確保
    reserve = len("\n\n") + len(footer_line) if footer_line else 0
    embed_body_limit = max(0, EMBED_DESC_LIMIT_SOFT - reserve)

    body_for_embed, rest = split_for_embed(body, embed_body_limit)

    # embed description（本文 + footer）
    if body_for_embed.strip():
        desc = body_for_embed.rstrip()
        if footer_line:
            desc += "\n\n" + footer_line
    else:
        desc = footer_line if footer_line else ""

    # 念のため hard limit に収める（4000運用でも、何かの拍子に超えるのを防ぐ）
    desc = truncate(desc, EMBED_DESC_LIMIT_HARD)

    embed = {
        "title": embed_title,
        "url": url,
        "description": desc,
    }

    overflow_msgs: List[str] = []
    rest = (rest or "").strip()
    if rest:
        # 「続き」ヘッダを最初のメッセージに付ける（2000以内で）
        chunks = chunk_text_by_paragraph(rest, DISCORD_CONTENT_LIMIT)
        if chunks:
            chunks[0] = truncate("（続き）\n" + chunks[0], DISCORD_CONTENT_LIMIT)
        overflow_msgs = chunks

    return embed, overflow_msgs


def post_news_item_embed_then_overflow_then_images(item: Dict) -> None:
    """
    1通目：Embed（最大4000相当）
    2通目以降：溢れた本文（content、2000文字ごと）
    最後：画像（あれば）
    """
    title = item.get("title") or "（タイトル不明）"
    category = item.get("category") or "（不明）"
    date = item.get("date") or ""
    url = item["url"]

    detail = parse_news_detail(url, title=title, category=category, list_date=date)

    final_date = (detail.get("date") or date or "（不明）").strip()
    body = (detail.get("body") or "").strip()

    embed, overflow_msgs = build_embed_and_overflow_messages(
        title=title,
        url=url,
        body=body,
        category=category,
        date=final_date,
    )

    payload1 = {
        "content": "",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    webhook_post_json(payload1)
    time.sleep(0.9)

    # 2通目以降（必要なら複数通）
    for msg in overflow_msgs:
        payload = {
            "content": truncate(msg, DISCORD_CONTENT_LIMIT),
            "allowed_mentions": {"parse": []},
        }
        webhook_post_json(payload)
        time.sleep(0.9)

    # images（JPEGのみ・最大10枚）
    imgs = (detail.get("images") or [])[:MAX_IMAGES_PER_POST]
    if not imgs:
        return

    files = download_images(imgs)
    if not files:
        return

    payload_img = {
        "content": "",
        "allowed_mentions": {"parse": []},
    }
    webhook_post_with_files(payload_img, files)


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

        post_news_item_embed_then_overflow_then_images(it)
        notified.add(it["url"])
        posted += 1

        time.sleep(1.0)

    state["notified_news_urls"] = sorted(notified)
    save_state(state)
    print(f"[NEWS] Done. today={today} posted={posted} skipped(already)={skipped}")


if __name__ == "__main__":
    main()
