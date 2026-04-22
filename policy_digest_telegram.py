from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
KST = timezone(timedelta(hours=9), name="KST")


SEARCH_TERMS = [
    "사설",
    "칼럼",
    "논평",
    "비평",
    "시론",
    "오피니언",
]

MAJOR_SOURCES = {
    "조선일보": 100,
    "중앙일보": 99,
    "동아일보": 98,
    "한겨레": 97,
    "경향신문": 96,
    "한국일보": 95,
    "서울신문": 94,
    "국민일보": 93,
    "문화일보": 92,
    "세계일보": 91,
    "매일경제": 90,
    "한국경제": 89,
    "서울경제": 88,
    "파이낸셜뉴스": 87,
    "이데일리": 86,
    "머니투데이": 85,
    "아시아경제": 84,
    "조선비즈": 83,
    "Chosunbiz": 83,
}

OPINION_KEYWORDS = (
    "사설",
    "칼럼",
    "논평",
    "비평",
    "시론",
    "오피니언",
    "기고",
    "취재수첩",
    "기자수첩",
    "데스크",
    "분석",
)

EXCLUDE_TITLE_KEYWORDS = (
    "인사]",
    "[인사",
    "인사 발령",
    "수상",
    "성료",
    "설명회",
    "인터뷰",
    "해명]",
    "[해명",
    "동정",
    "WT논평",
)

