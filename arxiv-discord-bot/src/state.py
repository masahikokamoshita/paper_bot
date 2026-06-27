"""送信済み論文IDを state/seen.json に保存し、重複送信を防ぐ。

GitHub Actions ではこのファイルを実行後にリポジトリへコミットして永続化する。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "seen.json"
MAX_KEEP = 5000  # 肥大化防止：古いものから捨てる


def load_seen() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return set(data.get("seen_ids", []))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("seen.json 読み込み失敗（空で開始）: %s", e)
        return set()


def save_seen(seen_ids: set[str]) -> None:
    ids = list(seen_ids)
    if len(ids) > MAX_KEEP:
        ids = ids[-MAX_KEEP:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {"updated_at": datetime.now(timezone.utc).isoformat(), "seen_ids": ids},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("seen.json 保存: %d 件", len(ids))
