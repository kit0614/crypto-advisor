# PERP FR ARB Dashboard

## ローカルダッシュボード（PC）

### セットアップ（初回のみ）
1. Python 3.10以上 をインストール（https://www.python.org）
2. このフォルダを任意の場所に置く

### 起動方法
**`起動.bat` をダブルクリック** → ブラウザが自動で開く

または:
```bash
pip install flask flask-cors requests
python server.py
# → http://localhost:5000
```

### 操作
- **更新ボタン**: 全取引所からFRを再取得（1〜2分かかります）
- **取引所フィルター**: 左サイドバーで取引所を選ぶと、その取引所が絡む機会に絞り込み
- **相手取引所**: さらに対となる取引所を選択可能
- **マトリクスタブ**: 全取引所ペアのベストスプレッドを一覧表示

---

## GitHub Actions Telegram通知（毎時45分）

### セットアップ
1. このフォルダをGitHubリポジトリにpush
2. GitHubリポジトリの **Settings → Secrets → Actions** に追加:
   - `TELEGRAM_BOT_TOKEN`: BotFather から取得したトークン
   - `TELEGRAM_CHAT_ID`: 通知先のチャットID

### Telegram Bot作成（未作成の場合）
1. Telegram で `@BotFather` を開く
2. `/newbot` → 名前とユーザー名を設定
3. 発行されたトークンを `TELEGRAM_BOT_TOKEN` に設定
4. Botにメッセージを送ってから `https://api.telegram.org/bot<TOKEN>/getUpdates` でChat IDを確認

### スケジュール
- 毎時 **45分** に自動実行（FR確定15分前）
- `.github/workflows/fr_alert.yml` の cron を変更すれば調整可能

### 手動実行
GitHub → Actions → FR Arbitrage Alert → Run workflow

---

## ファイル構成
```
fr_arb_system/
├── 起動.bat              # ダブルクリック起動（Windows）
├── server.py             # Flaskローカルサーバー
├── fr_fetcher.py         # 全取引所FR取得ロジック
├── notify_telegram.py    # Telegram通知スクリプト
├── requirements.txt
├── static/
│   └── index.html        # ダッシュボードUI
└── .github/
    └── workflows/
        └── fr_alert.yml  # GitHub Actionsワークフロー
```
