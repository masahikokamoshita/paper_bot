"""論文の要約(summary欄)を埋める。プロバイダを選べる。

summary.enabled: false -> API不使用、アブスト原文を掲載
summary.enabled: true  -> summary.provider で要約:
    "openai"    -> OpenAI Chat Completions（OPENAI_API_KEY）
    "anthropic" -> Claude Messages API（ANTHROPIC_API_KEY）
"""
from __future__ import annotations

import logging
import time

from .models import Paper

log = logging.getLogger(__name__)

_LANG_NAME = {"ja": "日本語", "en": "English"}

SYSTEM_PROMPT = """あなたは研究論文の要約アシスタントです。
与えられた論文のタイトルとアブストラクトを読み、{lang}で簡潔に要約してください。

出力フォーマット（マークダウン、これ以外は出力しない）:
**一言で**: （論文の主張を1文で）
**ポイント**:
- （重要点1）
- （重要点2）
- （重要点3）
**新規性/意義**: （なぜ重要か。1〜2文）

専門用語は保ちつつ、その分野に詳しくない読者にも伝わるように。誇張や憶測はしない。"""


def summarize_papers(papers: list[Paper], summary_cfg: dict) -> None:
    if not summary_cfg.get("enabled", True):
        _fill_with_abstract(papers, summary_cfg)
        return
    provider = summary_cfg.get("provider", "openai").lower()
    if provider == "anthropic":
        _summarize_with_claude(papers, summary_cfg)
    else:
        _summarize_with_openai(papers, summary_cfg)


def _fill_with_abstract(papers: list[Paper], summary_cfg: dict) -> None:
    max_chars = summary_cfg.get("abstract_max_chars")
    log.info("要約なしモード: アブスト原文を使用（API未使用）, %d 件", len(papers))
    for paper in papers:
        text = paper.abstract.strip()
        if max_chars and len(text) > int(max_chars):
            text = text[: int(max_chars) - 1] + "…"
        paper.summary = text


def _user_content(paper: Paper) -> str:
    return f"タイトル: {paper.title}\n\nアブストラクト:\n{paper.abstract}"


def _fallback(paper: Paper) -> str:
    return paper.abstract[:500] + ("…" if len(paper.abstract) > 500 else "")


# --- OpenAI ---------------------------------------------------------
def _summarize_with_openai(papers: list[Paper], summary_cfg: dict) -> None:
    from openai import OpenAI, OpenAIError

    model = summary_cfg.get("model", "gpt-4o-mini")
    lang = _LANG_NAME.get(summary_cfg.get("language", "ja"), "日本語")
    max_tokens = int(summary_cfg.get("max_tokens", 500))
    client = OpenAI()  # OPENAI_API_KEY
    log.info("要約(OpenAI): model=%s, %d 件", model, len(papers))

    for i, paper in enumerate(papers, 1):
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    max_completion_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT.format(lang=lang)},
                        {"role": "user", "content": _user_content(paper)},
                    ],
                )
                paper.summary = (resp.choices[0].message.content or "").strip()
                log.info("  [%d/%d] 要約完了: %s", i, len(papers), paper.arxiv_id)
                break
            except OpenAIError as e:
                log.warning("  要約失敗(%d回目) %s: %s", attempt + 1, paper.arxiv_id, e)
                time.sleep(2 * (attempt + 1))
        if not paper.summary:
            paper.summary = _fallback(paper)


# --- Anthropic ------------------------------------------------------
def _summarize_with_claude(papers: list[Paper], summary_cfg: dict) -> None:
    from anthropic import Anthropic, APIError

    model = summary_cfg.get("model", "claude-sonnet-4-6")
    lang = _LANG_NAME.get(summary_cfg.get("language", "ja"), "日本語")
    max_tokens = int(summary_cfg.get("max_tokens", 500))
    client = Anthropic()  # ANTHROPIC_API_KEY
    log.info("要約(Anthropic): model=%s, %d 件", model, len(papers))

    for i, paper in enumerate(papers, 1):
        for attempt in range(2):
            try:
                resp = client.messages.create(
                    model=model, max_tokens=max_tokens,
                    system=SYSTEM_PROMPT.format(lang=lang),
                    messages=[{"role": "user", "content": _user_content(paper)}],
                )
                paper.summary = "".join(b.text for b in resp.content if b.type == "text").strip()
                log.info("  [%d/%d] 要約完了: %s", i, len(papers), paper.arxiv_id)
                break
            except APIError as e:
                log.warning("  要約失敗(%d回目) %s: %s", attempt + 1, paper.arxiv_id, e)
                time.sleep(2 * (attempt + 1))
        if not paper.summary:
            paper.summary = _fallback(paper)
