"""論文の要約(summary)・タイトル和訳(title_ja)・所属(affiliations)を埋める。

【堅牢性の契約】summarize_papers() はどんな障害があっても例外を外に投げない。
    - APIキー未設定/不正、クォータ切れ、ネットワーク断、SDK内部の予期しない例外、
      PDF取得失敗など、あらゆる失敗時も関数は正常に戻る。
    - 要約を生成できなかった論文は、必ずアブストラクト（先頭500字）で summary を埋める。
      title_ja / affiliations は空のままとなり、送信側はその行を表示しないだけ。
    - 連続 _BREAKER_THRESHOLD 件失敗したら以降のAPI呼び出しを打ち切り（無駄なリトライ防止）、
      残りは即フォールバックする。
    → LLMが完全に死んでいても、配信（AI不要部分）は従来通り実行される。

summary.enabled: false -> API不使用、アブスト原文を掲載（title_ja/affiliationsは空）
summary.enabled: true  -> summary.provider で要約:
    "openai"    -> OpenAI Chat Completions（OPENAI_API_KEY）
    "anthropic" -> Claude Messages API（ANTHROPIC_API_KEY）

PDF関連（すべてbest-effort。PDF取得失敗時はタイトル＋アブストのみで要約）:
    use_fulltext: true          -> PDF本文の先頭 fulltext_max_chars 文字を要約の入力に含める
    include_affiliations: true  -> PDF1ページ目から著者の所属をLLMに抽出させる
    どちらか有効なら論文ごとにPDFを1回ダウンロードする（figure.download_pdf を再利用）。

タイトル和訳・所属は要約と同じ1回のAPI呼び出しで生成し、
出力先頭の「**タイトル和訳**:」「**所属**:」行をパースして paper に分離する。

summary.reasoning_effort: gpt-5系（reasoningモデル）のみ有効。
    "minimal" / "low" などを指定すると思考トークン消費を抑えられる。
    gpt-4o系など非reasoningモデルに設定するとAPIエラーになる（その場合も
    本モジュールの契約によりアブストフォールバックで配信は継続する）。
"""
from __future__ import annotations

import logging
import re
import time

from .models import Paper

log = logging.getLogger(__name__)

_LANG_NAME = {"ja": "日本語", "en": "English"}

# 連続でこの件数の論文の要約に失敗したら、以降のAPI呼び出しを打ち切る
_BREAKER_THRESHOLD = 3

SYSTEM_PROMPT_HEAD = """あなたは研究論文の要約アシスタントです。
与えられた論文の情報を読み、{lang}で簡潔に要約してください。

出力フォーマット（マークダウン、これ以外は出力しない）:
**タイトル和訳**: （論文タイトルの自然な{lang}訳。1行。訳注や括弧書きは付けない）
"""

# include_affiliations 時のみ差し込む行
AFFIL_FORMAT_LINE = """**所属**: （著者の主要な所属機関を筆頭著者側から最大4つ、"; "区切りで。略称があれば略称で。本文抜粋から判別できない場合は「不明」とだけ書く）
"""

SYSTEM_PROMPT_TAIL = """**一言で**: （論文の主張を1文で）
**ポイント**:
- （重要点1）
- （重要点2）
- （重要点3）
**新規性/意義**: （なぜ重要か。1〜2文）

専門用語は保ちつつ、その分野に詳しくない読者にも伝わるように。誇張や憶測はしない。"""

# 出力からメタ行を抜き出す（全角/半角コロン両対応）
_TITLE_JA_RE = re.compile(r"^\*\*タイトル和訳\*\*[:：]\s*(.+)$", re.MULTILINE)
_AFFIL_RE = re.compile(r"^\*\*所属\*\*[:：]\s*(.+)$", re.MULTILINE)


