# arXiv → 要約 → Discord / Slack Bot

arXiv（と Scirate の scite 数）でキーワードを監視し、新着論文を LLM で日本語要約して、
決まった時刻に **Discord と Slack の複数チャンネルへ振り分けて**送信する bot です。
GitHub Actions の cron で動くので常駐サーバは不要です。

```
arxiv(キーワードごとに検索) → scite付与 → 新着抽出 → 【上流で1回】要約(+高sciteは図解)
   → 各トピックのDiscord/Slackチャンネルへ配信 → 送信済みを記録
```

## 主な機能

- **複数トピック → 複数チャンネル**：トピックごとにキーワードを設定し、対応する Discord/Slack チャンネルへ送信。1論文が複数トピックに該当すれば複数チャンネルに届く（重複OK）。
- **要約は上流で1回**：論文1本につき1回だけ要約し、Discord・Slack で同じ結果を使い回す（送信先が増えてもコストは増えない）。
- **要約プロバイダを選択**：OpenAI（デフォルト）または Anthropic。要約なし（アブスト原文）モードも可。
- **高scite論文だけ図解**：PDF から代表図を1枚抜き、ビジョンで結果を日本語解説（〜1000字）。Slack・Discord とも**スレッド内**に図＋解説を入れる（Discordのスレッド化には Bot トークンが必要。無ければチャンネルへ追送）。
- **どのキーワードで当たったか記録**：キーワードを1語ずつ検索し、ヒット語を `state/match_log.jsonl` に永続保存（ノイズ源のキーワード特定に使える）。
- **取りこぼし防止（at-least-once）**：全送信先に届いた論文だけ「送信済み」に記録。失敗分は次回再送。

## 仕組みのポイント

- **取得**：arXiv 公式 API。トピックのキーワードを1語ずつ検索し、どの語で当たったかを記録。
- **Scirate**：公式 API が無いためカテゴリページを解析して scite 数を付与。**失敗しても arXiv 単体で正常動作**（自動スキップ）。
- **配信先**：
  - Discord … Webhook（トピックごとに1つ）。常駐bot不要。
  - Slack … Bot Token + Web API（スレッド返信・ファイル添付のため Webhook ではなく Bot を使用）。
- **重複防止/永続化**：`state/seen.json`（送信済み）と `state/match_log.jsonl`（キーワード記録）を Actions が毎回コミット。

## セットアップ

### 1. リポジトリを用意
一式を **private リポジトリ**として push（`.github/workflows/` がリポジトリ直下にあること）。

### 2. 送信先チャンネルを用意
- **Discord**：送りたいチャンネル →「チャンネルの編集」→「連携サービス」→「ウェブフック」→「新しいウェブフック」→ URL をコピー（チャンネルごとに1つ）。
  - **図解をスレッド化する場合（`discord.use_threads: true`）**：別途 Discord Bot が必要。discord.com/developers → New Application → Bot を作成し、**Bot Token** を取得。OAuth2 の招待URLで Bot をサーバに追加し、対象チャンネルで「メッセージを送信」「パブリックスレッドの作成」権限を付与。Bot Token を Secrets `DISCORD_BOT_TOKEN` に登録。Bot が無い/失敗時は自動でチャンネル追送にフォールバック。
- **Slack（使う場合）**：
  1. api.slack.com/apps で App を作成
  2. OAuth & Permissions → Bot Token Scopes に **`chat:write`** と **`files:write`**（図の添付に必要）を追加
  3. Install → **Bot User OAuth Token（`xoxb-...`）** をコピー
  4. 各チャンネルで Bot を招待（`/invite @アプリ名`）
  5. 各チャンネルの **ID（`Cxxxx`）** を取得（チャンネル名右クリック →「リンクをコピー」の末尾 等）

### 3. APIキーを用意
- 要約・図解に **OpenAI キー（`sk-...`）** を発行（provider=openai の場合）。
- provider=anthropic で使う場合のみ Anthropic キー。

### 4. GitHub Secrets を登録
**Settings → Secrets and variables → Actions → New repository secret**（値はブラウザで手入力、pushしない）。

| Name | 用途 | 必須 |
|------|------|------|
| `OPENAI_API_KEY` | 要約・図解（provider=openai） | 要約/図解を使うなら必須 |
| `DISCORD_WEBHOOK_<トピック>` | 各Discordチャンネル | 送るチャンネル分 |
| `SLACK_BOT_TOKEN` | Slack Bot Token（`xoxb-`） | Slackに送るなら必須 |
| `DISCORD_BOT_TOKEN` | Discordで図解をスレッド化する場合 | `discord.use_threads: true` なら必須 |
| `ANTHROPIC_API_KEY` | provider=anthropic の場合のみ | 任意 |

