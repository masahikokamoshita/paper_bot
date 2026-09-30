"""リソース推定パイプライン (Phase A)。

「リソース推定」topicにマッチした論文を対象に:
  1. 判定LLM: ①新規性がアルゴリズムか ②FTQCスコープか ③具体的数値見積もり報告があるか
  2. 全部Yesなら抽出LLM: moonshot-website の rows.json スキーマ（columns）を実行時取得して
     スキーマ同期し、条件ごとにレコード案を生成
  3. auto_pr有効時: フォーク経由で moonshot-website 本家へ draft PR を自動作成し、
     SlackにPRリンクを投稿（Phase B）。PR作成に失敗した場合は
     rows.json追記用JSONをSlackスレッドに投稿するフォールバック（Phase A動作）。

【PR方式】rows.jsonは1MB超のためGit Data API（blob→tree→commit→ref）を使用。
  既存内容には一切触れず records 配列末尾への挿入のみ（diffが追記行だけになる）。
  ブランチは upstream main の最新コミットから直接作るため、フォークの同期は不要。

【堅牢性の契約】process_papers() はどんな障害があっても例外を外に投げない。
本体配信（main.py の通常フロー）には一切影響しない。

【データ方針】(moonshot-website AGENTS.md 準拠)
  - 換算・計算は一切しない。論文の生の報告値をそのままの単位で該当列に転記
  - 数値には [値](https://arxiv.org/pdf/ID#page=N) 形式のページアンカー根拠を必須化
  - ページを特定できない値は NA
  - 処理済み論文は state/resource_est_done.json に記録して再処理を防ぐ
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests

from .models import Paper

log = logging.getLogger(__name__)

# =====================================================================
# pr_min_status の選択肢。値(数値)は「低い→高い」の順位で、DEFAULTS/config.yamlの
# pr_min_status には、このキー（"plottable" / "partial" / "no_numbers"）のいずれかを書く。
# "no_numbers" は選んでも常にPR対象外になる（数値が一切ないレコードをPRする意味がないため、
# _process_one 側で明示的にブロックしている）。
# =====================================================================
STATUS_RANK = {
    "plottable": 2,    # プロット可（横軸=論理量子ビット数・縦軸=ゲート数系が揃っている）
    "partial": 1,      # 一部欠け（数値はあるがプロット必須列が揃っていない）
    "no_numbers": 0,   # 数値なし（判定は通過したが抽出できる数値がゼロ）
}

# =====================================================================
# 【ここが設定の一等地】人間が触るのは基本ここだけでいい。
# config.yaml の resource_estimation: ブロックで同名キーを指定すれば上書きされる
# （config.yaml 側の値が優先。ここはconfig未指定時のデフォルト＝いわば工場出荷設定）。
# コード側は再読み込みするだけで、この辞書の外に同じ意味の数値をハードコードしない。
# =====================================================================
DEFAULTS = {
    # --- 有効/無効・投稿先 ---
    "enabled": {0: False, 1: True}[1],
    "topic_name": "リソース推定",
    "slack_channel_env": "SLACK_CHANNEL_RESOURCE_EST",

    # --- LLM ---
    # モデルIDは OpenAI のモデル一覧ページで最新を確認して追記する
    "model": {0: "gpt-5-mini", 1: "gpt-5", 2: "gpt-5-nano"}[0],
    # 0=渡さない（gpt-4o系など非reasoningモデル用） / 1〜4=思考量（少→多）
    "reasoning_effort": {0: None, 1: "minimal", 2: "low", 3: "medium", 4: "high"}[2],
    "judge_max_chars": 30000,       # 判定LLMに渡すPDF本文の最大文字数
    "extract_max_chars": 60000,     # 抽出LLMに渡すPDF本文の最大文字数
    "judge_max_tokens": 2000,
    "extract_max_tokens": 8000,
    "max_records_per_paper": 10,    # 1論文から抽出するレコード数の上限
    "notify_rejected": {0: False, 1: True}[1],        # 判定NGの論文もSlackに「⏭️判定除外」で通知するか

    # --- PR自動作成 (Phase B) ---
    "auto_pr": {0: False, 1: True}[0],               # true でPR自動作成。false ならSlackにJSON投稿のみ
    # 0=プロット可の論文だけPR / 1=一部欠けでもPR(既定) / 2=常にPR対象外 → 数字だけ変えればOK
    "pr_min_status": {0: "plottable", 1: "partial", 2: "no_numbers"}[1],
    "github_token_env": "MOONSHOT_PAT",
    "upstream_owner": "kosukemtr",
    "upstream_repo": "moonshot-website",
    "fork_owner": "masahikokamoshita",
    "base_branch": "main",
    "rows_path": "pages/quantum-resource-estimates/data/resource_estimates_rows.json",
    "schema_url": ("https://raw.githubusercontent.com/kosukemtr/moonshot-website/main/"
                   "pages/quantum-resource-estimates/data/resource_estimates_rows.json"),
}


def _cfg(re_cfg: dict, key: str):
    """re_cfg（config.yamlのresource_estimation:）→ 無ければDEFAULTS、の順で値を取る。
    新しい設定項目を足すときは DEFAULTS に1行足すだけでいい。"""
    return re_cfg.get(key, DEFAULTS[key])


# グラフの縦軸・横軸になり得る列（列名に部分一致するかで判定。rows.json側の列名が
# 変わった場合はここを直す）
X_AXIS_KEYS = ["論理量子ビット"]
Y_AXIS_KEYS = ["Toffoli数", "Tゲート数", "その他論理ゲート数"]

_STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "resource_est_done.json"
_SLACK_CHUNK = 3600  # Slack 1メッセージの安全な最大文字数（コードブロック込み）
_GH_API = "https://api.github.com"

# --- 任意依存パッケージ（未インストールでも import エラーでbot全体を落とさない）---
# 実際に使う関数の中で「Noneなら機能を諦める」を1回書くだけにするための遅延読み込み。
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
try:
    from slack_sdk import WebClient
    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
except ImportError:
    WebClient = None
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


JUDGE_SYSTEM = """あなたはFTQC（誤り耐性量子計算）のリソース見積もりデータベースのキュレーション補助AIです。
与えられた論文が以下の3条件をすべて満たすか判定し、JSONのみを出力してください。

