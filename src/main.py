"""複数チャンネル＋複数送信先(Discord/Slack)版オーケストレーション。

topicごとに: キーワード個別検索(どの語でヒットしたか記録) → seen除外 → scite付与
→ 並べ替え/上限 → 【上流で1回だけ要約】 → 各送信先(Discord/Slack)へ同じ要約を配信
→ 全送信先に届いた分だけ seen 記録（部分失敗は次回再送）。

実行: python -m src.main
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import (arxiv_source, scirate_source, summarize, figure,
               discord_sender, slack_sender, state)
from .models import Paper

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
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
        papers.sort(key=lambda p: (p.published or datetime.min.replace(tzinfo=timezone.utc)),
                    reverse=True)
    return papers[:cap] if cap and len(papers) > cap else papers


def _discord_env(topic: dict) -> str:
    # 旧 webhook_env / 新 discord_webhook_env の両対応
    return topic.get("discord_webhook_env") or topic.get("webhook_env") or ""


def main() -> int:
    cfg = load_config()
    defaults = cfg.get("defaults", {})
    arxiv_cfg = cfg.get("arxiv", {})
    scirate_cfg = cfg.get("scirate", {})
    summary_cfg = cfg.get("summary", {})
    discord_cfg = cfg.get("discord", {})
    slack_cfg = cfg.get("slack", {})
    topics = cfg.get("topics", [])

    default_cats = defaults.get("categories", [])
    default_cap = int(defaults.get("max_papers_per_run", 15))
    summary_enabled = bool(summary_cfg.get("enabled", True))

    # Slack 設定
    slack_enabled = bool(slack_cfg.get("enabled", False))
    slack_token = os.environ.get(slack_cfg.get("bot_token_env", "SLACK_BOT_TOKEN"), "")
    slack_thread = bool(slack_cfg.get("summary_in_thread", True))
    if slack_enabled and not slack_token:
        log.warning("Slack有効だが bot token 未設定のため Slack はスキップ")
        slack_enabled = False

    seen = state.load_seen()
    registry: dict[str, Paper] = {}
    topic_papers: dict[str, list[Paper]] = {}

    # --- 1) topicごとにキーワード個別検索 → seen除外 ---
    for topic in topics:
        name = topic["name"]
        cats = topic.get("categories", default_cats)
        try:
            fetched = arxiv_source.fetch_topic(name, topic.get("keywords", []), cats, arxiv_cfg)
        except Exception as e:
            log.error("[%s] 取得失敗（スキップ）: %s", name, e)
            topic_papers[name] = []
            continue
        seen_ids = seen.get(name, set())
        new_list: list[Paper] = []
        for p in fetched:
            if p.version_less_id in seen_ids:
                continue
            canonical = registry.setdefault(p.version_less_id, p)
            # 共有オブジェクトにこのtopicのヒットキーワードを反映
            canonical.matched_keywords.setdefault(name, [])
            for kw in p.matched_keywords.get(name, []):
                if kw not in canonical.matched_keywords[name]:
                    canonical.matched_keywords[name].append(kw)
            canonical.matched_by = name
            new_list.append(canonical)
        topic_papers[name] = new_list

    # --- 2) Scirate scite 付与 ---
    if scirate_cfg.get("enabled", False) and registry:
        scite_map = scirate_source.fetch_scite_map(
            scirate_cfg.get("categories", []),
            interval=float(arxiv_cfg.get("request_interval_sec", 3.0)))
        scirate_source.annotate(list(registry.values()), scite_map)

    # --- 3) 並べ替え・上限 ---
    for topic in topics:
        name = topic["name"]
        cap = int(topic.get("max_papers_per_run", default_cap))
        topic_papers[name] = _sort_and_cap(topic_papers[name], scirate_cfg, cap)

    # --- 3.5) ヒットキーワードを永続ログに記録（どの語で引っかかったか）---
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    match_entries = []
    for topic in topics:
        name = topic["name"]
        for p in topic_papers[name]:
            match_entries.append({
                "date": today, "topic": name, "arxiv_id": p.arxiv_id,
                "keywords": p.matched_keywords.get(name, []), "title": p.title,
            })
            log.info("[%s] %s | kw=%s | %s",
                     name, p.arxiv_id, p.matched_keywords.get(name, []), p.title[:80])
    state.append_match_log(match_entries)

    # --- 4) 上流で1回だけ要約（重複は共有オブジェクトなので1回）---
    to_summarize = {p.version_less_id: p for ps in topic_papers.values() for p in ps}
    if not to_summarize:
        log.info("新着なし。終了。")
        state.save_seen(seen)
        return 0
    summarize.summarize_papers(list(to_summarize.values()), summary_cfg)

    # --- 4.5) 高scite論文だけ「結果解説＋代表図」を上流で1回生成（Slack/Discord共用）---
    figure_cfg = cfg.get("figure", {})
    if figure_cfg.get("enabled", False):
        try:
            figure.build_for_papers(list(to_summarize.values()), figure_cfg)
        except Exception as e:
            log.error("図解生成でエラー（スキップ）: %s", e)

    # --- 5) 各topicを全送信先へ配信し、全送信先に届いた分だけ seen 記録 ---
    for topic in topics:
        name = topic["name"]
        papers = topic_papers[name]
        if not papers:
            continue

        per_dest_sent: list[set[str]] = []

        # Discord
        webhook_url = os.environ.get(_discord_env(topic), "")
        if webhook_url:
            try:
                ids = discord_sender.send_papers(papers, webhook_url, discord_cfg, name)
                discord_sender.send_figures(papers, webhook_url, discord_cfg, name)
            except Exception as e:
                log.error("[%s/Discord] 例外: %s", name, e); ids = []
            per_dest_sent.append(set(ids))

        # Slack
        slack_channel = topic.get("slack_channel", "")
        if slack_enabled and slack_channel:
            try:
                ids = slack_sender.send_papers(papers, slack_token, slack_channel, None,
                                               name, summary_enabled, slack_thread)
            except Exception as e:
                log.error("[%s/Slack] 例外: %s", name, e); ids = []
            per_dest_sent.append(set(ids))

        if not per_dest_sent:
            log.warning("[%s] 送信先が未設定のためスキップ", name)
            continue

        # 全送信先に届いた論文だけ seen（部分失敗は次回再送＝取りこぼし防止）
        fully_sent = set.intersection(*per_dest_sent) if per_dest_sent else set()
        if fully_sent:
            seen.setdefault(name, set()).update(fully_sent)

    state.save_seen(seen)
    log.info("完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
