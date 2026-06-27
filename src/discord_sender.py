"""Discord Webhook へ要約付き論文を送信する（topicごとに宛先URLを指定）。

送信に成功した論文IDだけを返すので、呼び出し側はそれだけを「送信済み」に
記録できる（at-least-once 配信。失敗分は次回再送 = 取りこぼし防止）。
"""
from __future__ import annotations

import logging
import time

import requests

from .models import Paper

log = logging.getLogger(__name__)

ARXIV_RED = 0xB31B1B
EMBED_DESC_LIMIT = 4000
MAX_SEND_RETRY = 4  # 一時的な失敗のリトライ回数


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _build_embed(paper: Paper) -> dict:
    desc = _truncate(paper.summary.strip(), EMBED_DESC_LIMIT)
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


def _post(webhook_url: str, payload: dict) -> bool:
    """1リクエスト送信。成功でTrue。一時的失敗はリトライ。"""
    for attempt in range(MAX_SEND_RETRY):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=30)
        except requests.RequestException as e:
            log.warning("Discord 接続失敗(%d回目): %s", attempt + 1, e)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429:  # レート制限
            try:
                retry_after = float(resp.json().get("retry_after", 1))
            except Exception:
                retry_after = 2.0
            log.warning("Discord レート制限。%.1f秒待機", retry_after)
            time.sleep(retry_after + 0.5)
            continue
        if 500 <= resp.status_code < 600:  # サーバ側一時障害
            log.warning("Discord 5xx(%d回目): %s", attempt + 1, resp.status_code)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code >= 400:  # 4xx は設定ミス等。リトライしても無駄
            log.error("Discord 送信エラー %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    log.error("Discord 送信を %d 回試みて失敗", MAX_SEND_RETRY)
    return False


def send_papers(papers: list[Paper], webhook_url: str, discord_cfg: dict,
                topic_label: str) -> list[str]:
    """papers を webhook_url のチャンネルへ送る。成功した論文のID(version_less)を返す。"""
    if not papers:
        return []
    username = discord_cfg.get("username", "arXiv Bot")
    batch_size = min(int(discord_cfg.get("embeds_per_message", 8)), 10)

    # ヘッダー（失敗しても本体送信は試みる）
    _post(webhook_url, {
        "username": username,
        "content": f"📚 **[{topic_label}]** 新着 **{len(papers)}件**",
    })
    time.sleep(0.5)

    sent_ids: list[str] = []
    for start in range(0, len(papers), batch_size):
        batch = papers[start:start + batch_size]
        ok = _post(webhook_url, {
            "username": username,
            "embeds": [_build_embed(p) for p in batch],
        })
        if ok:
            sent_ids.extend(p.version_less_id for p in batch)
            log.info("  [%s] 送信成功: %d 件", topic_label, len(batch))
        else:
            log.error("  [%s] バッチ送信失敗（次回再送）: %d 件", topic_label, len(batch))
        time.sleep(0.7)
    return sent_ids
