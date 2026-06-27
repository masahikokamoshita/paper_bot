"""送信済み論文IDを topic ごとに記録し、重複送信を防ぐ。

複数チャンネル運用のため、seen は {topic名: set(id)} 構造。
これにより「topic Aには送ったが topic Bにはまだ」を正しく扱える
（=同じ論文を複数チャンネルに送ってよいが、同じチャンネルへの二重送信は防ぐ）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "seen.json"
MAX_KEEP_PER_TOPIC = 3000  # topごとの保持上限（肥大化防止）


def load_seen() -> dict[str, set[str]]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        topics = data.get("topics", {})
        return {name: set(ids) for name, ids in topics.items()}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("seen.json 読み込み失敗（空で開始）: %s", e)
        return {}


def save_seen(seen: dict[str, set[str]]) -> None:
    topics = {}
    for name, ids in seen.items():
        ids_list = list(ids)
        if len(ids_list) > MAX_KEEP_PER_TOPIC:
            ids_list = ids_list[-MAX_KEEP_PER_TOPIC:]
        topics[name] = ids_list
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {"updated_at": datetime.now(timezone.utc).isoformat(), "topics": topics},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    total = sum(len(v) for v in topics.values())
    log.info("seen.json 保存: %d topic / 合計 %d 件", len(topics), total)
