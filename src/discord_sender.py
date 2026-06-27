"""Discord Webhook へ論文を送信する（topicごとに宛先URLを指定）。

表示スタイル(discord.style):
  compact = 見出し＋「・[タイトル](リンク) 日付/scite」の一覧を1メッセージにまとめて送る
            （embedを使わないので件数/文字数の心配がほぼ無い。テキスト2000字でだけ自動分割）
  full    = 1論文=1カード(embed)で本文(要約 or アブスト)・著者・分野も表示

送信に成功した論文IDだけを返す（at-least-once 配信。失敗分は次回再送 = 取りこぼし防止）。
"""
from __future__ import annotations

import logging
import time

import requests

from .models import Paper

log = logging.getLogger(__name__)

ARXIV_RED = 0xB31B1B
CONTENT_LIMIT = 1900         # Discordのcontent上限2000の安全マージン
EMBED_DESC_LIMIT = 4000
EMBED_TOTAL_BUDGET = 5500    # 1メッセージ内の全embed合計（Discordは6000）
MAX_SEND_RETRY = 4


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _meta_suffix(paper: Paper) -> str:
    parts = []
    if paper.published:
        parts.append(paper.published.strftime("%Y-%m-%d"))
    if paper.scites is not None:
        parts.append(f"scite {paper.scites}")
    return ("  ｜ " + " ・ ".join(parts)) if parts else ""


def _post(webhook_url: str, payload: dict) -> bool:
    """1リクエスト送信。成功でTrue。一時的失敗はリトライ。"""
    for attempt in range(MAX_SEND_RETRY):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=30)
        except requests.RequestException as e:
            log.warning("Discord 接続失敗(%d回目): %s", attempt + 1, e)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 1))
            except Exception:
                retry_after = 2.0
            log.warning("Discord レート制限。%.1f秒待機", retry_after)
            time.sleep(retry_after + 0.5)
            continue
        if 500 <= resp.status_code < 600:
            log.warning("Discord 5xx(%d回目): %s", attempt + 1, resp.status_code)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            log.error("Discord 送信エラー %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    log.error("Discord 送信を %d 回試みて失敗", MAX_SEND_RETRY)
    return False


# ---------------- compact: テキスト一覧 ----------------
def _send_compact(papers: list[Paper], webhook_url: str, username: str,
                  topic_label: str) -> list[str]:
    header = f"📚 **[{topic_label}]** 新着 {len(papers)}件"
    # (id, 1行テキスト) を作る
    rows = []
    for p in papers:
        line = f"・ [{_truncate(p.title, 220)}]({p.abs_url}){_meta_suffix(p)}"
        rows.append((p.version_less_id, line))

    sent_ids: list[str] = []
    buf_lines = [header]
    buf_ids: list[str] = []
    buf_len = len(header)

    def flush() -> None:
        nonlocal buf_lines, buf_ids, buf_len
        if not buf_ids:  # 見出ししか無いなら送らない
            return
        if _post(webhook_url, {"username": username, "content": "\n".join(buf_lines)}):
            sent_ids.extend(buf_ids)
            log.info("  [%s] 送信成功: %d 件", topic_label, len(buf_ids))
        else:
            log.error("  [%s] 送信失敗（次回再送）: %d 件", topic_label, len(buf_ids))
        buf_lines, buf_ids, buf_len = [], [], 0
        time.sleep(0.7)

    for pid, line in rows:
        add = len(line) + 1
        if buf_ids and buf_len + add > CONTENT_LIMIT:
            flush()
        buf_lines.append(line)
        buf_ids.append(pid)
        buf_len += add
    flush()
    return sent_ids


# ---------------- full: 1論文=1カード(embed) ----------------
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


def _embed_char_count(embed: dict) -> int:
    n = len(embed.get("title", "")) + len(embed.get("description", ""))
    for f in embed.get("fields", []):
        n += len(f.get("name", "")) + len(f.get("value", ""))
    n += len(embed.get("footer", {}).get("text", ""))
    return n


def _send_full(papers: list[Paper], webhook_url: str, username: str,
               topic_label: str, max_count: int) -> list[str]:
    _post(webhook_url, {"username": username,
                        "content": f"📚 **[{topic_label}]** 新着 **{len(papers)}件**"})
    time.sleep(0.5)
    cap = min(max_count, 10)
    sent_ids: list[str] = []
    batch, ids, chars = [], [], 0
    for p in papers:
        emb = _build_embed(p)
        c = _embed_char_count(emb)
        if batch and (len(batch) >= cap or chars + c > EMBED_TOTAL_BUDGET):
            if _post(webhook_url, {"username": username, "embeds": batch}):
                sent_ids.extend(ids)
                log.info("  [%s] 送信成功: %d 件", topic_label, len(ids))
            else:
                log.error("  [%s] バッチ送信失敗（次回再送）: %d 件", topic_label, len(ids))
            batch, ids, chars = [], [], 0
            time.sleep(0.7)
        batch.append(emb); ids.append(p.version_less_id); chars += c
    if batch:
        if _post(webhook_url, {"username": username, "embeds": batch}):
            sent_ids.extend(ids)
            log.info("  [%s] 送信成功: %d 件", topic_label, len(ids))
        else:
            log.error("  [%s] バッチ送信失敗（次回再送）: %d 件", topic_label, len(ids))
    return sent_ids


def send_papers(papers: list[Paper], webhook_url: str, discord_cfg: dict,
                topic_label: str) -> list[str]:
    if not papers:
        return []
    username = discord_cfg.get("username", "arXiv Bot")
    style = discord_cfg.get("style", "compact")
    if style == "full":
        return _send_full(papers, webhook_url, username, topic_label,
                          int(discord_cfg.get("embeds_per_message", 8)))
    return _send_compact(papers, webhook_url, username, topic_label)