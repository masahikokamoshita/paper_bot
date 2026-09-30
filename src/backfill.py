"""リソース推定バックフィル: 指定期間のarXiv論文を検索し、moonshot未収録のものだけを
resource_est の判定→抽出→Slack/PR パイプラインに流す。

通常運用（main.py）は lookback_hours（直近4日）の新着しか見ないので、
過去の論文をまとめて処理したいときに使う。通常のtopic配信（Slack/Discordの新着通知）は
行わず、リソース推定パイプラインだけを実行する。

使い方（ローカル）:
    python -m src.backfill --from 2025-01-01 --to 2025-06-30 --dry-run   # 候補を数えるだけ
    python -m src.backfill --from 2025-01-01 --to 2025-06-30 --max 10    # 最大10件を処理

GitHub Actions からは .github/workflows/backfill.yml の Run workflow で期間を入力して実行。

必要な環境変数: OPENAI_API_KEY, SLACK_BOT_TOKEN, SLACK_CHANNEL_RESOURCE_EST,
              （auto_pr有効時）MOONSHOT_PAT
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.parse
from datetime import datetime

import feedparser
import requests

from . import resource_est
from .arxiv_source import (API_URL, USER_AGENT, _arxiv_id_from_entry_id,
                           _build_kw_query, _category_clause, _parse_published)
from .main import load_config
from .models import Paper

log = logging.getLogger(__name__)

# =====================================================================
# 【ここが設定の一等地】
# =====================================================================
DEFAULTS = {
    "page_size": 100,          # arXiv API 1リクエストあたりの取得件数
    "max_total": 500,          # 期間内に検索する上限（暴走防止）
    "max_process": 20,         # 1回のバックフィルでパイプラインに流す上限（コスト制御）
    "interval_sec": 3.0,       # arXiv APIへのリクエスト間隔（礼儀）
    "retry_backoff": [15, 45, 90],  # 429/5xx時の待機秒
}


def _fetch_range(keywords: list[str], categories: list[str],
                 date_from: str, date_to: str, page_size: int, max_total: int,
                 interval: float, backoff: list[int]) -> list[Paper]:
    """期間指定でarXivを検索し、全キーワードのOR検索結果をページングで集める。"""
    kw_clause = "(" + " OR ".join(_build_kw_query(k) for k in keywords) + ")"
    cat_clause = _category_clause(categories)
    d0 = datetime.strptime(date_from, "%Y-%m-%d").strftime("%Y%m%d0000")
    d1 = datetime.strptime(date_to, "%Y-%m-%d").strftime("%Y%m%d2359")
    parts = [kw_clause] + ([cat_clause] if cat_clause else []) + [f"submittedDate:[{d0} TO {d1}]"]
    query = " AND ".join(parts)
    log.info("arXiv期間検索: %s", query)

    papers: list[Paper] = []
    start = 0
    while start < max_total:
        params = {"search_query": query, "start": start, "max_results": page_size,
                  "sortBy": "submittedDate", "sortOrder": "descending"}
        url = f"{API_URL}?{urllib.parse.urlencode(params)}"
        resp = None
        for attempt in range(len(backoff) + 1):
            try:
                r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            except requests.RequestException as e:
                if attempt >= len(backoff):
                    log.error("arXiv接続失敗（リトライ上限）: %s", e)
                    return papers
                log.warning("arXiv接続失敗(%d回目)。%d秒待機: %s", attempt + 1, backoff[attempt], e)
                time.sleep(backoff[attempt])
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt >= len(backoff):
                    log.error("arXiv %s（リトライ上限）", r.status_code)
                    return papers
                wait = int(r.headers.get("Retry-After") or backoff[attempt])
                log.warning("arXiv %s(%d回目)。%d秒待機", r.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                log.error("arXivエラー %s", r.status_code)
                return papers
            resp = r
            break
        time.sleep(interval)
        if resp is None:
            return papers

        feed = feedparser.parse(resp.content)
        got = 0
        for entry in feed.entries:
            full_id = _arxiv_id_from_entry_id(entry.get("id", ""))
            if not full_id:
                continue
            pdf_url = ""
            for link in entry.get("links", []):
                if link.get("type") == "application/pdf":
                    pdf_url = link.get("href", "")
            papers.append(Paper(
                arxiv_id=full_id,
                title=" ".join(entry.get("title", "").split()),
                abstract=" ".join(entry.get("summary", "").split()),
                authors=[a.get("name", "") for a in entry.get("authors", [])],
                categories=[t.get("term", "") for t in entry.get("tags", [])],
                published=_parse_published(entry.get("published", "")),
                abs_url=entry.get("link", f"https://arxiv.org/abs/{full_id}"),
                pdf_url=pdf_url or f"https://arxiv.org/pdf/{full_id}",
            ))
            got += 1
        log.info("  start=%d: %d 件取得（累計 %d）", start, got, len(papers))
        if got < page_size:
            break
        start += page_size
    return papers


def _mark_matched_keywords(papers: list[Paper], topic_name: str, keywords: list[str]) -> None:
    """どのキーワードでヒットしたかをタイトル＋アブストの部分一致で記録（表示用）。"""
    for p in papers:
        text = f"{p.title} {p.abstract}".lower()
        hits = []
        for k in keywords:
            plain = k.split(":", 1)[1] if ":" in k and k.split(":", 1)[0] in (
                "ti", "abs", "au", "co", "cat", "jr", "rn", "all") else k
            if plain.strip('"').lower() in text:
                hits.append(plain.strip('"'))
        p.matched_keywords[topic_name] = hits or ["(期間検索)"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="リソース推定バックフィル")
    ap.add_argument("--from", dest="date_from", required=True, help="開始日 YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", required=True, help="終了日 YYYY-MM-DD")
    ap.add_argument("--max", dest="max_process", type=int, default=None,
                    help=f"処理する最大件数（既定 {DEFAULTS['max_process']}）")
    ap.add_argument("--dry-run", action="store_true",
                    help="候補の一覧表示のみ（LLM/Slack/PRは実行しない）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    cfg = load_config()
    re_cfg = cfg.get("resource_estimation", {})
    topic_name = resource_est._cfg(re_cfg, "topic_name")
    topic = next((t for t in cfg.get("topics", []) if t.get("name") == topic_name), None)
    if not topic:
        log.error("config.yaml に topic '%s' がありません", topic_name)
        return 1
    keywords = topic.get("keywords", [])
    categories = topic.get("categories") or cfg.get("arxiv", {}).get("categories", ["quant-ph"])
    bf = {**DEFAULTS, **cfg.get("backfill", {})}
    max_process = args.max_process if args.max_process is not None else int(bf["max_process"])

    # 1) 期間検索
    papers = _fetch_range(keywords, categories, args.date_from, args.date_to,
                          int(bf["page_size"]), int(bf["max_total"]),
                          float(bf["interval_sec"]), list(bf["retry_backoff"]))
    log.info("期間内マッチ: %d 件", len(papers))

    # 2) 除外: moonshot既収録 + 判定済み(done)
    known = resource_est.moonshot_arxiv_ids(resource_est._cfg(re_cfg, "schema_url"))
    done = resource_est._load_done()
    before = len(papers)
    papers = [p for p in papers if p.version_less_id not in known]
    n_known = before - len(papers)
    before = len(papers)
    papers = [p for p in papers if p.version_less_id not in done]
    n_done = before - len(papers)
    log.info("除外: moonshot既収録 %d 件 / 判定済み %d 件 → 候補 %d 件", n_known, n_done, len(papers))

    _mark_matched_keywords(papers, topic_name, keywords)

    # 3) 候補一覧を表示
    print(f"\n=== バックフィル候補 {args.date_from} 〜 {args.date_to}: {len(papers)} 件"
          f"（処理上限 {max_process}）===")
    for p in papers[:max_process]:
        d = p.published.strftime("%Y-%m-%d") if p.published else "----------"
        print(f"  {d}  {p.version_less_id:<12} {p.title[:80]}")
    if len(papers) > max_process:
        print(f"  ... 他 {len(papers) - max_process} 件（--max で上限を上げると処理されます）")
    if args.dry_run:
        print("\n--dry-run のためここで終了（LLM/Slack/PR は実行していません）")
        return 0
    if not papers:
        return 0

    # 4) パイプラインへ
    slack_cfg = cfg.get("slack", {})
    slack_token = os.environ.get(slack_cfg.get("bot_token_env", "SLACK_BOT_TOKEN"), "")
    channel = os.environ.get(resource_est._cfg(re_cfg, "slack_channel_env"), "")
    if not slack_token or not channel:
        log.error("SLACK_BOT_TOKEN / %s が未設定です", resource_est._cfg(re_cfg, "slack_channel_env"))
        return 1
    header = (f"🗂 *リソース推定バックフィル* {args.date_from} 〜 {args.date_to}: "
              f"候補 {min(len(papers), max_process)} 件を処理します"
              f"（期間内マッチ {len(papers) + n_known + n_done} / moonshot既収録 {n_known} 除外）")
    n = resource_est.run_pipeline(papers[:max_process], re_cfg, slack_token, channel, header=header)
    log.info("バックフィル完了: %d 件処理", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
