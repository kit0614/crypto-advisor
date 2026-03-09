"""
相関両建てアドバイザー 統合版
=============================
【処理フロー】
1. スプレッドシートからオープンポジションを読み込み
2. CoinGeckoで現在価格を取得
3. Claude APIでポジション判断 + 新規推奨ペア(2〜3個)を取得
4. 推奨ペアごとにバックテストを自動実行（過去180日・グリッドサーチ）
5. Telegramに以下をまとめて通知
   - 既存ポジション判断
   - 新規推奨ペア + バックテスト結果（Top10含む）
   - 全体相場観

【注意】
- CoinGecko無料APIのレート制限対策として、バックテスト間に待機時間を入れています
- バックテストは日次データのため、4時間足との誤差があります（傾向把握として活用）
- スプレッドシートのP列(index=15)にエントリーレシオを記録してください
"""

import os
import re
import json
import time
import itertools
import requests
import datetime as dt
from typing import Optional

import numpy as np
import gspread
from google.oauth2.service_account import Credentials

# ========== 環境変数 ==========
SPREADSHEET_ID    = os.environ["SPREADSHEET_ID"]
SHEET_NAME        = os.environ.get("SHEET_NAME", "猫山")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# ========== 銘柄マッピング ==========
SYMBOL_TO_CG_ID = {
    "BTC": "bitcoin",       "ETH": "ethereum",      "SOL": "solana",
    "BNB": "binancecoin",   "ARB": "arbitrum",       "OP": "optimism",
    "DOT": "polkadot",      "ADA": "cardano",        "LINK": "chainlink",
    "AVAX": "avalanche-2",  "ATOM": "cosmos",        "VET": "vechain",
    "APT": "aptos",         "SEI": "sei-network",    "IMX": "immutable-x",
    "GRT": "the-graph",     "FIL": "filecoin",       "AAVE": "aave",
    "UNI": "uniswap",       "XLM": "stellar",        "ETC": "ethereum-classic",
    "DOGE": "dogecoin",     "XRP": "ripple",         "SUI": "sui",
    "TRX": "tron",          "BCH": "bitcoin-cash",   "ALGO": "algorand",
    "ONDO": "ondo-finance",  "WLD": "worldcoin-wld",
}

LONG_CANDIDATES  = ["BTC", "ETH", "SOL", "BNB"]
SHORT_CANDIDATES = [
    "ARB", "OP", "DOT", "ADA", "LINK", "AVAX",
    "ATOM", "VET", "APT", "SEI", "IMX", "GRT",
    "FIL", "AAVE", "UNI", "XLM", "ETC", "DOGE",
]
ZOMBIE_SYMBOLS = ["LTC", "BCH", "ETC", "XLM", "DOGE", "ALGO", "TRX", "XRP"]

# ========== ルール定数 ==========
RATIO_TAKE_PROFIT_PCT = 0.08   # レシオ-8%で利確シグナル
RATIO_STOP_LOSS_PCT   = 0.15   # レシオ+15%で損切りシグナル
RATIO_NANPIN_PCT      = 0.07   # レシオ+7%でナンピン検討
DOLLAR_STOP_LOSS      = -200.0 # -$200で問答無用撤退
DOLLAR_TAKE_PROFIT    = 30.0   # +$30で利確検討
HOLD_HOURS_LONG       = 168    # 7日超で利確積極検討

# ========== バックテスト設定 ==========
BACKTEST_DAYS  = 180
POSITION_SIZE  = 500  # 片側ポジション額($)
TP_RANGE = [round(x, 1) for x in np.arange(0.5, 6.0, 0.5)]   # 0.5〜5.5%
SL_RANGE = [round(x, 1) for x in np.arange(2.0, 21.0, 1.0)]  # 2〜20%
BACKTEST_SHEET = "backtest_results"


# ==============================================================
# ① 現在価格取得
# ==============================================================
def get_prices(symbols: list) -> dict:
    ids, sym_to_id = [], {}
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


