# arXiv → 要約 → Discord Bot

arXiv（と Scirate の scite 数）でキーワード・著者を監視し、新着論文を
Claude API で日本語要約して、決まった時刻に Discord へ送信する bot です。
GitHub Actions の cron で動くので、常駐サーバは不要です。

```
arxiv → (Scirateでscite数を付与) → 新着だけ抽出 → Claudeで要約 → Discordへ送信 → 送信済みを記録
```

## 仕組みのポイント

- **取得**: arXiv 公式 API（`export.arxiv.org/api/query`）。キーワードは OR 検索、著者は著者ごとに検索して重複排除。
- **Scirate**: 公式 API が無いため、カテゴリページを取得して scite 数（注目度）を付与します。**取得に失敗しても arXiv 単体で正常動作**します（自動スキップ）。
- **要約**: Claude Messages API。デフォルト `claude-sonnet-4-6`（品質重視）。安く回すなら `config.yaml` の `summary.model` を `claude-haiku-4-5-20251001` に。
- **配信**: Discord Webhook（常駐 bot 不要、POST するだけ）。
- **重複防止**: 送信済み ID を `state/seen.json` に保存し、Actions が毎回コミットして永続化。

## セットアップ（GitHub Actions）

### 1. リポジトリを用意
このフォルダ一式を **private リポジトリ**として GitHub に push します。

### 2. Discord Webhook URL を取得
配信したいチャンネル → 「チャンネルの編集」→「連携サービス」→「ウェブフック」→
「新しいウェブフック」→ URL をコピー。

### 3. Claude API キーを取得
https://platform.claude.com でアカウント作成 → API キーを発行。

### 4. GitHub Secrets を登録
リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で2つ登録：

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | Claude の API キー |
| `DISCORD_WEBHOOK_URL` | Discord の Webhook URL |

### 5. 監視対象を設定
`config.yaml` の `watch`（keywords / authors / categories）を編集。

### 6. 動作確認（手動実行）
**Actions タブ → 「arXiv Discord Bot」→ Run workflow** で即時実行できます。
Discord に届けば成功。あとは cron（デフォルト毎日 09:00 JST）で自動配信されます。

## 配信時刻を変える

`.github/workflows/arxiv-bot.yml` の cron を編集（**UTC基準**）。
日本時間 = UTC + 9 時間。

| やりたいこと | cron |
|---|---|
| 毎日 09:00 JST | `0 0 * * *` |
| 毎日 07:00 JST | `0 22 * * *` |
| 平日のみ 09:00 JST | `0 0 * * 1-5` |
| 1日2回（09:00 / 18:00 JST） | `0 0,9 * * *` |

> GitHub の cron は混雑時に数分〜遅延することがあります。また、リポジトリが
> 60日間無活動だと無効化されますが、本 bot は毎回 `seen.json` をコミットするため
> 活動とみなされ、無効化されにくいです。

## ローカルで試す

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python -m src.main
```

## 設定リファレンス（config.yaml）

- `watch.keywords` … OR 検索するフレーズ
- `watch.authors` … 著者名（フルネーム推奨）
- `watch.categories` … 絞り込む arXiv 分野（空で全分野）
- `arxiv.lookback_hours` … 新着とみなす投稿の範囲（cron 間隔より少し長めに）
- `scirate.min_scites` … この scite 数未満を除外（0で無効）
- `scirate.rank_by_scites` … scite 数が多い順に送る
- `scirate.add_trending` … キーワードに合致しなくても scite 上位の話題作を拾う
- `summary.model` … 要約モデル（`claude-sonnet-4-6` / `claude-haiku-4-5-20251001`）
- `summary.max_papers_per_run` … 1回の送信上限（コスト/レート対策）

## Scirate の scite が取れない場合

Scirate は公式 API が無く HTML を解析しているため、サイト構造が変わると
scite 数が取れなくなることがあります（その場合も bot は arXiv 単体で動作）。
復旧させたいときは `src/scirate_source.py` の `_extract_pairs()` を、実際の
ページ HTML に合わせて調整してください。

## 謝辞

This project uses the arXiv API.
Thank you to arXiv for use of its open access interoperability.
