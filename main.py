"""
相関両建てアドバイザー
- Googleスプレッドシートからオープンポジションを読み込み
- Bybit APIで現在価格を取得
- Claude APIで利確/ナンピン/損切/維持を分析
- 新規推奨ペアも提示
- Telegram Botで通知
"""

import os
import json
import requests
import datetime as dt
from typing import Optional
import gspread
from google.oauth2.service_account import Credentials

# ========== 設定 ==========
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = "猫山"  # タブ名

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]  # JSONの中身をそのまま

BYBIT_BASE = "https://api.bybit.com"

# ロング候補（強い銘柄）
LONG_CANDIDATES = ["BTC", "ETH", "SOL", "BNB"]

# ショート候補（弱い銘柄・相関両建て戦術に基づく）
SHORT_CANDIDATES = [
    "ARB", "OP", "DOT", "ADA", "LINK", "AVAX",
    "ATOM", "VET", "APT", "SEI", "IMX", "GRT",
    "FIL", "AAVE", "UNI", "XLM", "ETC", "DOGE",
]


# ========== Bybit 価格取得 ==========
def get_bybit_price(symbol: str) -> Optional[float]:
    """Bybitから現在価格を取得"""
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/tickers",
            params={"category": "linear", "symbol": f"{symbol}USDT"},
            timeout=10,
        )
        data = r.json()
        price = float(data["result"]["list"][0]["lastPrice"])
        return price
    except Exception as e:
        print(f"価格取得失敗 {symbol}: {e}")
        return None


def get_prices(symbols: list) -> dict:
    """複数銘柄の価格をまとめて取得"""
    prices = {}
    for sym in symbols:
        p = get_bybit_price(sym)
        if p:
            prices[sym] = p
    return prices


# ========== Googleスプレッドシート読み込み ==========
def get_open_positions() -> list:
    """
    スプレッドシートからオープンポジション（CLOSETIME空）を取得
    カラム: A=取引所, B=ENTRYTIME, C=CLOSETIME,
            D=LONG銘柄, E=QTY, F=ENTRY, G=EXIT, H=P&L,
            I=SHORT銘柄, J=QTY, K=ENTRY, L=EXIT, M=P&L,
            N=合計P&L, O=戦略タグ
    """
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet(SHEET_NAME)
    rows = ws.get_all_values()

    open_positions = []
    for i, row in enumerate(rows[2:], start=3):  # 3行目から（1,2行目はヘッダー）
        if len(row) < 13:
            continue
        exchange = row[0].strip()
        entry_time = row[1].strip()
        close_time = row[2].strip()
        long_sym = row[3].strip()
        long_qty = row[4].strip()
        long_entry = row[5].strip()
        short_sym = row[8].strip()
        short_qty = row[9].strip()
        short_entry = row[10].strip()
        strategy = row[14].strip() if len(row) > 14 else ""

        # オープン条件：取引所あり、LONG銘柄あり、CLOSETIMEが空
        if exchange and long_sym and not close_time:
            try:
                open_positions.append({
                    "row": i,
                    "exchange": exchange,
                    "entry_time": entry_time,
                    "long_sym": long_sym,
                    "long_qty": float(long_qty.replace(",", "")) if long_qty else 0,
                    "long_entry": float(long_entry.replace(",", "")) if long_entry else 0,
                    "short_sym": short_sym,
                    "short_qty": float(short_qty.replace(",", "")) if short_qty else 0,
                    "short_entry": float(short_entry.replace(",", "")) if short_entry else 0,
                    "strategy": strategy,
                })
            except Exception as e:
                print(f"行{i}のパース失敗: {e}")
                continue

    return open_positions


# ========== 含み損益計算 ==========
def calc_pnl(position: dict, prices: dict) -> dict:
    """現在価格から含み損益を計算"""
    long_sym = position["long_sym"]
    short_sym = position["short_sym"]

    long_price = prices.get(long_sym)
    short_price = prices.get(short_sym)

    long_pnl = None
    short_pnl = None
    total_pnl = None

    if long_price and position["long_entry"] > 0:
        long_pnl = (long_price - position["long_entry"]) * position["long_qty"]

    if short_price and position["short_entry"] > 0:
        short_pnl = (position["short_entry"] - short_price) * position["short_qty"]

    if long_pnl is not None and short_pnl is not None:
        total_pnl = long_pnl + short_pnl

    return {
        **position,
        "long_current": long_price,
        "short_current": short_price,
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
        "total_pnl": total_pnl,
    }


