"""Discord Webhook へ要約付き論文を送信する。

Webhook URL は環境変数 DISCORD_WEBHOOK_URL から読む。
1メッセージに複数 embed（最大10）をまとめて送り、429 は retry_after に従う。
"""
from __future__ import annotations

import logging
import os
import time

import requests

from .models import Paper

log = logging.getLogger(__name__)

ARXIV_RED = 0xB31B1B
EMBED_DESC_LIMIT = 4000   # Discordの上限は4096。安全マージン。


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _build_embed(paper: Paper) -> dict:
    parts = [paper.summary.strip()]
    desc = _truncate("\n\n".join(parts), EMBED_DESC_LIMIT)

    fields = []
    authors = ", ".join(paper.authors[:6])
    if len(paper.authors) > 6:
        authors += f" 他{len(paper.authors) - 6}名"
    if authors:
        fields.append({"name": "著者", "value": _truncate(authors, 1024), "inline": False})
    if paper.categories:
        fields.append({"name": "分野", "value": ", ".join(paper.categories[:8]), "inline": True})
    if paper.scites is not None:
        fields.append({"name": "Scite数", "value": str(paper.scites), "inline": True})
    fields.append({"name": "リンク", "value": f"[abs]({paper.abs_url}) ・ [PDF]({paper.pdf_url})", "inline": True})

    footer = paper.matched_by
    if paper.published:
        footer = f"{footer} ・ {paper.published.strftime('%Y-%m-%d')}"

    return {
        "title": _truncate(paper.title, 256),
        "url": paper.abs_url,
        "description": desc,
        "color": ARXIV_RED,
        "fields": fields,
        "footer": {"text": footer},
    }


def _post(webhook_url: str, payload: dict) -> None:
    while True:
        resp = requests.post(webhook_url, json=payload, timeout=30)
        if resp.status_code == 429:  # レート制限
            retry_after = resp.json().get("retry_after", 1)
            log.warning("Discord レート制限。%.1f秒待機", retry_after)
            time.sleep(float(retry_after) + 0.5)
            continue
        if resp.status_code >= 400:
            log.error("Discord 送信失敗 %s: %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()
        return


def send_papers(papers: list[Paper], discord_cfg: dict) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("環境変数 DISCORD_WEBHOOK_URL が未設定です")
    if not papers:
        log.info("送信対象なし")
        return

    username = discord_cfg.get("username", "arXiv Bot")
    batch_size = min(int(discord_cfg.get("embeds_per_message", 8)), 10)

    # 先頭にヘッダーメッセージ
    _post(webhook_url, {
        "username": username,
        "content": f"📚 新着論文 **{len(papers)}件** をお届けします",
    })
    time.sleep(0.5)

    for start in range(0, len(papers), batch_size):
        batch = papers[start:start + batch_size]
        _post(webhook_url, {
            "username": username,
            "embeds": [_build_embed(p) for p in batch],
        })
        log.info("Discord 送信: %d 件", len(batch))
        time.sleep(0.7)  # 礼儀