# ==============================================================
# ② スプレッドシート読み込み
# ==============================================================
def get_open_positions() -> list:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    rows = ws.get_all_values()

    open_positions = []
    for i, row in enumerate(rows[2:], start=3):
        if len(row) < 11:
            continue
        exchange        = row[0].strip()
        entry_time      = row[1].strip()
        long_sym        = row[3].strip()
        long_qty        = row[4].strip()
        long_entry      = row[5].strip()
        short_sym       = row[8].strip()
        short_qty       = row[9].strip()
        short_entry     = row[10].strip()
        strategy        = row[14].strip() if len(row) > 14 else ""
        entry_ratio_raw = row[15].strip() if len(row) > 15 else ""
        short_pnl_cell  = row[12].strip() if len(row) > 12 else ""

        if not (exchange and long_sym and not short_pnl_cell):
            continue
        try:
            long_entry_f  = float(long_entry.replace(",", ""))  if long_entry  else 0
            short_entry_f = float(short_entry.replace(",", "")) if short_entry else 0

            if entry_ratio_raw:
                entry_ratio = float(entry_ratio_raw.replace(",", ""))
            elif long_entry_f > 0 and short_entry_f > 0:
                entry_ratio = short_entry_f / long_entry_f
            else:
                entry_ratio = None

            open_positions.append({
                "row": i, "exchange": exchange, "entry_time": entry_time,
                "long_sym": long_sym,
                "long_qty":    float(long_qty.replace(",", ""))  if long_qty   else 0,
                "long_entry":  long_entry_f,
                "short_sym":   short_sym,
                "short_qty":   float(short_qty.replace(",", "")) if short_qty  else 0,
                "short_entry": short_entry_f,
                "strategy": strategy, "entry_ratio": entry_ratio,
            })
        except Exception as e:
            print(f"行{i}パース失敗: {e}")
    return open_positions


# ==============================================================
# ③ 含み損益 + レシオ計算
# ==============================================================
def calc_hold_hours(entry_time_str: str) -> Optional[float]:
    for fmt in ["%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"]:
        try:
            delta = dt.datetime.utcnow() - dt.datetime.strptime(entry_time_str, fmt)
            return delta.total_seconds() / 3600
        except ValueError:
            continue
    return None


def calc_pnl(position: dict, prices: dict) -> dict:
    long_price  = prices.get(position["long_sym"])
    short_price = prices.get(position["short_sym"])

    long_pnl  = (long_price  - position["long_entry"])  * position["long_qty"] \
        if long_price  and position["long_entry"]  > 0 else None
    short_pnl = (position["short_entry"] - short_price) * position["short_qty"] \
        if short_price and position["short_entry"] > 0 else None
    total_pnl = (long_pnl + short_pnl) \
        if (long_pnl is not None and short_pnl is not None) else None

    current_ratio = (short_price / long_price) \
        if (long_price and short_price and long_price > 0) else None
    entry_ratio   = position.get("entry_ratio")
    ratio_change_pct = ((current_ratio - entry_ratio) / entry_ratio * 100) \
        if (current_ratio and entry_ratio and entry_ratio > 0) else None
    hold_hours = calc_hold_hours(position.get("entry_time", ""))

    # シグナル判定
    if total_pnl is not None and total_pnl <= DOLLAR_STOP_LOSS:
        signal = "🔴損切り【撤退ライン到達】"
    elif ratio_change_pct is not None and ratio_change_pct >= RATIO_STOP_LOSS_PCT * 100:
        signal = f"🔴損切り検討【レシオ+{ratio_change_pct:.1f}%】"
    elif (ratio_change_pct is not None and ratio_change_pct <= -RATIO_TAKE_PROFIT_PCT * 100) \
            or (total_pnl is not None and total_pnl >= DOLLAR_TAKE_PROFIT):
        pnl_str = f"${total_pnl:.2f}" if total_pnl is not None else ""
        ratio_str = f"レシオ{ratio_change_pct:.1f}% / " if ratio_change_pct is not None else ""
        signal = f"🟢利確検討【{ratio_str}{pnl_str}】"
    elif ratio_change_pct is not None \
            and RATIO_NANPIN_PCT * 100 <= ratio_change_pct < RATIO_STOP_LOSS_PCT * 100:
        signal = f"🟡ナンピン検討【レシオ+{ratio_change_pct:.1f}%】"
    elif hold_hours is not None and hold_hours >= HOLD_HOURS_LONG \
            and total_pnl is not None and total_pnl > 0:
        signal = f"🟢利確検討【{hold_hours:.0f}時間保有】"
    else:
        signal = "🔵維持"

    return {
        **position,
        "long_current": long_price,   "short_current": short_price,
        "long_pnl":     long_pnl,     "short_pnl":     short_pnl,
        "total_pnl":    total_pnl,    "current_ratio": current_ratio,
        "entry_ratio":  entry_ratio,  "ratio_change_pct": ratio_change_pct,
        "hold_hours":   hold_hours,   "signal":        signal,
    }


