"""
相関両建てアドバイザー
- Googleスプレッドシートからオープンポジションを読み込み
- CoinGecko APIで現在価格を取得
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
SHEET_NAME = os.environ.get("SHEET_NAME", "猫山")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# CoinGecko用シンボル→IDマッピング
SYMBOL_TO_CG_ID = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
    "ARB": "arbitrum", "OP": "optimism", "DOT": "polkadot", "ADA": "cardano",
    "LINK": "chainlink", "AVAX": "avalanche-2", "ATOM": "cosmos", "VET": "vechain",
    "APT": "aptos", "SEI": "sei-network", "IMX": "immutable-x", "GRT": "the-graph",
    "FIL": "filecoin", "AAVE": "aave", "UNI": "uniswap", "XLM": "stellar",
    "ETC": "ethereum-classic", "DOGE": "dogecoin", "XRP": "ripple",
    "ONDO": "ondo-finance", "WLD": "worldcoin-wld", "SUI": "sui",
    "TRX": "tron", "BCH": "bitcoin-cash", "ALGO": "algorand",
}

LONG_CANDIDATES = ["BTC", "ETH", "SOL", "BNB"]
SHORT_CANDIDATES = [
    "ARB", "OP", "DOT", "ADA", "LINK", "AVAX",
    "ATOM", "VET", "APT", "SEI", "IMX", "GRT",
    "FIL", "AAVE", "UNI", "XLM", "ETC", "DOGE",
]


# ========== CoinGecko 価格取得 ==========
def get_prices(symbols: list) -> dict:
    ids = []
    sym_to_id = {}
    for sym in symbols:
        cg_id = SYMBOL_TO_CG_ID.get(sym.upper())
        if cg_id:
            ids.append(cg_id)
            sym_to_id[cg_id] = sym.upper()

    if not ids:
        return {}

    try:
        r = requests.get(
            f"{COINGECKO_BASE}/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": "usd"},
            timeout=30,
            headers={"Accept": "application/json"},
        )
        data = r.json()
        prices = {}
        for cg_id, val in data.items():
            sym = sym_to_id.get(cg_id)
            if sym and "usd" in val:
                prices[sym] = float(val["usd"])
        print(f"価格取得成功: {len(prices)}銘柄")
        return prices
    except Exception as e:
        print(f"価格取得失敗: {e}")
        return {}


# ========== Googleスプレッドシート読み込み ==========
def get_open_positions() -> list:
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
    for i, row in enumerate(rows[2:], start=3):
        if len(row) < 11:
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
                print(f"行{i}パース失敗: {e}")

    return open_positions


# ========== 含み損益計算 ==========
def calc_pnl(position: dict, prices: dict) -> dict:
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
    pos_text = ""
    for p in positions_with_pnl:
        long_pnl_str = f"${p['long_pnl']:.2f}" if p['long_pnl'] is not None else "取得不可"
        short_pnl_str = f"${p['short_pnl']:.2f}" if p['short_pnl'] is not None else "取得不可"
        total_str = f"${p['total_pnl']:.2f}" if p['total_pnl'] is not None else "計算不可"
        long_cur = f"${p['long_current']:.4f}" if p['long_current'] else "取得不可"
        short_cur = f"${p['short_current']:.6f}" if p['short_current'] else "取得不可"

        pos_text += f"""
【{p['exchange']}】{p['long_sym']}ロング / {p['short_sym']}ショート ({p['strategy']})
  エントリー: {p['entry_time']}
  LONG: ${p['long_entry']:.4f} → {long_cur}  PnL: {long_pnl_str}
  SHORT: ${p['short_entry']:.6f} → {short_cur}  PnL: {short_pnl_str}
  合計含み損益: {total_str}
"""

    price_text = "\n".join([f"  {sym}: ${price}" for sym, price in sorted(prices.items())])

    prompt = f"""あなたは仮想通貨の相関両建てトレードの専門アドバイザーです。

【戦術ルール】
- 強い銘柄(BTC/ETH/SOL/BNB)をロング、弱いアルトをショート
- ポジション比率1:1、レバレッジ最大5倍
- 利確は早め、損切り厳守、ナンピン最大3回
- 合計損失$200で撤退

【現在のオープンポジション】
{pos_text if pos_text else "なし"}

【現在価格】
{price_text}

【依頼】
1. 各ポジションへの判断（利確/ナンピン/損切/維持）と理由を簡潔に
2. 新規推奨ペア2〜3個（根拠付き）
3. 全体的な相場観

日本語で、スマホで読みやすく、絵文字を適度に使って。"""

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
    if "content" not in data:
        error_msg = data.get("error", {}).get("message", str(data))
        raise RuntimeError(f"Claude APIエラー: {error_msg}")
    return data["content"][0]["text"]


# ========== Telegram通知 ==========
def send_telegram(message: str):
    max_len = 4000
    chunks = [message[i:i+max_len] for i in range(0, len(message), max_len)]
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
            timeout=10,
        )
        if not r.ok:
            print(f"Telegram送信失敗: {r.text}")


# ========== メイン ==========
def main():
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] 分析開始")

    print("スプレッドシート読み込み中...")
    positions = get_open_positions()
    print(f"オープンポジション: {len(positions)}件")

    symbols_needed = set(LONG_CANDIDATES + SHORT_CANDIDATES)
    for p in positions:
        if p["long_sym"]:
            symbols_needed.add(p["long_sym"])
        if p["short_sym"]:
            symbols_needed.add(p["short_sym"])

    print("価格取得中...")
    prices = get_prices(list(symbols_needed))

    positions_with_pnl = [calc_pnl(p, prices) for p in positions]

    print("Claude分析中...")
    analysis = analyze_with_claude(positions_with_pnl, prices)

    message = f"""🤖 相関両建てアドバイザー
🕐 {now}
📊 オープンポジション: {len(positions)}件

{analysis}

━━━━━━━━━━━━
💡 取引はご自身の判断で実行してください"""

    print("Telegram通知送信中...")
    send_telegram(message)
    print("完了！")


if __name__ == "__main__":
    main()
