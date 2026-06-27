"""複数チャンネル版オーケストレーション。

各 topic ごとに arXiv 検索 → 重複(seen)除外 → scite付与 → 並べ替え/上限 →
（必要なら）要約 → topic専用 Webhook へ送信 → 送信成功分のみ seen 記録。

実行: python -m src.main
環境変数: 各 topic の webhook_env で指定したもの（+ 要約ありなら ANTHROPIC_API_KEY）
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

from . import arxiv_source, scirate_source, summarize, discord_sender, state
from .models import Paper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _sort_and_cap(papers: list[Paper], scirate_cfg: dict, cap: int) -> list[Paper]:
    if int(scirate_cfg.get("min_scites", 0)) > 0:
        m = int(scirate_cfg["min_scites"])
        papers = [p for p in papers if (p.scites or 0) >= m]
    if scirate_cfg.get("rank_by_scites", False):
        papers.sort(key=lambda p: (p.scites or 0), reverse=True)
    else:
        papers.sort(key=lambda p: (p.published or datetime.min.replace(tzinfo=None)),
                    reverse=True)
    return papers[:cap] if cap and len(papers) > cap else papers


def main() -> int:
    cfg = load_config()
    defaults = cfg.get("defaults", {})
    arxiv_cfg = cfg.get("arxiv", {})
    scirate_cfg = cfg.get("scirate", {})
    summary_cfg = cfg.get("summary", {})
    discord_cfg = cfg.get("discord", {})
    topics = cfg.get("topics", [])

    default_cats = defaults.get("categories", [])
    default_cap = int(defaults.get("max_papers_per_run", 15))

    seen = state.load_seen()
    registry: dict[str, Paper] = {}        # id -> Paper（要約を共有して1回だけ要約するため）
    topic_papers: dict[str, list[Paper]] = {}

    # --- 1) topicごとに取得し、seen除外 ---
    for topic in topics:
        name = topic["name"]
        watch = {
            "keywords": topic.get("keywords", []),
            "authors": [],
            "categories": topic.get("categories", default_cats),
        }
        try:
            fetched = arxiv_source.fetch_papers(watch, arxiv_cfg)
        except Exception as e:
            log.error("[%s] 取得失敗（このtopicはスキップ）: %s", name, e)
            topic_papers[name] = []
            continue

        seen_ids = seen.get(name, set())
        new_list: list[Paper] = []
        for p in fetched:
            if p.version_less_id in seen_ids:
                continue
            canonical = registry.setdefault(p.version_less_id, p)
            new_list.append(canonical)
        topic_papers[name] = new_list
        log.info("[%s] 新着候補 %d 件", name, len(new_list))

    # --- 2) Scirate で scite 付与（任意・失敗しても続行）---
    if scirate_cfg.get("enabled", False) and registry:
        scite_map = scirate_source.fetch_scite_map(
            scirate_cfg.get("categories", []),
            interval=float(arxiv_cfg.get("request_interval_sec", 3.0)),
        )
        scirate_source.annotate(list(registry.values()), scite_map)

    # --- 3) topicごとに並べ替え・上限 ---
    for topic in topics:
        name = topic["name"]
        cap = int(topic.get("max_papers_per_run", default_cap))
        topic_papers[name] = _sort_and_cap(topic_papers[name], scirate_cfg, cap)

    # --- 4) 送る予定の論文だけ要約（重複は共有オブジェクトなので1回だけ）---
    to_summarize = {p.version_less_id: p for ps in topic_papers.values() for p in ps}
    if to_summarize:
        for p in to_summarize.values():
            # matched_by に該当topicを書いておく（複数該当も表現）
            hit = [t["name"] for t in topics if p in topic_papers[t["name"]]]
            p.matched_by = " / ".join(hit)
        summarize.summarize_papers(list(to_summarize.values()), summary_cfg)
    else:
        log.info("新着なし。終了。")
        return 0

    # --- 5) topicごとに送信し、成功分だけ seen 記録 ---
    for topic in topics:
        name = topic["name"]
        papers = topic_papers[name]
        if not papers:
            continue
        env_name = topic.get("webhook_env", "")
        webhook_url = os.environ.get(env_name, "")
        if not webhook_url:
            log.warning("[%s] %s が未設定のためスキップ", name, env_name)
            continue
        try:
            sent_ids = discord_sender.send_papers(papers, webhook_url, discord_cfg, name)
        except Exception as e:
            log.error("[%s] 送信中に例外（このtopicはスキップ）: %s", name, e)
            sent_ids = []
        if sent_ids:
            seen.setdefault(name, set()).update(sent_ids)

    state.save_seen(seen)
    log.info("完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