# =====================================================================
# 公開API: どんな障害でも例外を投げない
# =====================================================================
def summarize_papers(papers: list[Paper], summary_cfg: dict) -> None:
    if not summary_cfg.get("enabled", True):
        _fill_with_abstract(papers, summary_cfg)
        return
    try:
        provider = str(summary_cfg.get("provider", "openai")).lower()
        if provider == "anthropic":
            _summarize_with_claude(papers, summary_cfg)
        else:
            _summarize_with_openai(papers, summary_cfg)
    except Exception as e:  # 想定外の例外もここで必ず止める（配信を守る）
        log.error("要約処理で予期しないエラー。アブストにフォールバックして続行: %s", e)

    # 最終保証: summary が空の論文は必ずアブストで埋める
    n_fallback = 0
    for paper in papers:
        if not paper.summary.strip():
            paper.summary = _fallback(paper)
            n_fallback += 1
    if n_fallback:
        log.warning("要約できなかった %d 件はアブスト原文で配信します", n_fallback)


def _fill_with_abstract(papers: list[Paper], summary_cfg: dict) -> None:
    max_chars = summary_cfg.get("abstract_max_chars")
    log.info("要約なしモード: アブスト原文を使用（API未使用）, %d 件", len(papers))
    for paper in papers:
        text = paper.abstract.strip()
        if max_chars and len(text) > int(max_chars):
            text = text[: int(max_chars) - 1] + "…"
        paper.summary = text


def _fallback(paper: Paper) -> str:
    return paper.abstract[:500] + ("…" if len(paper.abstract) > 500 else "")


# =====================================================================
# PDF本文の取得（best-effort、失敗時は空文字）
# =====================================================================
def _pdf_context(paper: Paper, summary_cfg: dict) -> str:
    """PDF本文テキストを返す。use_fulltext時は先頭fulltext_max_chars文字、
    所属抽出のみの場合は1ページ目（firstpage_max_chars文字）。失敗時は空文字。"""
    try:
        use_full = bool(summary_cfg.get("use_fulltext", False))
        want_affil = bool(summary_cfg.get("include_affiliations", False))
        if not (use_full or want_affil):
            return ""
        import fitz  # PyMuPDF
        from .figure import download_pdf

        pdf = download_pdf(paper)
        time.sleep(float(summary_cfg.get("pdf_interval_sec", 1.0)))  # arXivへの負荷対策
        if not pdf:
            return ""
        doc = fitz.open(stream=pdf, filetype="pdf")
        if use_full:
            limit = int(summary_cfg.get("fulltext_max_chars", 20000))
            parts, total = [], 0
            for page in doc:
                t = page.get_text()
                parts.append(t)
                total += len(t)
                if total >= limit:
                    break
            return "".join(parts)[:limit].strip()
        limit = int(summary_cfg.get("firstpage_max_chars", 4000))
        return doc[0].get_text()[:limit].strip()
    except Exception as e:
        log.warning("PDFテキスト取得失敗（アブストのみで要約継続） %s: %s", paper.arxiv_id, e)
        return ""


# =====================================================================
# プロンプト構築と結果パース
# =====================================================================
def _system_prompt(lang: str, with_affil: bool) -> str:
    head = SYSTEM_PROMPT_HEAD.format(lang=lang)
    return head + (AFFIL_FORMAT_LINE if with_affil else "") + SYSTEM_PROMPT_TAIL


def _user_content(paper: Paper, body_text: str = "") -> str:
    s = f"タイトル: {paper.title}\n\nアブストラクト:\n{paper.abstract}"
    if body_text:
        s += f"\n\n本文抜粋（PDF冒頭）:\n{body_text}"
    return s


def _apply_result(paper: Paper, raw_text: str) -> None:
    """LLM出力からタイトル和訳・所属の行を分離して paper に格納する。"""
    text = raw_text.strip()
    m = _TITLE_JA_RE.search(text)
    if m:
        paper.title_ja = m.group(1).strip()
        text = (text[: m.start()] + text[m.end():]).strip()
    m = _AFFIL_RE.search(text)
    if m:
        affil = m.group(1).strip()
        paper.affiliations = "" if affil in ("不明", "NA", "N/A") else affil
        text = (text[: m.start()] + text[m.end():]).strip()
    paper.summary = text


