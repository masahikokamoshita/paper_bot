"""arXiv 公式 API から論文を取得する。

API ドキュメント: https://info.arxiv.org/help/api/
Atom XML を feedparser で解析する。礼儀として連続アクセスには間隔を空ける。
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import feedparser
import requests

from .models import Paper

log = logging.getLogger(__name__)

API_URL = "http://export.arxiv.org/api/query"
USER_AGENT = "arxiv-discord-bot/1.0 (https://github.com/; research use)"
# arXiv への謝辞（利用規約で推奨されている）:
# "Thank you to arXiv for use of its open access interoperability."


def _arxiv_id_from_entry_id(entry_id: str) -> str:
    """'http://arxiv.org/abs/2509.01615v1' -> '2509.01615v1'"""
    return entry_id.rstrip("/").split("/abs/")[-1]


def _parse_published(value: str) -> datetime | None:
    try:
        # feedparser は 'published' を RFC3339 文字列で返す
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _query_api(search_query: str, max_results: int, interval: float) -> list[Paper]:
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    log.info("arXiv query: %s", search_query)

    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("arXiv リクエスト失敗: %s", e)
        return []
    finally:
        time.sleep(interval)  # 礼儀: 連続アクセスを避ける

    feed = feedparser.parse(resp.content)
    papers: list[Paper] = []
    for entry in feed.entries:
        full_id = _arxiv_id_from_entry_id(entry.get("id", ""))
        if not full_id:
            continue
        pdf_url = ""
        for link in entry.get("links", []):
            if link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
        papers.append(
            Paper(
                arxiv_id=full_id,
                title=" ".join(entry.get("title", "").split()),
                abstract=" ".join(entry.get("summary", "").split()),
                authors=[a.get("name", "") for a in entry.get("authors", [])],
                categories=[t.get("term", "") for t in entry.get("tags", [])],
                published=_parse_published(entry.get("published", "")),
                abs_url=entry.get("link", f"https://arxiv.org/abs/{full_id}"),
                pdf_url=pdf_url or f"https://arxiv.org/pdf/{full_id}",
            )
        )
    log.info("  -> %d 件取得", len(papers))
    return papers


def fetch_by_id(arxiv_id: str, arxiv_cfg: dict) -> Paper | None:
    """arXiv ID 単体でメタ情報を取得する（add_trending 用）。"""
    interval = float(arxiv_cfg.get("request_interval_sec", 3.0))
    params = {"id_list": arxiv_id, "max_results": 1}
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("arXiv id取得失敗 %s: %s", arxiv_id, e)
        return None
    finally:
        time.sleep(interval)

    feed = feedparser.parse(resp.content)
    if not feed.entries:
        return None
    entry = feed.entries[0]
    full_id = _arxiv_id_from_entry_id(entry.get("id", "")) or arxiv_id
    pdf_url = next(
        (l.get("href", "") for l in entry.get("links", []) if l.get("type") == "application/pdf"),
        f"https://arxiv.org/pdf/{full_id}",
    )
    return Paper(
        arxiv_id=full_id,
        title=" ".join(entry.get("title", "").split()),
        abstract=" ".join(entry.get("summary", "").split()),
        authors=[a.get("name", "") for a in entry.get("authors", [])],
        categories=[t.get("term", "") for t in entry.get("tags", [])],
        published=_parse_published(entry.get("published", "")),
        abs_url=entry.get("link", f"https://arxiv.org/abs/{full_id}"),
        pdf_url=pdf_url,
    )


def _category_clause(categories: list[str]) -> str:
    if not categories:
        return ""
    inner = " OR ".join(f"cat:{c}" for c in categories)
    return f"({inner})"


def fetch_papers(watch: dict, arxiv_cfg: dict) -> list[Paper]:
    """設定に基づき keyword 検索と author 検索を実行し、重複を除いて返す。"""
    keywords = watch.get("keywords", []) or []
    authors = watch.get("authors", []) or []
    categories = watch.get("categories", []) or []
    max_results = int(arxiv_cfg.get("max_results_per_query", 40))
    interval = float(arxiv_cfg.get("request_interval_sec", 3.0))
    lookback = int(arxiv_cfg.get("lookback_hours", 30))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)

    cat_clause = _category_clause(categories)
    collected: dict[str, Paper] = {}

    # --- キーワード検索（フレーズはダブルクォートで囲み OR 連結）---
    if keywords:
        kw_clause = " OR ".join(f'all:"{k}"' for k in keywords)
        query = f"({kw_clause})"
        if cat_clause:
            query = f"{query} AND {cat_clause}"
        for p in _query_api(query, max_results, interval):
            p.matched_by = "keyword"
            collected.setdefault(p.version_less_id, p)

    # --- 著者検索（著者ごとに1クエリ）---
    for author in authors:
        query = f'au:"{author}"'
        if cat_clause:
            query = f"{query} AND {cat_clause}"
        for p in _query_api(query, max_results, interval):
            existing = collected.get(p.version_less_id)
            if existing:
                existing.matched_by = "keyword+author"
            else:
                p.matched_by = "author"
                collected[p.version_less_id] = p

    # --- lookback_hours より古いものは新着扱いしない ---
    fresh = [
        p for p in collected.values()
        if p.published is None or p.published >= cutoff
    ]
    log.info("arXiv: 重複除去後 %d 件 / 新着 %d 件", len(collected), len(fresh))
    return fresh
