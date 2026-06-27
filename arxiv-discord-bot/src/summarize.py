"""Claude API（Messages API）で論文アブストラクトを要約する。

ANTHROPIC_API_KEY 環境変数を SDK が自動で読む。
"""
from __future__ import annotations

import logging
import time

from anthropic import Anthropic, APIError

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


def _summarize_one(client: Anthropic, paper: Paper, model: str,
                   lang: str, max_tokens: int) -> str:
    lang_name = _LANG_NAME.get(lang, "日本語")
    user_content = (
        f"タイトル: {paper.title}\n\n"
        f"アブストラクト:\n{paper.abstract}"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT.format(lang=lang_name),
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def summarize_papers(papers: list[Paper], summary_cfg: dict) -> None:
    """各論文に要約を付与する（破壊的）。失敗した論文はアブスト先頭で代替。"""
    model = summary_cfg.get("model", "claude-sonnet-4-6")
    lang = summary_cfg.get("language", "ja")
    max_tokens = int(summary_cfg.get("max_tokens", 500))

    client = Anthropic()  # ANTHROPIC_API_KEY を環境変数から読む
    log.info("要約開始: model=%s, %d 件", model, len(papers))

    for i, paper in enumerate(papers, 1):
        for attempt in range(2):  # 1回だけリトライ
            try:
                paper.summary = _summarize_one(client, paper, model, lang, max_tokens)
                log.info("  [%d/%d] 要約完了: %s", i, len(papers), paper.arxiv_id)
                break
            except APIError as e:
                log.warning("  要約失敗(%d回目) %s: %s", attempt + 1, paper.arxiv_id, e)
                time.sleep(2 * (attempt + 1))
        if not paper.summary:
            # 失敗時はアブストの先頭を要約代わりに
            paper.summary = paper.abstract[:500] + ("…" if len(paper.abstract) > 500 else "")
