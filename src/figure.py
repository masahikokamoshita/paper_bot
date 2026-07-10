"""高scite論文の「代表図1枚」＋「結果の日本語解説(〜1000字)」を作る。

- 図抽出: arXiv PDF を取得し、PyMuPDF で最も図らしいページ/画像を1枚選ぶ。
  1) 埋め込みラスタ画像が十分大きければそれを採用
  2) 無ければ、ベクター描画(get_drawings)が多いページを画像化（quant-ph等はベクター図が多い）
- 解説: OpenAI のビジョンで、抽出図＋本文抜粋を渡し、結果を日本語で解説させる。

すべて best-effort。失敗しても None を返すだけで、本体（テキスト配信）は止めない。
"""
from __future__ import annotations

import base64
import io
import logging

import fitz  # PyMuPDF
import requests

from .models import Paper

log = logging.getLogger(__name__)

USER_AGENT = "arxiv-discord-bot/1.0 (research use)"
MIN_IMG_SIDE = 200          # これ未満の画像はロゴ/アイコン扱いで無視
RENDER_DPI = 150


def download_pdf(paper: Paper, timeout: int = 60) -> bytes | None:
    url = paper.pdf_url or f"https://arxiv.org/pdf/{paper.version_less_id}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            log.warning("PDFではない応答: %s", paper.arxiv_id)
            return None
        return resp.content
    except requests.RequestException as e:
        log.warning("PDF取得失敗 %s: %s", paper.arxiv_id, e)
        return None


def extract_representative_figure(pdf_bytes: bytes) -> tuple[bytes, str] | None:
    """代表図を1枚選んでPNGバイト列で返す。取れなければ None。"""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        log.warning("PDFを開けません: %s", e)
        return None

    best = None  # (score, page_no, largest_xref, largest_area)
    for pno in range(len(doc)):
        page = doc[pno]
        largest_xref, largest_area, img_area = None, 0, 0
        for img in page.get_images(full=True):
            xref, w, h = img[0], img[2], img[3]
            area = w * h
            if min(w, h) < MIN_IMG_SIDE:
                continue
            img_area += area
            if area > largest_area:
                largest_area, largest_xref = area, xref
        try:
            n_draw = len(page.get_drawings())
        except Exception:
            n_draw = 0
        # 埋め込み画像の面積を主、ベクター描画数は頭打ちで従に評価
        score = img_area / 5e4 + min(n_draw, 80) * 0.08
        if pno == 0:
            score *= 0.4  # タイトルページは優先度を下げる
        if best is None or score > best[0]:
            best = (score, pno, largest_xref, largest_area)

    if best is None or best[0] <= 0:
        doc.close()
        return None

    _, pno, largest_xref, largest_area = best
    try:
        if largest_xref and largest_area >= MIN_IMG_SIDE * MIN_IMG_SIDE:
            # 埋め込みラスタ画像をそのまま取り出す
            pix = fitz.Pixmap(doc, largest_xref)
            if pix.n > 4:  # CMYK等 -> RGB
                pix = fitz.Pixmap(fitz.csRGB, pix)
            png = pix.tobytes("png")
        else:
            # ベクター図が多いページを丸ごと画像化
            png = doc[pno].get_pixmap(dpi=RENDER_DPI).tobytes("png")
    except Exception as e:
        log.warning("図の画像化に失敗: %s", e)
        doc.close()
        return None

    doc.close()
    return png, "image/png"


def _pdf_text(pdf_bytes: bytes, limit: int = 8000) -> str:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text[:limit]
    except Exception:
        return ""


def explain_result(paper: Paper, pdf_bytes: bytes, figure_png: bytes | None,
                   model: str, max_chars: int) -> str:
    """OpenAIのビジョンで結果を日本語解説（〜max_chars字）。失敗時は空文字。"""
    from openai import OpenAI, OpenAIError

    body_text = _pdf_text(pdf_bytes)
    prompt = (
        f"次の論文の『主要な結果』を、日本語で{max_chars}字以内で解説してください。\n"
        f"添付図がある場合はその図が何を示しているかにも触れてください。\n"
        f"研究者向けに、何がどれだけ改善/実証されたのか具体的に。誇張や憶測はしない。\n\n"
        f"タイトル: {paper.title}\n\n"
        f"アブストラクト:\n{paper.abstract}\n\n"
        f"本文抜粋:\n{body_text}"
    )
    content: list = [{"type": "text", "text": prompt}]
    if figure_png:
        b64 = base64.b64encode(figure_png).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    try:
        client = OpenAI()  # OPENAI_API_KEY を環境変数から読む
        resp = client.chat.completions.create(
            model=model,
            max_completion_tokens=max(int(max_chars * 1.4), 700),
            messages=[{"role": "user", "content": content}],
        )
        return (resp.choices[0].message.content or "").strip()
    except OpenAIError as e:
        log.warning("結果解説の生成に失敗 %s: %s", paper.arxiv_id, e)
        return ""


def build_for_papers(papers: list[Paper], figure_cfg: dict) -> None:
    """高scite論文に result_explanation と figure_png を付与する（破壊的・best-effort）。"""
    model = figure_cfg.get("model", "gpt-4o-mini")
    max_chars = int(figure_cfg.get("explain_max_chars", 1000))
    min_scites = int(figure_cfg.get("min_scites", 20))
    cap = int(figure_cfg.get("max_papers_per_run", 3))

    targets = [p for p in papers if (p.scites or 0) >= min_scites]
    targets.sort(key=lambda p: (p.scites or 0), reverse=True)
    targets = targets[:cap]
    if not targets:
        log.info("図解対象なし（min_scites=%d 以上が無い）", min_scites)
        return
    log.info("図解対象 %d 件（model=%s）", len(targets), model)

    for p in targets:
        pdf = download_pdf(p)
        if not pdf:
            continue
        fig = extract_representative_figure(pdf)
        if fig:
            p.figure_png, p.figure_mime = fig[0], fig[1]
        explanation = explain_result(p, pdf, p.figure_png, model, max_chars)
        if explanation:
            p.result_explanation = explanation
            log.info("  図解生成: %s（図%s）", p.arxiv_id, "あり" if fig else "なし")
