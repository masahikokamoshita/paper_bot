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
    matched_by: str = ""               # 何でヒットしたか（keyword/author/trending）
    scites: Optional[int] = None       # Scirate の scite 数（取れた場合）
    summary: str = field(default="")   # Claude による要約

    @property
    def version_less_id(self) -> str:
        return self.arxiv_id.split("v")[0]