EXCLUDE_SOURCES = (
    "대한민국 정책브리핑",
    "연합뉴스TV",
    "네이트",
    "v.daum.net",
)


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    link: str
    published: datetime | None
    query: str


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log(message: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with (LOG_DIR / "policy_digest.log").open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    return html.unescape(value).strip()


def build_queries() -> list[str]:
    return [f'"{term}"' for term in SEARCH_TERMS]


def google_news_rss_url(query: str, lookback_days: int) -> str:
    params = {
        "q": f"{query} when:{lookback_days}d",
        "hl": "ko",
        "gl": "KR",
        "ceid": "KR:ko",
    }
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def fetch_url(url: str, timeout: int = 20) -> bytes:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 opinion-digest-bot/1.0",
                    "Accept": "application/rss+xml, application/xml, text/xml",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def parse_google_rss(xml_bytes: bytes, query: str, limit: int) -> list[NewsItem]:
    root = ET.fromstring(xml_bytes)
    items: list[NewsItem] = []
    for item in root.findall("./channel/item")[:limit]:
        title = strip_tags(item.findtext("title", default=""))
        link = strip_tags(item.findtext("link", default=""))
        source_node = item.find("source")
        source = strip_tags(source_node.text if source_node is not None else "")
        pub_date = strip_tags(item.findtext("pubDate", default=""))
        published = None
        if pub_date:
            try:
                published = parsedate_to_datetime(pub_date)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        if title and link:
            items.append(
                NewsItem(
                    title=title,
                    source=source or "출처 미상",
                    link=link,
                    published=published,
                    query=query,
                )
            )
    return items


def source_score(source: str) -> int:
    for name, score in MAJOR_SOURCES.items():
        if name in source:
            return score
    return 0


def normalize_title(title: str) -> str:
    title = re.sub(r"\s+-\s+[^-]+$", "", title)
    return re.sub(r"\W+", "", title).lower()


def is_recent(item: NewsItem, lookback_hours: int, now: datetime) -> bool:
    if not item.published:
        return False
    published = item.published.astimezone(KST)
    if published > now + timedelta(minutes=5):
        return False
    return published >= now - timedelta(hours=lookback_hours)


def is_relevant_opinion_item(item: NewsItem, lookback_hours: int, now: datetime) -> bool:
    title = item.title
    source = item.source
    if any(blocked in source for blocked in EXCLUDE_SOURCES):
        return False
    if source_score(source) <= 0:
        return False
    if any(blocked in title for blocked in EXCLUDE_TITLE_KEYWORDS):
        return False
    if title.strip() in {"오피니언", "사설", "칼럼", "논평", "비평"}:
        return False
    if not any(keyword in title for keyword in OPINION_KEYWORDS):
        return False
    return is_recent(item, lookback_hours, now)


def collect_news(
    max_items_per_query: int,
    lookback_hours: int,
    max_digest_items: int,
) -> list[NewsItem]:
    collected: list[NewsItem] = []
    seen_titles: set[str] = set()
    now = datetime.now(KST)
    lookback_days = max(1, (lookback_hours + 23) // 24)

    for query in build_queries():
        try:
            xml_bytes = fetch_url(google_news_rss_url(query, lookback_days))
            raw_items = parse_google_rss(xml_bytes, query, max(max_items_per_query * 8, 40))
            for item in raw_items:
                if not is_relevant_opinion_item(item, lookback_hours=lookback_hours, now=now):
                    continue
                key = normalize_title(item.title)
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                collected.append(item)
        except Exception as exc:
            log(f"RSS fetch failed query={query!r}: {exc}")
        time.sleep(0.4)

    collected.sort(
        key=lambda item: (
            item.published or datetime.min.replace(tzinfo=timezone.utc),
            source_score(item.source),
        ),
        reverse=True,
    )
    return collected[:max_digest_items]


def item_time(item: NewsItem) -> str:
    if not item.published:
        return "시간 미상"
    return item.published.astimezone(KST).strftime("%m/%d %H:%M")


def html_link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def build_digest(items: list[NewsItem], lookback_hours: int, html_output: bool = False) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    lines = [
        f"주요 언론사 논설 모음 ({today})",
        "",
        f"주요 언론사의 사설, 칼럼, 논평, 비평을 최근 {lookback_hours}시간 기준으로 모았습니다.",
    ]
    if not items:
        lines.extend(["", "최근 기준에 맞는 항목이 없습니다."])
        return "\n".join(lines)

    for index, item in enumerate(items, start=1):
        title = html_link(item.link, item.title) if html_output else item.title
        lines.extend(
            [
                "",
                f"{index}. {title}",
                f"   {item.source} | {item_time(item)}",
            ]
        )

    return "\n".join(lines)


def telegram_api(token: str, method: str, payload: dict[str, str]) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            request = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    assert last_error is not None
    raise last_error


def parse_chat_ids(value: str) -> list[str]:
    return [chat_id.strip() for chat_id in value.split(",") if chat_id.strip()]


def send_telegram(token: str, chat_ids: list[str], text: str) -> None:
    max_len = 3900
    parts = [text[i : i + max_len] for i in range(0, len(text), max_len)]
    delivered_chats = 0
    failures: list[str] = []
    for chat_id in chat_ids:
        try:
            for idx, part in enumerate(parts, start=1):
                prefix = f"({idx}/{len(parts)})\n" if len(parts) > 1 else ""
                result = telegram_api(
                    token,
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": prefix + part,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "true",
                    },
                )
                if not result.get("ok"):
                    raise RuntimeError(f"Telegram API error chat_id={chat_id}: {result}")
                time.sleep(0.5)
            delivered_chats += 1
        except Exception as exc:
            failures.append(f"{chat_id}: {exc}")
            log(f"telegram send failed chat_id={chat_id}: {exc}")

    if delivered_chats == 0:
        raise RuntimeError("All Telegram deliveries failed: " + "; ".join(failures))


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    load_env(ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = parse_chat_ids(os.getenv("TELEGRAM_CHAT_ID", ""))
    max_items_per_query = env_int("MAX_ITEMS_PER_QUERY", 10)
    max_digest_items = env_int("MAX_DIGEST_ITEMS", 20)
    lookback_hours = env_int("NEWS_LOOKBACK_HOURS", 24)
    fallback_lookback_hours = env_int("FALLBACK_NEWS_LOOKBACK_HOURS", 48)
    dry_run = os.getenv("DRY_RUN", "0").strip() == "1"

    if not token or not chat_ids:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.", file=sys.stderr)
        return 2

    items = collect_news(
        max_items_per_query=max_items_per_query,
        lookback_hours=lookback_hours,
        max_digest_items=max_digest_items,
    )
    effective_lookback_hours = lookback_hours
    if not items and fallback_lookback_hours > lookback_hours:
        items = collect_news(
            max_items_per_query=max_items_per_query,
            lookback_hours=fallback_lookback_hours,
            max_digest_items=max_digest_items,
        )
        effective_lookback_hours = fallback_lookback_hours

    if dry_run:
        print(build_digest(items, lookback_hours=effective_lookback_hours, html_output=False))
    else:
        send_telegram(
            token,
            chat_ids,
            build_digest(items, lookback_hours=effective_lookback_hours, html_output=True),
        )
    log(f"sent digest items={len(items)} dry_run={dry_run} lookback_hours={effective_lookback_hours}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        log(f"network error: {exc}")
        raise
    except Exception as exc:
        log(f"fatal error: {exc}")
        raise
