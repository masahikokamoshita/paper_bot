"""Slack へ論文を送信する（slack_sdk）。

親メッセージ = タイトル(リンク)＋メタ ＋ 和訳タイトル ＋ 著者 ＋ 所属
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


HEAD_AUTHORS = 5  # 先頭から表示する著者数（筆頭側）
TAIL_AUTHORS = 5  # 末尾から表示する著者数（責任著者側）

# 姓の一部として扱う小文字の前置詞（省略しない）: van der Waals, de la Cruz など
_NAME_PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da",
                   "la", "le", "los", "las", "ter", "ten", "op", "af", "zu", "dos", "du"}


def _abbrev_author(name: str) -> str:
    """'Jin Yang' -> 'J. Yang' のように名をイニシャル化する（姓は残す）。
    - 'Jean-Pierre Dupont' -> 'J.-P. Dupont'
    - 'Juan de la Cruz'    -> 'J. de la Cruz'（小文字前置詞から後ろは姓扱い）
    - 単一語名・Collaboration名などはそのまま。"""
    tokens = name.split()
    if len(tokens) < 2:
        return name
    low = name.lower()
    if "collaboration" in low or "consortium" in low or " team" in low:
        return name
    # 姓の開始位置: 末尾トークン、ただしその前に小文字前置詞が続く場合はそこまで含める
    family_start = len(tokens) - 1
    while family_start > 1 and tokens[family_start - 1].lower() in _NAME_PARTICLES:
        family_start -= 1
    given = tokens[:family_start]
    if not given:
        return name

    def initial(tok: str) -> str:
        # 'Jean-Pierre' -> 'J.-P.', 'A.' -> 'A.'。小文字前置詞はそのまま残す
        if tok.lower() in _NAME_PARTICLES:
            return tok
        parts = [p for p in tok.split("-") if p]
        return "-".join(p[0].upper() + "." for p in parts)

    return " ".join(initial(t) for t in given) + " " + " ".join(tokens[family_start:])


def _authors_line(paper: Paper) -> str:
    """著者を「先頭5名 …(中略N名)… 末尾5名」で表示する。
    筆頭著者と、末尾に来ることが多い責任著者の両方を必ず残す。
    合計10名以下なら全員表示。"""
    a = [_abbrev_author(n) for n in paper.authors]
    if not a:
        return ""
    if len(a) <= HEAD_AUTHORS + TAIL_AUTHORS:
        return ", ".join(a)
    omitted = len(a) - HEAD_AUTHORS - TAIL_AUTHORS
    return (", ".join(a[:HEAD_AUTHORS])
            + f" …(中略{omitted}名)… "
            + ", ".join(a[-TAIL_AUTHORS:]))


def _parent_text(paper: Paper, topic_label: str) -> str:
    """親メッセージ本文: タイトル(リンク)＋メタ / 和訳タイトル / 著者 / 所属"""
    lines = [f"<{paper.abs_url}|{paper.title}>{_meta(paper, topic_label)}"]
    if paper.title_ja:
        lines.append(f"_{paper.title_ja}_")
    authors = _authors_line(paper)
    if authors:
        lines.append(f"👤 {authors}")
    if paper.affiliations:
        lines.append(f"🏛 {paper.affiliations}")
    return "\n".join(lines)


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
        text = _parent_text(p, topic_label)
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
