"""arXiv -> Scirate注釈 -> Claude要約 -> Discord送信 を統合する。

実行: python -m src.main
環境変数: ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL
"""
from __future__ import annotations

import logging
import sys
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


def _sort_and_filter(papers: list[Paper], scirate_cfg: dict) -> list[Paper]:
    min_scites = int(scirate_cfg.get("min_scites", 0))
    if min_scites > 0:
        papers = [p for p in papers if (p.scites or 0) >= min_scites]
    if scirate_cfg.get("rank_by_scites", False):
        papers.sort(key=lambda p: (p.scites or 0), reverse=True)
    else:
        papers.sort(key=lambda p: (p.published or __import__("datetime").datetime.min), reverse=True)
    return papers


def main() -> int:
    cfg = load_config()
    watch = cfg.get("watch", {})
    arxiv_cfg = cfg.get("arxiv", {})
    scirate_cfg = cfg.get("scirate", {})
    summary_cfg = cfg.get("summary", {})
    discord_cfg = cfg.get("discord", {})

    # 1) arXiv から取得
    papers = arxiv_source.fetch_papers(watch, arxiv_cfg)

    # 2) Scirate で scite 数を付与（任意・失敗しても続行）
    if scirate_cfg.get("enabled", False):
        scite_map = scirate_source.fetch_scite_map(
            scirate_cfg.get("categories", []),
            interval=float(arxiv_cfg.get("request_interval_sec", 3.0)),
        )
        scirate_source.annotate(papers, scite_map)

        # scite上位の話題作を追加で拾う（add_trending）
        if scirate_cfg.get("add_trending", False) and scite_map:
            known = {p.version_less_id for p in papers}
            top = sorted(scite_map.items(), key=lambda kv: kv[1], reverse=True)
            top_n = int(scirate_cfg.get("trending_top_n", 5))
            added = 0
            for arxiv_id, sc in top:
                if added >= top_n:
                    break
                if arxiv_id in known:
                    continue
                # メタ情報を arXiv API から取得
                extra = arxiv_source.fetch_by_id(arxiv_id, arxiv_cfg)
                if extra:
                    extra.matched_by = "trending"
                    extra.scites = sc
                    papers.append(extra)
                    added += 1

    # 3) 重複除去（送信済みを除く）
    seen = state.load_seen()
    new_papers = [p for p in papers if p.version_less_id not in seen]
    log.info("送信候補: %d 件（送信済み除外後）", len(new_papers))

    # 4) 並べ替え・フィルタ・件数上限
    new_papers = _sort_and_filter(new_papers, scirate_cfg)
    cap = int(summary_cfg.get("max_papers_per_run", 10))
    if len(new_papers) > cap:
        log.info("上限 %d 件に制限（残りは次回）", cap)
        new_papers = new_papers[:cap]

    if not new_papers:
        log.info("新着なし。終了。")
        return 0

    # 5) Claude で要約
    summarize.summarize_papers(new_papers, summary_cfg)

    # 6) Discord へ送信
    discord_sender.send_papers(new_papers, discord_cfg)

    # 7) 送信済みとして記録
    for p in new_papers:
        seen.add(p.version_less_id)
    state.save_seen(seen)

    log.info("完了: %d 件を送信", len(new_papers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
