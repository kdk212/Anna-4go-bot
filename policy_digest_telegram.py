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
STATE_PATH = BASE_DIR / "policy_digest_state.json"
KST = timezone(timedelta(hours=9), name="KST")


POLICY_TERMS = [
    "정부 정책",
    "중앙정부 정책",
    "기획재정부 정책",
    "교육부 정책",
    "보건복지부 정책",
    "국토교통부 정책",
    "산업통상자원부 정책",
    "고용노동부 정책",
    "공정거래위원회 정책",
    "규제개혁",
]

OPINION_TERMS = "사설 OR 칼럼 OR 논평 OR 비평 OR 시론 OR 기고 OR 오피니언 OR 분석"

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
    "기고",
    "시론",
    "취재수첩",
    "기자수첩",
    "데스크",
    "오피니언",
    "전문가",
    "분석",
)

POLICY_RELEVANCE_KEYWORDS = (
    "정부",
    "정책",
    "중앙정부",
    "부처",
    "재경부",
    "교육부",
    "복지부",
    "국토부",
    "산업부",
    "고용부",
    "공정위",
    "공정거래",
    "규제",
    "개혁",
    "재정",
    "세금",
    "조세",
    "예산",
    "복지",
    "교육",
    "노동",
    "부동산",
    "주택",
    "의료",
    "물가",
    "민생",
    "산업",
    "고용",
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
)

EXCLUDE_SOURCES = (
    "대한민국 정책브리핑",
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
    return [f'"{term}" ({OPINION_TERMS})' for term in POLICY_TERMS]


def google_news_rss_url(query: str, lookback_days: int) -> str:
    params = {
        "q": f"{query} when:{lookback_days}d",
        "hl": "ko",
        "gl": "KR",
        "ceid": "KR:ko",
    }
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def fetch_url(url: str, timeout: int = 20) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 policy-digest-bot/1.0",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


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


def load_seen_links(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    links = data.get("sent_links", [])
    return set(links if isinstance(links, list) else [])


def save_seen_links(path: Path, links: set[str]) -> None:
    data = {
        "updated_at": datetime.now(KST).isoformat(),
        "sent_links": sorted(links)[-1000:],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_news(
    max_items_per_query: int,
    lookback_days: int,
    max_digest_items: int,
    seen_links: set[str],
) -> list[NewsItem]:
    collected: list[NewsItem] = []
    seen_titles: set[str] = set()
    for query in build_queries():
        try:
            xml_bytes = fetch_url(google_news_rss_url(query, lookback_days))
            raw_items = parse_google_rss(xml_bytes, query, max(max_items_per_query * 6, 30))
            for item in raw_items:
                if not is_relevant_opinion_item(item):
                    continue
                if item.link in seen_links:
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
            source_score(item.source),
            item.published or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return collected[:max_digest_items]


def is_relevant_opinion_item(item: NewsItem) -> bool:
    title = item.title
    source = item.source
    if any(blocked in source for blocked in EXCLUDE_SOURCES):
        return False
    if source_score(source) <= 0:
        return False
    if any(blocked in title for blocked in EXCLUDE_TITLE_KEYWORDS):
        return False
    has_opinion_marker = any(keyword in title for keyword in OPINION_KEYWORDS)
    has_policy_marker = any(keyword in title for keyword in POLICY_RELEVANCE_KEYWORDS)
    return has_opinion_marker and has_policy_marker


def source_score(source: str) -> int:
    for name, score in MAJOR_SOURCES.items():
        if name in source:
            return score
    return 0


def normalize_title(title: str) -> str:
    title = re.sub(r"\s+-\s+[^-]+$", "", title)
    return re.sub(r"\W+", "", title).lower()


def item_time(item: NewsItem) -> str:
    if not item.published:
        return "시간 미상"
    return item.published.astimezone(KST).strftime("%m/%d %H:%M")


def html_link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def build_digest(items: list[NewsItem], lookback_days: int, html_output: bool = False) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    lines = [
        f"중앙정부 정책 주요 논설 ({today})",
        "",
        f"주요 언론사의 사설, 칼럼, 논평, 비평 중심으로 최근 {lookback_days}일 새 글을 모았습니다.",
    ]
    if not items:
        lines.extend(["", "오늘 수집된 새 항목이 없습니다."])
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
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def send_telegram(token: str, chat_id: str, text: str) -> None:
    max_len = 3900
    parts = [text[i : i + max_len] for i in range(0, len(text), max_len)]
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
            raise RuntimeError(f"Telegram API error: {result}")
        time.sleep(0.5)


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
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    max_items_per_query = env_int("MAX_ITEMS_PER_QUERY", 5)
    max_digest_items = env_int("MAX_DIGEST_ITEMS", 20)
    lookback_days = env_int("NEWS_LOOKBACK_DAYS", 7)
    fallback_lookback_days = env_int("FALLBACK_NEWS_LOOKBACK_DAYS", 7)
    dry_run = os.getenv("DRY_RUN", "0").strip() == "1"
    ignore_sent = os.getenv("IGNORE_SENT", "0").strip() == "1"

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.", file=sys.stderr)
        return 2

    seen_links = set() if ignore_sent else load_seen_links(STATE_PATH)
    items = collect_news(
        max_items_per_query=max_items_per_query,
        lookback_days=lookback_days,
        max_digest_items=max_digest_items,
        seen_links=seen_links,
    )
    digest_lookback_days = lookback_days
    if not items and fallback_lookback_days > lookback_days:
        items = collect_news(
            max_items_per_query=max_items_per_query,
            lookback_days=fallback_lookback_days,
            max_digest_items=max_digest_items,
            seen_links=seen_links,
        )
        digest_lookback_days = fallback_lookback_days

    if dry_run:
        print(build_digest(items, lookback_days=digest_lookback_days, html_output=False))
    else:
        send_telegram(token, chat_id, build_digest(items, lookback_days=digest_lookback_days, html_output=True))
        if items:
            original_seen_links = load_seen_links(STATE_PATH)
            save_seen_links(STATE_PATH, original_seen_links | {item.link for item in items})
    log(f"sent digest items={len(items)} dry_run={dry_run}")
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