# ========== Claude API分析 ==========
def analyze_with_claude(positions_with_pnl: list, prices: dict) -> str:
    """Claude APIでポジション分析と新規推奨ペアを生成"""

    # ポジション情報をテキスト化
    pos_text = ""
    for p in positions_with_pnl:
        long_pnl_str = f"${p['long_pnl']:.2f}" if p['long_pnl'] is not None else "価格取得不可"
        short_pnl_str = f"${p['short_pnl']:.2f}" if p['short_pnl'] is not None else "価格取得不可"
        total_str = f"${p['total_pnl']:.2f}" if p['total_pnl'] is not None else "計算不可"

        pos_text += f"""
【{p['exchange']}】{p['long_sym']}ロング / {p['short_sym']}ショート
  エントリー: {p['entry_time']}
  LONGエントリー: ${p['long_entry']:.4f} → 現在: ${p['long_current'] or '取得不可'}  PnL: {long_pnl_str}
  SHORTエントリー: ${p['short_entry']:.6f} → 現在: ${p['short_current'] or '取得不可'}  PnL: {short_pnl_str}
  合計含み損益: {total_str}
  戦略: {p['strategy']}
"""

    # 現在価格一覧
    price_text = "\n".join([f"  {sym}: ${price:.4f}" for sym, price in prices.items()])

    prompt = f"""あなたは仮想通貨の相関両建てトレードの専門アドバイザーです。

【戦術の基本ルール】
- 強い銘柄（BTC/ETH/SOL/BNB）をロング、弱い銘柄（アルト）をショート
- ポジション比率は常に1:1
- レバレッジ最大5倍
- 複数ペアに分散投資
- 利確は早め、損切りルール厳守
- ナンピンは最大3回まで
- 合計損失$200で問答無用で撤退

【現在のオープンポジション】
{pos_text if pos_text else "なし"}

【現在価格】
{price_text}

【依頼】
1. 各ポジションについて「利確 / ナンピン / 損切 / 現状維持」の判断と理由を簡潔に
2. 今エントリーすべき新規推奨ペアを2〜3個（根拠付きで）
3. 全体的な相場観コメント

回答は日本語で、スマホで読みやすいよう簡潔に。絵文字を適度に使ってください。"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )

    data = response.json()
    return data["content"][0]["text"]


# ========== Telegram通知 ==========
def send_telegram(message: str):
    """Telegram Botでメッセージ送信"""
    # Telegramは4096文字制限があるので分割
    max_len = 4000
    chunks = [message[i:i+max_len] for i in range(0, len(message), max_len)]

    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        if not r.ok:
            print(f"Telegram送信失敗: {r.text}")


# ========== メイン ==========
def main():
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] 分析開始")

    # 1. オープンポジション取得
    print("スプレッドシート読み込み中...")
    positions = get_open_positions()
    print(f"オープンポジション: {len(positions)}件")

    # 2. 必要な銘柄の価格取得
    symbols_needed = set(LONG_CANDIDATES + SHORT_CANDIDATES)
    for p in positions:
        if p["long_sym"]:
            symbols_needed.add(p["long_sym"])
        if p["short_sym"]:
            symbols_needed.add(p["short_sym"])

    print("価格取得中...")
    prices = get_prices(list(symbols_needed))
    print(f"価格取得: {len(prices)}銘柄")

    # 3. 含み損益計算
    positions_with_pnl = [calc_pnl(p, prices) for p in positions]

    # 4. Claude分析
    print("Claude分析中...")
    analysis = analyze_with_claude(positions_with_pnl, prices)

    # 5. 通知メッセージ組み立て
    message = f"""🤖 *相関両建てアドバイザー*
🕐 {now}
📊 オープンポジション: {len(positions)}件

{analysis}

━━━━━━━━━━━━━━
💡 取引はご自身の判断で実行してください"""

    # 6. Telegram送信
    print("Telegram通知送信中...")
    send_telegram(message)
    print("完了！")


if __name__ == "__main__":
    main()
