"""論文1件を表すデータモデル。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Paper:
    arxiv_id: str                      # バージョンなしのID（例: 2509.01615）
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published: Optional[datetime]      # 投稿日時(UTC)
    abs_url: str                       # arXiv abstractページ
    pdf_url: str                       # PDF直リンク
    matched_by: str = ""               # 何でヒットしたか（topic名など）
    scites: Optional[int] = None       # Scirate の scite 数（取れた場合）
    summary: str = field(default="")   # Claude による要約
    # topic名 -> そのtopic内でヒットしたキーワード群
    matched_keywords: dict[str, list[str]] = field(default_factory=dict)
    # 高scite論文向け: 結果の日本語解説＋代表図（runtime専用。stateには保存しない）
    result_explanation: str = ""
    figure_png: Optional[bytes] = None
    figure_mime: str = "image/png"

    @property
    def version_less_id(self) -> str:
        return self.arxiv_id.split("v")[0]