条件1 is_algorithm_paper: 論文の主な新規性が量子アルゴリズム（の改良・解析・リソース見積もり）にあるか。
  ハードウェア実験のみ・誤り訂正符号理論のみ・レビュー/サーベイは false。
条件2 in_ftqc_scope: 対象アルゴリズムがFTQCスコープに入るか。
  含める: QPE、qubitization、Trotter/積公式、QSVT、Grover/振幅増幅系、Shor系、
          および振幅増幅を組み込んだハイブリッド（SQD-AA、QSCI-CMP など）
  除外: VQE/VQD/QAOA等の変分法、素のQSCI/SQD（サンプリングのみ）、変分QML
  迷う場合は「振幅増幅・QPE等のFT前提サブルーチンを回路に含むか」で判定する。
条件3 has_concrete_estimates: 具体的な問題インスタンス（分子、鍵長、格子サイズ等）に対する
  量子ビット数・ゲート数（T数/Toffoli数等）・実行時間などの数値見積もりの報告があるか。
  相対削減率（%）のみの報告は false。

出力形式（JSONのみ、説明文なし）:
{"is_algorithm_paper": true/false, "in_ftqc_scope": true/false,
 "has_concrete_estimates": true/false, "reason": "判定理由を日本語で1〜2文"}"""

EXTRACT_SYSTEM = """あなたはFTQCリソース見積もりデータベースへの転記AIです。
論文本文から、データベースのレコード案をJSONで出力してください。

スキーマ（列名の完全な一覧。レコードの各キーはこの列名を一字一句そのまま使うこと）:
{columns}

記入例（実際にデータベースに収録されているレコード）:
{example}