# ==============================================================
# ④ Claude API（ポジション判断 + 推奨ペア抽出）
# ==============================================================
def analyze_with_claude(positions_with_pnl: list, prices: dict) -> tuple:
    """戻り値: (表示用テキスト, 推奨ペアリスト[{"long":str,"short":str}])"""

    pos_text = ""
    for p in positions_with_pnl:
        total_str  = f"${p['total_pnl']:.2f}"    if p["total_pnl"]    is not None else "計算不可"
        long_cur   = f"${p['long_current']:.4f}"  if p["long_current"] else "取得不可"
        short_cur  = f"${p['short_current']:.6f}" if p["short_current"] else "取得不可"
        hold_str   = f"{p['hold_hours']:.0f}時間" if p["hold_hours"]   is not None else "不明"

        if p["current_ratio"] is not None and p["entry_ratio"] is not None:
            direction  = "有利✅" if (p["ratio_change_pct"] or 0) < 0 else "不利⚠️"
            ratio_info = (f"エントリー:{p['entry_ratio']:.6f}→現在:{p['current_ratio']:.6f} "
                          f"変化:{p['ratio_change_pct']:+.1f}%({direction})")
        elif p["current_ratio"] is not None:
            ratio_info = f"現在レシオ:{p['current_ratio']:.6f}（エントリー時未記録）"
        else:
            ratio_info = "計算不可"

        pos_text += (
            f"\n【{p['exchange']}】{p['long_sym']}L/{p['short_sym']}S ({p['strategy']})\n"
            f"  エントリー:{p['entry_time']}（保有:{hold_str}）\n"
            f"  LONG:${p['long_entry']:.4f}→{long_cur}\n"
            f"  SHORT:${p['short_entry']:.6f}→{short_cur}\n"
            f"  合計損益:{total_str} / レシオ:{ratio_info}\n"
            f"  判定:{p['signal']}\n"
        )

    price_text  = "\n".join([f"  {s}: ${v}" for s, v in sorted(prices.items())])
    zombie_list = ", ".join(ZOMBIE_SYMBOLS)

    prompt = f"""あなたは仮想通貨の相関両建てトレードの専門アドバイザーです。

【戦術ルール（数値ベース・厳守）】
■ポジション基本
- 強い銘柄(BTC/ETH/SOL/BNB)をロング、弱いアルトをショート
- ロング：ショート=1:1、レバレッジ最大5倍

■損切り（最優先）
- 合計損失-$200到達→即損切り
- レシオ変化+15%以上→損切り検討

■利確
- 含み益+$30以上 or レシオ変化-8%以下→利確
- 7日超保有かつ含み益あり→積極利確

■ナンピン
- レシオ変化+7%〜+15%→ナンピン検討（最大3回）

■ゾンビ銘柄（新規ショート非推奨）: {zombie_list}

【オープンポジション】
{pos_text if pos_text else "なし"}

【現在価格】
{price_text}

【回答形式（厳守）】
1. 各ポジション判断（利確/ナンピン/損切り/維持を明示、根拠1行）
2. 新規推奨ペア2〜3個（必ず以下の形式を含めること）
   RECOMMEND: LONG=BTC SHORT=DOT
   RECOMMEND: LONG=ETH SHORT=ARB
   ※ゾンビ銘柄をショートに含めないこと
   ※推奨理由も1行で記載
3. 相場観（3行以内）

日本語・スマホで読みやすく・絵文字適度に。"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1800,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    data = response.json()
    if "content" not in data:
        raise RuntimeError(f"Claude APIエラー: {data.get('error', {}).get('message', str(data))}")

    text = data["content"][0]["text"]

    # RECOMMEND行から推奨ペアを抽出
    pairs = []
    for m in re.finditer(r"RECOMMEND:\s*LONG=(\w+)\s+SHORT=(\w+)", text):
        long_sym, short_sym = m.group(1).upper(), m.group(2).upper()
        if long_sym in SYMBOL_TO_CG_ID and short_sym in SYMBOL_TO_CG_ID:
            pairs.append({"long": long_sym, "short": short_sym})

    # 表示テキストからRECOMMEND行を除去（後でバックテスト結果と一緒に表示）
    display_text = re.sub(r"RECOMMEND:.*\n?", "", text).strip()
    return display_text, pairs


# ==============================================================
# ⑤ バックテスト（日次データ取得 + グリッドサーチ）
# ==============================================================
def fetch_daily_prices_for_bt(symbol: str, days: int) -> Optional[dict]:
    """
    日次終値を {date: price} で返す
    【改良点】
    - リトライ回数を3→5回に増加
    - 429(レート制限)は長めに待機（60秒）
    - 5xx系サーバーエラーも個別ハンドリング
    - 取得成功時にデータ件数をログ出力
    """
    cg_id = SYMBOL_TO_CG_ID.get(symbol.upper())
    if not cg_id:
        print(f"  {symbol}: CoinGecko IDが未登録")
        return None

    url    = f"{COINGECKO_BASE}/coins/{cg_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": "daily"}

    for attempt in range(5):
        try:
            r = requests.get(url, params=params, timeout=45,
                             headers={"Accept": "application/json"})

            # レート制限（429）は長めに待機してリトライ
            if r.status_code == 429:
                wait = 60 + attempt * 15
                print(f"  {symbol} レート制限(429) → {wait}秒待機してリトライ({attempt+1}/5)")
                time.sleep(wait)
                continue

            # サーバーエラー（5xx）は短め待機でリトライ
            if r.status_code >= 500:
                wait = 15 + attempt * 10
                print(f"  {symbol} サーバーエラー({r.status_code}) → {wait}秒待機({attempt+1}/5)")
                time.sleep(wait)
                continue

            r.raise_for_status()
            price_data = r.json().get("prices", [])

            if not price_data:
                print(f"  {symbol}: 価格データが空（attempt {attempt+1}/5）")
                time.sleep(10)
                continue

            result = {
                dt.datetime.utcfromtimestamp(p[0] / 1000).date(): p[1]
                for p in price_data
            }
            print(f"  {symbol}: {len(result)}日分取得成功")
            return result

        except requests.exceptions.Timeout:
            print(f"  {symbol} タイムアウト({attempt+1}/5) → 15秒待機")
            time.sleep(15)
        except Exception as e:
            print(f"  {symbol} 取得失敗({attempt+1}/5): {type(e).__name__}: {e}")
            time.sleep(10 + attempt * 5)

    print(f"  {symbol}: 5回リトライ失敗、スキップ")
    return None


def run_backtest_for_pair(long_sym: str, short_sym: str) -> Optional[dict]:
    """
    1ペアのバックテストを実行してグリッドサーチ結果を返す
    戻り値: {best_tp, best_sl, best_pnl, best_wr, best_count, top10}
    """
    print(f"  {long_sym}/{short_sym} データ取得中...")
    long_prices  = fetch_daily_prices_for_bt(long_sym,  BACKTEST_DAYS + 5)
    time.sleep(8)   # レート制限対策：長め待機
    short_prices = fetch_daily_prices_for_bt(short_sym, BACKTEST_DAYS + 5)
    time.sleep(8)

    if not long_prices or not short_prices:
        print(f"  {long_sym}/{short_sym} データ取得失敗")
        return None

    common_dates = sorted(set(long_prices) & set(short_prices))[-BACKTEST_DAYS:]
    if len(common_dates) < 30:
        print(f"  {long_sym}/{short_sym} データ不足({len(common_dates)}日)")
        return None

    ratios = [short_prices[d] / long_prices[d] for d in common_dates]
    n      = len(ratios)

    # グリッドサーチ
    results = []
    MAX_HOLD = 30
    for tp, sl in itertools.product(TP_RANGE, SL_RANGE):
        trades = []
        i = 0
        while i < n - 1:
            entry_r  = ratios[i]
            tp_level = entry_r * (1 - tp / 100)
            sl_level = entry_r * (1 + sl / 100)
            hold     = min(MAX_HOLD, n - 1 - i)
            exit_r   = ratios[min(i + MAX_HOLD, n - 1)]
            for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
                cur = ratios[j]
                if cur <= tp_level or cur >= sl_level:
                    hold   = j - i
                    exit_r = cur
                    break
            trades.append(-(exit_r - entry_r) / entry_r * POSITION_SIZE)
            i += hold + 1

        if not trades:
            continue
        total = round(sum(trades), 2)
        wr    = round(sum(1 for x in trades if x > 0) / len(trades) * 100, 1)
        results.append({"tp": tp, "sl": sl, "pnl": total, "wr": wr, "count": len(trades)})

    if not results:
        return None

    results.sort(key=lambda x: x["pnl"], reverse=True)
    best = results[0]
    return {
        "best_tp": best["tp"], "best_sl": best["sl"],
        "best_pnl": best["pnl"], "best_wr": best["wr"],
        "best_count": best["count"], "top10": results[:10],
    }


# ==============================================================
# ⑥ バックテスト結果をスプレッドシートに書き込み
# ==============================================================
def write_backtest_result(long_sym: str, short_sym: str, result: dict):
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet(BACKTEST_SHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=BACKTEST_SHEET, rows=500, cols=10)
            ws.append_row(["実行日時", "LONG", "SHORT",
                           "最適利確%", "最適損切%", "過去勝率%", "総損益($)", "取引回数"])

        ws.append_row([
            dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            long_sym, short_sym,
            result["best_tp"], result["best_sl"],
            result["best_wr"], result["best_pnl"], result["best_count"],
        ])
        print(f"  スプレッドシート書き込み完了: {long_sym}/{short_sym}")
    except Exception as e:
        print(f"  スプレッドシート書き込み失敗: {e}")


# ==============================================================
# ⑦ バックテスト結果のTelegram表示テキスト生成
# ==============================================================
def format_backtest_message(long_sym: str, short_sym: str, result: dict) -> str:
    top10_lines = ""
    for rank, r in enumerate(result["top10"], 1):
        top10_lines += (
            f"  {rank:2}位 TP{r['tp']}% SL{r['sl']}%"
            f" 勝率{r['wr']}% ${r['pnl']} ({r['count']}回)\n"
        )
    return (
        f"\n📊 {long_sym}L/{short_sym}S（過去{BACKTEST_DAYS}日）\n"
        f"  🏆 最適: 利確{result['best_tp']}% / 損切{result['best_sl']}%\n"
        f"  勝率:{result['best_wr']}% 総損益:${result['best_pnl']} {result['best_count']}回\n"
        f"  ─ Top10 ─\n"
        f"{top10_lines}"
    )


# ==============================================================
# ⑧ Telegram通知
# ==============================================================
def send_telegram(message: str):
    for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
            timeout=10,
        )
        if not r.ok:
            print(f"Telegram送信失敗: {r.text}")


# ==============================================================
# ⑨ メイン
# ==============================================================
def main():
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] 分析開始")

    # 1. ポジション読み込み
    print("スプレッドシート読み込み中...")
    positions = get_open_positions()
    print(f"オープンポジション: {len(positions)}件")

    # 2. 価格取得
    symbols_needed = set(LONG_CANDIDATES + SHORT_CANDIDATES)
    for p in positions:
        symbols_needed.update([p["long_sym"], p["short_sym"]])
    print("価格取得中...")
    prices = get_prices(list(symbols_needed))

    # 3. 含み損益計算
    positions_with_pnl = [calc_pnl(p, prices) for p in positions]

    # 4. Claude分析（ポジション判断 + 推奨ペア取得）
    print("Claude分析中...")
    claude_text, recommend_pairs = analyze_with_claude(positions_with_pnl, prices)
    print(f"推奨ペア: {recommend_pairs}")

    # 5. 推奨ペアのバックテスト自動実行
    bt_section = ""
    if recommend_pairs:
        print(f"バックテスト実行: {len(recommend_pairs)}ペア")
        for pair in recommend_pairs:
            long_sym, short_sym = pair["long"], pair["short"]
            result = run_backtest_for_pair(long_sym, short_sym)
            if result:
                bt_section += format_backtest_message(long_sym, short_sym, result)
                write_backtest_result(long_sym, short_sym, result)
            else:
                bt_section += f"\n⚠️ {long_sym}/{short_sym} バックテスト失敗\n"
            time.sleep(5)  # ペア間の待機（レート制限対策）
    else:
        bt_section = "\n⚠️ 推奨ペアの自動抽出ができませんでした\n"

    # 6. 価格スナップショット生成
    # ポジション保有銘柄と推奨ペア銘柄を優先表示、その後LONGロング候補を表示
    position_syms = []
    for p in positions_with_pnl:
        for sym in [p["long_sym"], p["short_sym"]]:
            if sym and sym not in position_syms:
                position_syms.append(sym)

    recommend_syms = []
    for pair in recommend_pairs:
        for sym in [pair["long"], pair["short"]]:
            if sym and sym not in recommend_syms and sym not in position_syms:
                recommend_syms.append(sym)

    # スナップショット表示：保有銘柄 → 推奨銘柄 → LONGロング候補
    snapshot_lines = []
    if position_syms:
        snapshot_lines.append("【保有中】")
        for sym in position_syms:
            price = prices.get(sym)
            snapshot_lines.append(f"  {sym}: ${price:,.4f}" if price else f"  {sym}: 取得不可")

    if recommend_syms:
        snapshot_lines.append("【推奨候補】")
        for sym in recommend_syms:
            price = prices.get(sym)
            snapshot_lines.append(f"  {sym}: ${price:,.4f}" if price else f"  {sym}: 取得不可")

    snapshot_lines.append("【主要銘柄】")
    for sym in LONG_CANDIDATES:
        if sym not in position_syms and sym not in recommend_syms:
            price = prices.get(sym)
            snapshot_lines.append(f"  {sym}: ${price:,.2f}" if price else f"  {sym}: 取得不可")

    price_snapshot = "\n".join(snapshot_lines)

    # 7. Telegram通知
    message = (
        f"🤖 相関両建てアドバイザー\n"
        f"🕐 取得時刻: {now}\n"
        f"⚠️ 通知遅延がある場合は上記時刻を基準にしてください\n"
        f"📊 オープンポジション: {len(positions)}件\n"
        f"━━━━━━━━━━━━\n"
        f"💹 価格スナップショット（取得時刻基準）\n"
        f"{price_snapshot}\n"
        f"━━━━━━━━━━━━\n"
        f"{claude_text}\n"
        f"━━━━━━━━━━━━\n"
        f"🔬 新規推奨ペア バックテスト結果\n"
        f"{bt_section}\n"
        f"━━━━━━━━━━━━\n"
        f"💡 取引はご自身の判断で実行してください"
    )

    print("Telegram通知送信中...")
    send_telegram(message)
    print("完了！")


if __name__ == "__main__":
    main()