# =====================================================================
# OpenAI
# =====================================================================
def _summarize_with_openai(papers: list[Paper], summary_cfg: dict) -> None:
    # クライアント初期化の失敗（キー未設定/空、パッケージ問題など）は
    # ここで捕捉して即フォールバック（従来はここで全体が落ちていた）
    try:
        from openai import OpenAI
        client = OpenAI()  # OPENAI_API_KEY
    except Exception as e:
        log.error("OpenAIクライアント初期化失敗。全件アブストで配信します: %s", e)
        return

    model = summary_cfg.get("model", "gpt-5-mini")
    lang = _LANG_NAME.get(summary_cfg.get("language", "ja"), "日本語")
    max_tokens = int(summary_cfg.get("max_tokens", 2000))
    log.info("要約(OpenAI): model=%s, %d 件", model, len(papers))

    # gpt-5系reasoningモデル向けオプション（設定時のみ渡す）
    extra_kwargs: dict = {}
    effort = summary_cfg.get("reasoning_effort")
    if effort:
        extra_kwargs["reasoning_effort"] = str(effort)

    consecutive_failures = 0
    for i, paper in enumerate(papers, 1):
        if consecutive_failures >= _BREAKER_THRESHOLD:
            log.error("APIエラーが %d 件連続したため要約を打ち切り。残り %d 件はアブストで配信",
                      _BREAKER_THRESHOLD, len(papers) - i + 1)
            return
        body = _pdf_context(paper, summary_cfg)
        with_affil = bool(summary_cfg.get("include_affiliations", False)) and bool(body)
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    max_completion_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": _system_prompt(lang, with_affil)},
                        {"role": "user", "content": _user_content(paper, body)},
                    ],
                    **extra_kwargs,
                )
                _apply_result(paper, resp.choices[0].message.content or "")
                if paper.summary.strip():
                    log.info("  [%d/%d] 要約完了: %s", i, len(papers), paper.arxiv_id)
                    break
                log.warning("  応答が空(%d回目) %s", attempt + 1, paper.arxiv_id)
            except Exception as e:  # SDK例外に限らず全て捕捉（配信を守る）
                log.warning("  要約失敗(%d回目) %s: %s", attempt + 1, paper.arxiv_id, e)
                time.sleep(2 * (attempt + 1))
        if paper.summary.strip():
            consecutive_failures = 0
        else:
            consecutive_failures += 1


# =====================================================================
# Anthropic
# =====================================================================
def _summarize_with_claude(papers: list[Paper], summary_cfg: dict) -> None:
    try:
        from anthropic import Anthropic
        client = Anthropic()  # ANTHROPIC_API_KEY
    except Exception as e:
        log.error("Anthropicクライアント初期化失敗。全件アブストで配信します: %s", e)
        return

    model = summary_cfg.get("model", "claude-sonnet-4-6")
    lang = _LANG_NAME.get(summary_cfg.get("language", "ja"), "日本語")
    max_tokens = int(summary_cfg.get("max_tokens", 2000))
    log.info("要約(Anthropic): model=%s, %d 件", model, len(papers))

    consecutive_failures = 0
    for i, paper in enumerate(papers, 1):
        if consecutive_failures >= _BREAKER_THRESHOLD:
            log.error("APIエラーが %d 件連続したため要約を打ち切り。残り %d 件はアブストで配信",
                      _BREAKER_THRESHOLD, len(papers) - i + 1)
            return
        body = _pdf_context(paper, summary_cfg)
        with_affil = bool(summary_cfg.get("include_affiliations", False)) and bool(body)
        for attempt in range(2):
            try:
                resp = client.messages.create(
                    model=model, max_tokens=max_tokens,
                    system=_system_prompt(lang, with_affil),
                    messages=[{"role": "user", "content": _user_content(paper, body)}],
                )
                raw = "".join(b.text for b in resp.content if b.type == "text")
                _apply_result(paper, raw)
                if paper.summary.strip():
                    log.info("  [%d/%d] 要約完了: %s", i, len(papers), paper.arxiv_id)
                    break
                log.warning("  応答が空(%d回目) %s", attempt + 1, paper.arxiv_id)
            except Exception as e:  # SDK例外に限らず全て捕捉（配信を守る）
                log.warning("  要約失敗(%d回目) %s: %s", attempt + 1, paper.arxiv_id, e)
                time.sleep(2 * (attempt + 1))
        if paper.summary.strip():
            consecutive_failures = 0
        else:
            consecutive_failures += 1
