"""Slack へ論文を送信する（slack_sdk）。

親メッセージ = タイトル(リンク)＋メタ
返信1        = 日本語要約（summary_in_thread）
返信2        = 結果解説＋代表図（高scite論文で生成されている場合のみ）

スレッド返信/ファイル添付のため Bot Token(xoxb-)＋Web API を使う。
Bot を対象チャンネルに招待しておくこと。成功した論文ID(親投稿できたもの)を返す。
"""
from __future__ import annotations

import io
import logging
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from .models import Paper

log = logging.getLogger(__name__)


def _to_mrkdwn(text: str) -> str:
    return text.replace("**", "*")  # Claude/OpenAIの**bold**をSlackの*bold*へ


def _meta(paper: Paper, topic_label: str) -> str:
    parts = []
    if paper.published:
        parts.append(paper.published.strftime("%Y-%m-%d"))
    if paper.scites is not None:
        parts.append(f"scite {paper.scites}")
    kws = paper.matched_keywords.get(topic_label, [])
    if kws:
        parts.append("kw: " + ", ".join(kws))
    return ("  |  " + "  ・  ".join(parts)) if parts else ""


def _client(token: str) -> WebClient:
    c = WebClient(token=token)
    c.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    return c


def send_papers(papers: list[Paper], token: str, channel: str, _unused,
                topic_label: str, summary_enabled: bool, summary_in_thread: bool) -> list[str]:
    if not papers:
        return []
    client = _client(token)

    try:
        client.chat_postMessage(channel=channel,
                                text=f"📚 *[{topic_label}]* 新着 {len(papers)}件",
                                unfurl_links=False, unfurl_media=False)
    except SlackApiError as e:
        log.warning("[%s/Slack] ヘッダー投稿失敗: %s", topic_label, e.response.get("error"))
    time.sleep(0.4)

    sent_ids: list[str] = []
    for p in papers:
        text = f"<{p.abs_url}|{p.title}>{_meta(p, topic_label)}"
        try:
            resp = client.chat_postMessage(channel=channel, text=text,
                                           unfurl_links=False, unfurl_media=False)
            ts = resp["ts"]
        except SlackApiError as e:
            log.error("  [%s/Slack] 送信失敗（次回再送）%s: %s",
                      topic_label, p.arxiv_id, e.response.get("error"))
            continue
        sent_ids.append(p.version_less_id)

        # 返信1: 日本語要約
        if summary_enabled and summary_in_thread and p.summary.strip():
            try:
                client.chat_postMessage(channel=channel, thread_ts=ts,
                                        text=_to_mrkdwn(p.summary.strip()))
            except SlackApiError as e:
                log.warning("  [%s/Slack] 要約返信失敗: %s", topic_label, e.response.get("error"))
            time.sleep(0.3)

        # 返信2: 結果解説＋代表図（あれば）
        if p.result_explanation or p.figure_png:
            caption = _to_mrkdwn(p.result_explanation.strip()) if p.result_explanation else "（結果図）"
            try:
                if p.figure_png:
                    client.files_upload_v2(channel=channel, thread_ts=ts,
                                           file=io.BytesIO(p.figure_png),
                                           filename=f"{p.version_less_id}.png",
                                           initial_comment=caption)
                else:
                    client.chat_postMessage(channel=channel, thread_ts=ts, text=caption)
            except SlackApiError as e:
                log.warning("  [%s/Slack] 図解返信失敗: %s", topic_label, e.response.get("error"))
            time.sleep(0.4)

        time.sleep(0.3)

    log.info("  [%s/Slack] 送信成功: %d 件", topic_label, len(sent_ids))
    return sent_ids
