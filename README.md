# sst-daily-updater

NASA MUR SST データを日次で取得し、Supabase `sst_daily` テーブルに格納する GitHub Actions ジョブ。

## 概要

| 項目 | 値 |
|------|-----|
| データソース | NASA MUR SST (ERDDAP) |
| 空間解像度 | 1km |
| 時間解像度 | 日次 |
| データ遅延 | 3日 |
| 実行タイミング | 毎日 JST 10:00 (UTC 01:00) |

## 対象地点

| コード | 名称 | 座標 | MMM |
|--------|------|------|-----|
| manza | 万座 | 26.5080, 127.8540 | 29.0℃ |
| sesoko | 瀬底 | 26.6494, 127.8536 | 29.0℃ |
| ogasawara | 小笠原 | 27.0942, 142.1919 | 28.5℃ |

## GitHub Secrets

以下の Secrets を設定してください：

| Secret | 説明 |
|--------|------|
| `SUPABASE_URL` | Supabase プロジェクト URL |
| `SUPABASE_KEY` | Supabase サービスロールキー |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot トークン（オプション） |
| `TELEGRAM_CHAT_ID` | Telegram チャット ID（オプション） |

## 手動実行

1. GitHub リポジトリの Actions タブへ
2. 「SST Daily Sync」ワークフローを選択
3. 「Run workflow」をクリック
4. モードを選択（daily / dry-run）

## ローカルテスト

```bash
# 環境変数を設定
export SUPABASE_URL="https://xxx.supabase.co"
export SUPABASE_KEY="xxx"

# dry-run
python sync.py --dry-run

# 実行
python sync.py

# 特定サイトのみ
python sync.py --site manza
```

## 関連リソース

| リソース | URL |
|----------|-----|
| marine-obs.org | https://marine-obs.org |
| Supabase | https://supabase.com/dashboard/project/pegiuiblpliainpdggfj |
| stormglass-updater | https://github.com/hirokifukui/stormglass-updater |