`DISCORD_WEBHOOK_<トピック>` の名前は `config.yaml` の各トピックの `webhook_env`（または `discord_webhook_env`）と**完全一致**させること（例：`DISCORD_WEBHOOK_QSVT`）。未登録のチャンネルは自動スキップされます。

### 5. トピックを設定
`config.yaml` の `topics` を編集：各トピックに `keywords`、Discord用 `webhook_env`、Slackに送るなら `slack_channel`（チャンネルID）を記入。

### 6. 動作確認
**Actions タブ →「arXiv Discord Bot」→ Run workflow** で即時実行。届けば成功。以降は cron（デフォルト毎日 09:00 JST）で自動配信。

## 配信時刻を変える

`.github/workflows/arxiv-bot.yml` の cron を編集（**UTC基準**、日本時間 = UTC + 9時間）。

| やりたいこと | cron |
|---|---|
| 毎日 09:00 JST | `0 0 * * *` |
| 毎日 07:37 JST（空いてる時間で遅延回避） | `37 22 * * *` |
| 平日のみ 09:23 JST | `23 0 * * 1-5` |

> GitHub の cron は混雑時に遅れます（特に「00分」「UTC 0:00」は集中）。半端な分にずらすと遅延が減ります。精度は「おおよその時刻」です。リポジトリが60日無活動だと停止しますが、毎回 `state/` をコミットするため停止しにくい設計です。

## ローカルで試す

```bash
pip install -r requirements.txt
export OPENAI_API_KEY="sk-..."
export DISCORD_WEBHOOK_QSVT="https://discord.com/api/webhooks/..."
export SLACK_BOT_TOKEN="xoxb-..."   # Slackを使う場合
python -m src.main
```

## 設定リファレンス（config.yaml）

- `defaults.categories` / `defaults.max_papers_per_run` … 全トピック共通の既定
- `arxiv.lookback_hours` … 新着とみなす投稿の範囲（広めが安全。重複はseenで防止）
- `scirate.min_scites` / `rank_by_scites` … scite下限フィルタ / scite順に送る
- `summary.enabled` … `true`=要約する / `false`=アブスト原文を掲載（API不要）
- `summary.provider` … `openai`（`OPENAI_API_KEY`）/ `anthropic`（`ANTHROPIC_API_KEY`）
- `summary.model` … 例）`gpt-4o-mini`、`gpt-4.1-mini`、`gpt-5.4-mini` など
- `summary.max_tokens` … 要約の長さ上限
- `figure.enabled` … 高scite論文の「結果解説＋代表図」を生成するか
- `figure.min_scites` … 図解対象の scite 下限
- `figure.max_papers_per_run` … 1実行で図解する上限（コスト対策）
- `figure.explain_max_chars` … 結果解説の文字数上限（例 1000）
- `discord.style` … `compact`（タイトル＋リンクの一覧）/ `full`（本文カード）
- `discord.use_threads` … 高scite論文の図解をスレッド内に入れる（要 `DISCORD_BOT_TOKEN`）
- `slack.enabled` / `bot_token_env` / `summary_in_thread` … Slack設定
- `topics[].keywords` … 1語ずつ検索されOR的に集約（当たった語を記録）
- `topics[].webhook_env` … Discord Webhook の Secrets 名
- `topics[].slack_channel_env` … SlackチャンネルIDを入れた Secrets の環境変数名（推奨。例 `SLACK_CHANNEL_QSVT`）
- `topics[].slack_channel` … SlackチャンネルIDを直書き（Publicリポジトリでは非推奨）
- `topics[].categories` … 省略時は `defaults.categories`

## 出力の形

- **Discord（compact）**：`・[タイトル](リンク) 日付・scite・kw` の一覧。高scite論文は、アンカーメッセージにスレッドを作りその中に図＋結果解説（`use_threads: true`＋Botトークン時）。無ければチャンネルへ追送。
- **Slack**：親＝タイトル＋リンク／返信1＝日本語要約／返信2＝結果解説＋代表図（高scite論文のみ）。

## 既知の注意点

- **図の抽出は best-effort**：埋め込み画像があればそれを、無ければ図の多いページを画像化。「代表的な図1枚」であり、必ず主結果の図とは限らない。失敗しても本体配信は継続。
- **OpenAIのモデル名**は変わることがある。`model not found` 等が出たら `config.yaml` の `summary.model` / `figure.model` を利用可能なモデルに変更する。
- **Slackの図添付**には Bot スコープ `files:write` が必要。
- **キーワード個別検索**により arXiv へのリクエスト数は増える（日次実行なら問題ない範囲）。

## Scirate の scite が取れない場合

Scirate は公式 API が無く HTML を解析しているため、サイト構造の変化で取れなくなることがあります（その場合も arXiv 単体で動作）。`src/scirate_source.py` の `_extract_pairs()` を実際のページHTMLに合わせて調整してください。

## 謝辞

This project uses the arXiv API.
Thank you to arXiv for use of its open access interoperability.