厳守ルール:
1. 換算・計算は一切禁止。論文が報告した生の値を、そのままの単位で該当列に転記する。
   例: T数のみ報告ならTゲート数列に記入しToffoli数列はNA。T/4換算やCCZ→Toffoli換算はしない。
   数値は 3.2e11 のような指数表記の文字列で記入する。
2. 数値を記入した場合、「数値根拠」列に必ず [値]({pdf_url}#page=N) 形式の
   ページアンカー付きリンクを含める（Nは本文のPDFページ番号。本文には [PAGE N] マーカーがある）。
   ページを特定できない値はその列を "NA" にする。推測でページ番号を書かない。
3. 論文中の条件（分子・サイズ・精度・手法バリアント）ごとに1レコード。最大{max_records}レコード。
   それ以上ある場合は代表的な条件を選び、notesに「他にも条件あり」と書く。
4. end-to-endでないサブルーチン単体の見積もりは「見積もりの種類」列にその旨を明記する。
5. 不明な列はすべて "NA"。推測で埋めない。
6. FTQC見積もり（実験でない）の場合、「実験実施」列は "0"。
7. 論文が古典計算ベースライン（時間・手法）を報告している場合のみ、古典計算系の列に転記する。

出力形式（JSONのみ、説明文なし）:
{{"records": [{{列名: 値, ...}}, ...],
  "notes": "抽出時の注意点・欠損の説明を日本語で"}}"""


# =====================================================================
# 公開API: どんな障害でも例外を投げない
# =====================================================================
def process_papers(papers: list[Paper], cfg: dict, slack_token: str) -> None:
    try:
        _process(papers, cfg, slack_token)
    except Exception as e:
        log.error("リソース推定処理で予期しないエラー（本体配信には影響なし）: %s", e)


def _process(papers: list[Paper], cfg: dict, slack_token: str) -> None:
    re_cfg = cfg.get("resource_estimation", {})
    if not _cfg(re_cfg, "enabled"):
        return
    topic_name = _cfg(re_cfg, "topic_name")
    targets = [p for p in papers if topic_name in p.matched_keywords]
    if not targets:
        return

    channel = os.environ.get(_cfg(re_cfg, "slack_channel_env"), "")
    if not slack_token or not channel:
        log.warning("[リソース推定] Slack token/channel 未設定のためスキップ"
                    "（Secret %s を確認）", _cfg(re_cfg, "slack_channel_env"))
        return

    run_pipeline(targets, re_cfg, slack_token, channel)


def run_pipeline(papers: list[Paper], re_cfg: dict, slack_token: str, channel: str,
                 header: str | None = None) -> int:
    """論文リストを（topicフィルタなしで）判定→抽出→Slack/PR に流す。
    通常運用（_process）とバックフィル（backfill.py）の共通入口。
    処理済み（done記録）は自動でスキップ。処理した件数を返す。例外は投げない。"""
    done = _load_done()
    targets = [p for p in papers if p.version_less_id not in done]
    if not targets:
        log.info("[リソース推定] 新規対象なし")
        return 0
    log.info("[リソース推定] 対象 %d 件", len(targets))

    # LLMクライアント（未インストール/初期化失敗＝全件スキップ、次回再試行）
    if OpenAI is None:
        log.error("[リソース推定] openai パッケージ未インストールのためスキップ")
        return 0
    try:
        client = OpenAI()
    except Exception as e:
        log.error("[リソース推定] OpenAIクライアント初期化失敗: %s", e)
        return 0

    # スキーマ取得（失敗したら今回はスキップして次回再試行）
    columns, example = _fetch_schema(_cfg(re_cfg, "schema_url"))
    if not columns:
        log.error("[リソース推定] スキーマ取得失敗のため今回はスキップ")
        return 0

    slack = _slack_client(slack_token)
    if header:
        _post(slack, channel, header)

    n = 0
    for paper in targets:
        try:
            ok = _process_one(paper, re_cfg, client, columns, example, slack, channel)
            if ok:
                done.add(paper.version_less_id)
                _save_done(done)
                n += 1
        except Exception as e:
            log.error("[リソース推定] %s の処理でエラー（次回再試行）: %s", paper.arxiv_id, e)
    return n


def moonshot_arxiv_ids(schema_url: str) -> set[str]:
    """moonshot rows.json に既に載っている論文の arXiv ID（バージョンなし）を集める。
    論文列・数値根拠列などに含まれる arxiv.org/abs|pdf/XXXX.XXXXX リンクから抽出。"""
    pat = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})")
    ids: set[str] = set()
    try:
        r = requests.get(schema_url, timeout=60)
        r.raise_for_status()
        for rec in r.json().get("records", []):
            for v in rec.values():
                for m in pat.finditer(str(v)):
                    ids.add(m.group(1))
    except Exception as e:
        log.error("[リソース推定] moonshot既収録IDの取得失敗: %s", e)
    return ids


# =====================================================================
# 1論文の処理
# =====================================================================
def _process_one(paper: Paper, re_cfg: dict, client, columns: list[str],
                 example: dict, slack, channel: str) -> bool:
    """判定→抽出→Slack投稿。Slack投稿まで完了したらTrue（done記録対象）。"""
    model = _cfg(re_cfg, "model")

    body = _pdf_text_paged(paper, int(_cfg(re_cfg, "judge_max_chars")))
    verdict = _judge(client, model, re_cfg, paper, body)
    passed = (verdict.get("is_algorithm_paper") and verdict.get("in_ftqc_scope")
              and verdict.get("has_concrete_estimates"))
    log.info("[リソース推定] %s 判定: algo=%s ftqc=%s numbers=%s",
             paper.arxiv_id, verdict.get("is_algorithm_paper"),
             verdict.get("in_ftqc_scope"), verdict.get("has_concrete_estimates"))

    if not passed:
        if _cfg(re_cfg, "notify_rejected"):
            mark = lambda b: "✅" if b else "❌"
            text = (f"⏭️ *判定除外*: <{paper.abs_url}|{_esc(paper.title)}>\n"
                    f"　アルゴリズム新規性 {mark(verdict.get('is_algorithm_paper'))} / "
                    f"FTQCスコープ {mark(verdict.get('in_ftqc_scope'))} / "
                    f"数値報告 {mark(verdict.get('has_concrete_estimates'))}\n"
                    f"　理由: {verdict.get('reason', '(取得失敗)')}")
            _post(slack, channel, text)
        return True  # 判定済みとしてdone記録（再判定しない）

    # 抽出（判定より長い本文を渡す）
    body_full = _pdf_text_paged(paper, int(_cfg(re_cfg, "extract_max_chars")))
    result = _extract(client, model, re_cfg, paper, body_full or body, columns, example)
    records = result.get("records", [])
    status = _plot_status(records)
    status_label = {"plottable": "🟢 プロット可", "partial": "🟡 一部欠け（表には載る/要補完）",
                    "no_numbers": "🔴 数値なし（要人間確認）"}[status]

    # 自動draft PR (Phase B)
    #   auto_pr: false   -> PRを作らない（SlackにJSON投稿のみ = Phase A動作）
    #   pr_min_status: 抽出結果のステータスが何以上ならPRを作るか。STATUS_RANK のキーで指定:
    #     "plottable"  -> プロット可の論文だけPRを作る（最も厳しい）
    #     "partial"    -> プロット可・一部欠けの両方でPRを作る（デフォルト）
    #     "no_numbers" -> 常にPR対象外（数値ゼロの論文をPRする意味がないため）
    pr_url, pr_err = None, None
    min_status = _cfg(re_cfg, "pr_min_status")
    if min_status not in STATUS_RANK:
        log.warning("[リソース推定] pr_min_status='%s' は不明な値。'partial'として扱います", min_status)
        min_status = "partial"
    pr_wanted = (records and _cfg(re_cfg, "auto_pr")
                 and STATUS_RANK[status] >= STATUS_RANK[min_status]
                 and status != "no_numbers")
    if pr_wanted:
        pr_url, pr_err = _try_create_pr(paper, records, verdict, result, status, re_cfg)
    elif records and _cfg(re_cfg, "auto_pr"):
        log.info("[リソース推定] %s は status=%s のためPRを見送り（pr_min_status=%s）",
                 paper.arxiv_id, status, min_status)

    # 親メッセージ
    lines = [f"🎯 *リソース推定候補*: <{paper.abs_url}|{_esc(paper.title)}>",
             f"　判定: {verdict.get('reason', '')}",
             f"　抽出: {len(records)} レコード / {status_label}"]
    if pr_url:
        lines.append(f"　📮 draft PR: {pr_url}")
    elif pr_err:
        lines.append(f"　⚠️ PR自動作成失敗（{pr_err}）。JSONをスレッドに掲載します")
    ts = _post(slack, channel, "\n".join(lines))
    if ts is None:
        return False  # 投稿失敗→done記録せず次回再試行

    # スレッド1: rows.json追記用スニペット（PRが作れた場合は省略＝PR側にある）
    if records and not pr_url:
        snippet = json.dumps(records, ensure_ascii=False, indent=2)
        for chunk in _chunks(snippet, _SLACK_CHUNK):
            _post(slack, channel, f"```{chunk}```", thread_ts=ts)
            time.sleep(0.3)
    # スレッド2: レビュー手引き
    guide = _review_guide(paper, verdict, result, status, pr_url)
    _post(slack, channel, guide, thread_ts=ts)
    return True


# =====================================================================
# LLM呼び出し
# =====================================================================
def _llm_json(client, model: str, re_cfg: dict, system: str, user: str,
              max_tokens: int) -> dict:
    kwargs = {}
    effort = _cfg(re_cfg, "reasoning_effort")
    if effort:
        kwargs["reasoning_effort"] = str(effort)
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=model, max_completion_tokens=max_tokens,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                **kwargs)
            raw = (resp.choices[0].message.content or "").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            return json.loads(raw)
        except Exception as e:
            log.warning("[リソース推定] LLM呼び出し失敗(%d回目): %s", attempt + 1, e)
            time.sleep(3 * (attempt + 1))
    return {}


def _judge(client, model: str, re_cfg: dict, paper: Paper, body: str) -> dict:
    user = (f"タイトル: {paper.title}\n\nアブストラクト:\n{paper.abstract}\n\n"
            f"本文抜粋:\n{body if body else '(PDF取得失敗。タイトルとアブストで判定)'}")
    v = _llm_json(client, model, re_cfg, JUDGE_SYSTEM, user,
                  int(_cfg(re_cfg, "judge_max_tokens")))
    if not v:
        v = {"is_algorithm_paper": False, "in_ftqc_scope": False,
             "has_concrete_estimates": False, "reason": "LLM判定失敗（自動除外）"}
    return v


def _extract(client, model: str, re_cfg: dict, paper: Paper,
             body: str, columns: list[str], example: dict) -> dict:
    pdf_url = f"https://arxiv.org/pdf/{paper.arxiv_id}"
    system = EXTRACT_SYSTEM.format(
        columns=json.dumps(columns, ensure_ascii=False),
        example=json.dumps(example, ensure_ascii=False),
        pdf_url=pdf_url,
        max_records=int(_cfg(re_cfg, "max_records_per_paper")))
    user = (f"タイトル: {paper.title}\narXiv ID: {paper.arxiv_id}\n"
            f"PDF URL: {pdf_url}\n発表日: "
            f"{paper.published.strftime('%Y-%m-%d') if paper.published else 'NA'}\n\n"
            f"アブストラクト:\n{paper.abstract}\n\n本文（[PAGE N]マーカー付き）:\n{body}")
    result = _llm_json(client, model, re_cfg, system, user,
                       int(_cfg(re_cfg, "extract_max_tokens")))
    if not isinstance(result.get("records"), list):
        result["records"] = []
    return result


# =====================================================================
# 補助
# =====================================================================
def _fetch_schema(url: str) -> tuple[list[str], dict]:
    """rows.json から columns と記入例（実験実施=0のFTQC見積もりレコード）を取得。"""
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        d = r.json()
        columns = d.get("columns", [])
        example = {}
        for rec in d.get("records", []):
            if str(rec.get("実験実施", "")) == "0":
                example = rec
                break
        return columns, example
    except Exception as e:
        log.error("[リソース推定] スキーマ取得エラー: %s", e)
        return [], {}


def _pdf_text_paged(paper: Paper, max_chars: int) -> str:
    """PDF本文を [PAGE N] マーカー付きで返す（ページアンカー根拠付け用）。失敗時は空。"""
    if fitz is None:
        log.warning("[リソース推定] PyMuPDF(fitz) 未インストールのためPDF取得をスキップ")
        return ""
    try:
        # figure モジュールとの循環import回避のため、ここだけ遅延読み込みのまま残す
        from .figure import download_pdf
        pdf = download_pdf(paper)
        time.sleep(1.0)
        if not pdf:
            return ""
        doc = fitz.open(stream=pdf, filetype="pdf")
        parts, total = [], 0
        for i, page in enumerate(doc, 1):
            t = f"\n[PAGE {i}]\n" + page.get_text()
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return "".join(parts)[:max_chars]
    except Exception as e:
        log.warning("[リソース推定] PDF取得失敗 %s: %s", paper.arxiv_id, e)
        return ""


def _plot_status(records: list[dict]) -> str:
    """グラフの点になれるか: 論理量子ビット + (Toffoli/T/その他ゲート数) が揃った行があるか。"""
    if not records:
        return "no_numbers"
    def has_val(rec, keys):
        for col, v in rec.items():
            if any(k in col for k in keys):
                if v not in (None, "", "NA", "N/A", "0", 0) or v in (0, "0"):
                    if str(v).strip() not in ("", "NA", "N/A"):
                        return True
        return False
    any_numbers = False
    for rec in records:
        x = has_val(rec, X_AXIS_KEYS)
        y = has_val(rec, Y_AXIS_KEYS)
        if x or y:
            any_numbers = True
        if x and y:
            return "plottable"
    return "partial" if any_numbers else "no_numbers"


def _review_guide(paper: Paper, verdict: dict, result: dict, status: str,
                  pr_url: str | None = None) -> str:
    if pr_url:
        head = [f"*📋 レビュー手引き*", f"・draft PR: {pr_url}",
                "・PR上でdiffを確認し、必要なら直接編集してからreadyに"]
    else:
        head = ["*📋 レビュー手引き（手動PR用）*",
                f"・追記先: `pages/quantum-resource-estimates/data/resource_estimates_rows.json`",
                f"・上のJSONを `records` 配列の末尾に追記 → フォークからdraft PR"]
    lines = head + [
        f"・確認事項: ①数値根拠リンクを開いて値がページと一致するか "
        f"②値が正しい列に入っているか（論理/物理、T/Toffoli の区別） "
        f"③実験実施フラグ ④サブルーチン単体なら見積もりの種類に明記されているか",
    ]
    if status != "plottable":
        lines.append("⚠️ プロット必須列（論理量子ビット数＋ゲート数系）が未充足です。"
                     "論文の表を確認して人間側で補完するか、表専用の行として扱ってください。")
    notes = result.get("notes", "")
    if notes:
        lines.append(f"・抽出時メモ: {notes}")
    lines.append("_※ このデータはLLM抽出の下書きです。検証なしでPRしないでください。_")
    return "\n".join(lines)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _slack_client(token: str):
    if WebClient is None:
        raise RuntimeError("slack_sdk パッケージが未インストールです")
    c = WebClient(token=token)
    c.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    return c


def _post(slack, channel: str, text: str, thread_ts: str | None = None):
    """成功時はts、失敗時はNoneを返す。"""
    try:
        kwargs = {"channel": channel, "text": text,
                  "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return slack.chat_postMessage(**kwargs)["ts"]
    except Exception as e:
        log.error("[リソース推定] Slack投稿失敗: %s", e)
        return None


def _load_done() -> set[str]:
    try:
        if _STATE_PATH.exists():
            return set(json.loads(_STATE_PATH.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning("[リソース推定] done状態の読込失敗（空で継続）: %s", e)
    return set()


def _save_done(done: set[str]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except Exception as e:
        log.warning("[リソース推定] done状態の保存失敗: %s", e)


# =====================================================================
# Phase B: GitHub draft PR 自動作成（Git Data API使用、rows.jsonは1MB超のため）
# =====================================================================
_GH_API = "https://api.github.com"


def _gh(method: str, path: str, token: str, **kw):
    return requests.request(
        method, _GH_API + path, timeout=60,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"}, **kw)


def _append_records_to_rows(raw: str, records: list[dict]) -> str:
    """rows.json のrecords配列末尾にレコードを挿入し、row_countを更新する。
    既存部分のバイト列は一切変更しない（PR diffが追記行だけになる）。"""
    m = re.search(r"\n(\s*)\]\s*\}\s*$", raw)
    if not m:
        raise ValueError("rows.json の末尾構造（]}）を特定できない")
    insert_at = m.start()
    blocks = []
    for rec in records:
        dumped = json.dumps(rec, ensure_ascii=False, indent=2)
        blocks.append("\n".join("    " + ln for ln in dumped.splitlines()))
    inserted = raw[:insert_at] + ",\n" + ",\n".join(blocks) + raw[insert_at:]
    # row_count 更新（値の検証も兼ねてパース）
    d = json.loads(inserted)
    new_count = len(d["records"])
    inserted, n = re.subn(r'("row_count":\s*)\d+', rf"\g<1>{new_count}", inserted, count=1)
    if n != 1 or len(json.loads(inserted)["records"]) != new_count:
        raise ValueError("row_count の更新に失敗")
    return inserted


def _pr_body(paper: Paper, records: list[dict], verdict: dict, result: dict,
             status: str) -> str:
    status_ja = {"plottable": "プロット可", "partial": "一部欠け（グラフ必須列が未充足）",
                 "no_numbers": "数値なし"}[status]
    date = paper.published.strftime("%Y-%m-%d") if paper.published else "NA"
    body = [
        "## ⚠️ bot下書き・未検証",
        "paper_bot による自動抽出のdraft PRです。数値はLLM抽出であり、"
        "**マージ前に必ず数値根拠リンクと論文を照合してください**。",
        "",
        f"- 論文: [{paper.title}]({paper.abs_url}) (arXiv:{paper.version_less_id}, {date})",
        f"- 追加レコード: {len(records)} 件 / ステータス: {status_ja}",
        f"- 自動判定: {verdict.get('reason', '')}",
    ]
    notes = result.get("notes", "")
    if notes:
        body.append(f"- 抽出時メモ: {notes}")
    body += [
        "",
        "### レビューチェックリスト",
        "- [ ] 数値根拠リンクの値が論文の該当ページと一致する",
        "- [ ] 値が正しい列に入っている（論理/物理量子ビット、T/Toffoli/その他の区別）",
        "- [ ] 実験実施フラグが正しい",
        "- [ ] サブルーチン単体の見積もりなら「見積もりの種類」に明記されている",
        "- [ ] マージ後に `scripts/build_resource_estimates.sh` を実行する",
        "",
        "_自動生成: [paper_bot](https://github.com/masahikokamoshita/paper_bot) resource_est (Phase B)_",
    ]
    return "\n".join(body)


def _try_create_pr(paper: Paper, records: list[dict], verdict: dict, result: dict,
                   status: str, re_cfg: dict) -> tuple[str | None, str | None]:
    """draft PRを作成し (pr_url, None) を返す。失敗時は (None, 理由)。例外は投げない。"""
    try:
        token = os.environ.get(_cfg(re_cfg, "github_token_env"), "")
        if not token:
            return None, "MOONSHOT_PAT未設定"
        up_owner = _cfg(re_cfg, "upstream_owner")
        repo = _cfg(re_cfg, "upstream_repo")
        fork = _cfg(re_cfg, "fork_owner")
        base = _cfg(re_cfg, "base_branch")
        path = _cfg(re_cfg, "rows_path")
        branch = f"bot/resource-est-{paper.version_less_id.replace('.', '-')}"

        # 1) upstream mainの最新コミットとそのtree
        r = _gh("GET", f"/repos/{up_owner}/{repo}/git/ref/heads/{base}", token)
        if r.status_code != 200:
            return None, f"upstream ref取得失敗({r.status_code})"
        base_commit = r.json()["object"]["sha"]
        r = _gh("GET", f"/repos/{up_owner}/{repo}/git/commits/{base_commit}", token)
        if r.status_code != 200:
            return None, f"base commit取得失敗({r.status_code})"
        base_tree = r.json()["tree"]["sha"]

        # 2) 現行rows.jsonをblob APIで取得（1MB超のためcontents APIでは読めない）
        r = _gh("GET", f"/repos/{up_owner}/{repo}/contents/{path}", token,
                params={"ref": base_commit})
        if r.status_code != 200:
            return None, f"rows.jsonメタ取得失敗({r.status_code})"
        blob_sha = r.json()["sha"]
        r = _gh("GET", f"/repos/{up_owner}/{repo}/git/blobs/{blob_sha}", token)
        if r.status_code != 200:
            return None, f"rows.json blob取得失敗({r.status_code})"
        import base64
        raw = base64.b64decode(r.json()["content"]).decode("utf-8")

        # 3) レコード挿入（最小diff）→ 新blob（フォーク側に作成）
        new_raw = _append_records_to_rows(raw, records)
        r = _gh("POST", f"/repos/{fork}/{repo}/git/blobs", token,
                json={"content": base64.b64encode(new_raw.encode("utf-8")).decode(),
                      "encoding": "base64"})
        if r.status_code != 201:
            return None, f"blob作成失敗({r.status_code}: フォーク {fork}/{repo} の存在とPAT権限を確認)"
        new_blob = r.json()["sha"]

        # 4) tree → commit
        r = _gh("POST", f"/repos/{fork}/{repo}/git/trees", token,
                json={"base_tree": base_tree,
                      "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": new_blob}]})
        if r.status_code != 201:
            return None, f"tree作成失敗({r.status_code})"
        new_tree = r.json()["sha"]
        msg = f"Add resource estimates from arXiv:{paper.version_less_id} (bot draft)"
        r = _gh("POST", f"/repos/{fork}/{repo}/git/commits", token,
                json={"message": msg, "tree": new_tree, "parents": [base_commit]})
        if r.status_code != 201:
            return None, f"commit作成失敗({r.status_code})"
        new_commit = r.json()["sha"]

        # 5) フォークにブランチ作成（既存なら最新へ強制更新）
        r = _gh("POST", f"/repos/{fork}/{repo}/git/refs", token,
                json={"ref": f"refs/heads/{branch}", "sha": new_commit})
        if r.status_code == 422:  # 既存ブランチ
            r = _gh("PATCH", f"/repos/{fork}/{repo}/git/refs/heads/{branch}", token,
                    json={"sha": new_commit, "force": True})
            if r.status_code != 200:
                return None, f"ブランチ更新失敗({r.status_code})"
        elif r.status_code != 201:
            return None, f"ブランチ作成失敗({r.status_code})"

        # 6) 本家へdraft PR（既存なら流用）
        title = f"[bot] リソース見積もり追加: arXiv:{paper.version_less_id}"
        r = _gh("POST", f"/repos/{up_owner}/{repo}/pulls", token,
                json={"title": title, "head": f"{fork}:{branch}", "base": base,
                      "draft": True, "body": _pr_body(paper, records, verdict, result, status)})
        if r.status_code == 201:
            return r.json()["html_url"], None
        if r.status_code == 422:  # 同ブランチのPRが既に開いている等
            r2 = _gh("GET", f"/repos/{up_owner}/{repo}/pulls", token,
                     params={"head": f"{fork}:{branch}", "state": "open"})
            if r2.status_code == 200 and r2.json():
                return r2.json()[0]["html_url"], None
            return None, f"PR作成422({r.json().get('errors', '')})"
        return None, f"PR作成失敗({r.status_code})"
    except Exception as e:
        log.error("[リソース推定] PR作成で例外: %s", e)
        return None, f"例外({type(e).__name__})"
