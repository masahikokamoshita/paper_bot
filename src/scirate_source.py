"""Scirate から scite 数（注目度）を取得する。

Scirate には公式APIが無いため、カテゴリページの HTML を解析して
{arxiv_id: scite数} のマップを作る。サイトのHTML構造は変わりうるので、
複数の手段で頑張って抽出し、ダメなら空を返して（=印を付けないだけで）
bot 全体は arXiv 単体で正常動作するようにしてある。

構造が変わって scite が取れなくなった場合は、_extract_pairs() を
実際のページHTMLに合わせて調整すればよい（README参照）。
"""
from __future__ import annotations

import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from .models import Paper

log = logging.getLogger(__name__)

BASE = "https://scirate.com/arxiv/{category}"
USER_AGENT = "arxiv-discord-bot/1.0 (research use)"
ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})")


def _extract_pairs(html: str) -> dict[str, int]:
    """HTMLから {arxiv_id(バージョンなし): scite数} を抽出する。

    Scirate の各論文ブロックには arXiv へのリンクと scite 数がある。
    リンク要素を起点に、ブロック内/近傍の数値を scite 数として拾う。
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs: dict[str, int] = {}

    # arXiv ID を含むリンクを全部探す（/arxiv/xxxx や arxiv.org/abs/xxxx）
    for a in soup.find_all("a", href=True):
        m = ARXIV_ID_RE.search(a["href"])
        if not m:
            continue
        arxiv_id = m.group(1)
        if arxiv_id in pairs:
            continue

        # この論文ブロック（親要素）を遡って探し、その中の scite 数を探す
        container = a
        scites = None
        for _ in range(5):  # 最大5階層さかのぼる
            container = container.parent
            if container is None:
                break
            # scite らしき要素を探す（class名に scite を含む / "Scite" 近傍の数値）
            scite_el = container.find(
                class_=lambda c: c and "scite" in c.lower()
            )
            if scite_el:
                num = re.search(r"\d+", scite_el.get_text(" ", strip=True))
                if num:
                    scites = int(num.group())
                    break
        if scites is not None:
            pairs[arxiv_id] = scites

    return pairs


def fetch_scite_map(categories: list[str], interval: float = 3.0) -> dict[str, int]:
    """指定カテゴリの scite マップをまとめて返す。失敗しても例外は投げない。"""
    result: dict[str, int] = {}
    for cat in categories:
        url = BASE.format(category=cat)
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            pairs = _extract_pairs(resp.text)
            log.info("Scirate %s: %d 件の scite 数を取得", cat, len(pairs))
            for k, v in pairs.items():
                result[k] = max(result.get(k, 0), v)
        except requests.RequestException as e:
            log.warning("Scirate %s の取得に失敗（スキップ）: %s", cat, e)
        except Exception as e:  # 解析失敗も致命傷にしない
            log.warning("Scirate %s の解析に失敗（スキップ）: %s", cat, e)
        finally:
            time.sleep(interval)
    return result


def annotate(papers: list[Paper], scite_map: dict[str, int]) -> None:
    """取得済み論文に scite 数を付与する（破壊的）。"""
    for p in papers:
        if p.version_less_id in scite_map:
            p.scites = scite_map[p.version_less_id]
