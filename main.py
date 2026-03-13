import os
import re
import json
import math
import time
import requests
import datetime as dt
from typing import Dict, List, Optional, Tuple

import numpy as np
import gspread
from google.oauth2.service_account import Credentials

# =========================
# 環境変数
# =========================
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "猫山")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

BYBIT_BASE = "https://api.bybit.com"

LONG_CANDIDATES = ["BTC", "ETH", "SOL", "BNB"]
SHORT_CANDIDATES = [
    "ARB", "OP", "DOT", "ADA", "LINK", "AVAX", "ATOM", "VET", "APT", "SEI",
    "IMX", "GRT", "FIL", "AAVE", "UNI", "XLM", "ETC", "DOGE", "ALGO", "ONDO",
    "WLD", "INJ", "LDO", "CRV", "DYDX", "PENDLE", "RUNE", "ENA", "AERO"
]
UNIVERSE = sorted(set(LONG_CANDIDATES + SHORT_CANDIDATES))

# NOTE型パラメータ
BARS_PER_DAY_4H = 6
WINDOW_14D = 14 * BARS_PER_DAY_4H
WINDOW_30D = 30 * BARS_PER_DAY_4H
WINDOW_90D = 90 * BARS_PER_DAY_4H
WINDOW_180D = 180 * BARS_PER_DAY_4H
WINDOW_12M = 365 * BARS_PER_DAY_4H
ATR_N = 14
SPIKE_K = 6.0
SPIKE_FLOOR = 0.02
HARD_SL_DOLLAR = -30.0
TP_HALF_DOLLAR = 8.0
TP_FULL_DOLLAR = 15.0
MA90_MULT_CUT = 1.05

# バックテスト
BT_TP_VALUES = [0.8, 1.2, 1.5, 2.0, 2.5, 3.0]
BT_SL_VALUES = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
BT_MAX_HOLD_BARS = 18  # 3日

# ================
# Util
# ================

def utc_now_str() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def safe_float(x: str) -> Optional[float]:
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def calc_hold_hours(entry_time_str: str) -> Optional[float]:
    for fmt in ["%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"]:
        try:
            delta = dt.datetime.utcnow() - dt.datetime.strptime(entry_time_str, fmt)
            return delta.total_seconds() / 3600
        except ValueError:
            pass
    return None


def sma(arr: np.ndarray, n: int) -> np.ndarray:
    if len(arr) < n:
        return np.full(len(arr), np.nan)
    out = np.full(len(arr), np.nan)
    csum = np.cumsum(np.insert(arr, 0, 0.0))
    out[n - 1:] = (csum[n:] - csum[:-n]) / n
    return out


def slope_log(series: np.ndarray, window: int) -> float:
    if len(series) < max(window, 10):
        return np.nan
    y = np.log(series[-window:])
    x = np.arange(len(y))
    try:
        return float(np.polyfit(x, y, 1)[0])
    except Exception:
        return np.nan


def max_runup_from_low(series: np.ndarray) -> float:
    if len(series) < 2:
        return np.nan
    min_so_far = series[0]
    best = 0.0
    for v in series[1:]:
        if min_so_far > 0:
            best = max(best, (v - min_so_far) / min_so_far)
        min_so_far = min(min_so_far, v)
    return float(best)


# ================
# Bybit 4h データ取得
# ================

def bybit_symbol(sym: str) -> str:
    return f"{sym.upper()}USDT"


def fetch_bybit_klines(symbol: str, interval: str = "240", total_limit: int = 2200) -> List[dict]:
    pair = bybit_symbol(symbol)
    rows: List[dict] = []
    end_ms = None
    remaining = total_limit

    while remaining > 0:
        limit = min(remaining, 1000)
        params = {
            "category": "linear",
            "symbol": pair,
            "interval": interval,
            "limit": limit,
        }
        if end_ms is not None:
            params["end"] = end_ms
        r = requests.get(f"{BYBIT_BASE}/v5/market/kline", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit kline error {pair}: {data}")
        part = data["result"]["list"]
        if not part:
            break
        # Bybitは新しい順
        for item in part:
            rows.append({
                "ts": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            })
        remaining -= len(part)
        oldest = min(int(x[0]) for x in part)
        end_ms = oldest - 1
        if len(part) < limit:
            break
        time.sleep(0.08)

    # 時系列昇順・重複排除
    dedup = {}
    for r in rows:
        dedup[r["ts"]] = r
    out = [dedup[k] for k in sorted(dedup.keys())]
    return out


def fetch_latest_closes(symbols: List[str]) -> Dict[str, float]:
    prices = {}
    for sym in symbols:
        try:
            rows = fetch_bybit_klines(sym, total_limit=5)
            if rows:
                prices[sym] = float(rows[-1]["close"])
        except Exception as e:
            print(f"latest close failed {sym}: {e}")
    return prices


# ================
# Sheet 読み込み
# ================

def get_open_positions() -> List[dict]:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    rows = ws.get_all_values()

    positions = []
    for i, row in enumerate(rows[2:], start=3):
        if len(row) < 11:
            continue
        exchange = row[0].strip()
        entry_time = row[1].strip()
        long_sym = row[3].strip().upper()
        long_qty = safe_float(row[4])
        long_entry = safe_float(row[5])
        short_sym = row[8].strip().upper()
        short_qty = safe_float(row[9])
        short_entry = safe_float(row[10])
        short_pnl_cell = row[12].strip() if len(row) > 12 else ""
        strategy = row[14].strip() if len(row) > 14 else ""
        entry_ratio = safe_float(row[15]) if len(row) > 15 and row[15].strip() else None

        if not (exchange and long_sym and short_sym and not short_pnl_cell):
            continue
        if None in (long_qty, long_entry, short_qty, short_entry):
            continue
        if entry_ratio is None and long_entry > 0 and short_entry > 0:
            entry_ratio = short_entry / long_entry

        positions.append({
            "row": i,
            "exchange": exchange,
            "entry_time": entry_time,
            "long_sym": long_sym,
            "long_qty": long_qty,
            "long_entry": long_entry,
            "short_sym": short_sym,
            "short_qty": short_qty,
            "short_entry": short_entry,
            "strategy": strategy,
            "entry_ratio": entry_ratio,
        })
    return positions


# ================
# NOTE 指標
# ================

def align_ratio_series(strong_rows: List[dict], weak_rows: List[dict]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    s = {r["ts"]: r["close"] for r in strong_rows}
    w = {r["ts"]: r["close"] for r in weak_rows}
    ts = sorted(set(s.keys()) & set(w.keys()))
    strong = np.array([s[t] for t in ts], dtype=float)
    weak = np.array([w[t] for t in ts], dtype=float)
    ratio = weak / strong
    return np.array(ts, dtype=np.int64), strong, ratio


def calc_corr180(strong: np.ndarray, weak: np.ndarray) -> float:
    if len(strong) < WINDOW_180D + 2:
        return np.nan
    rs = np.diff(np.log(strong[-(WINDOW_180D + 1):]))
    rw = np.diff(np.log(weak[-(WINDOW_180D + 1):]))
    if np.std(rs) == 0 or np.std(rw) == 0:
        return np.nan
    return float(np.corrcoef(rs, rw)[0, 1])


def calc_ratio_metrics(ts: np.ndarray, strong: np.ndarray, ratio: np.ndarray) -> dict:
    ratio_ret_4h = float(ratio[-1] / ratio[-2] - 1) if len(ratio) >= 2 else np.nan
    ratio_ret_1d = float(ratio[-1] / ratio[-7] - 1) if len(ratio) >= 7 else np.nan

    tr = np.abs(np.diff(ratio, prepend=ratio[0]))
    atr = sma(tr, ATR_N)
    atr_last = float(atr[-1]) if not math.isnan(atr[-1]) else np.nan
    atr_pct = float(atr_last / ratio[-1]) if ratio[-1] > 0 and not math.isnan(atr_last) else np.nan
    spike_threshold = max(SPIKE_FLOOR, SPIKE_K * atr_pct) if not math.isnan(atr_pct) else np.nan
    spike_flags = (ratio[1:] / ratio[:-1] - 1) > np.maximum(SPIKE_FLOOR, SPIKE_K * (atr[1:] / ratio[1:]))
    spike_count = int(np.nansum(spike_flags)) if len(spike_flags) else 0

    ma90 = float(np.nanmean(ratio[-WINDOW_90D:])) if len(ratio) >= WINDOW_90D else float(np.nanmean(ratio))
    ma90_mult = float(ratio[-1] / ma90) if ma90 > 0 else np.nan

    return {
        "ratio_ret_4h": ratio_ret_4h,
        "ratio_ret_1d": ratio_ret_1d,
        "atr_pct_ratio_14": atr_pct,
        "spike_threshold_4h": spike_threshold,
        "spike_count_12m": spike_count,
        "max_runup_12m": max_runup_from_low(ratio[-min(len(ratio), WINDOW_12M):]),
        "ma90_ratio": ma90,
        "ma90_mult": ma90_mult,
        "slope_14d": slope_log(ratio, min(WINDOW_14D, len(ratio))),
        "slope_90d": slope_log(ratio, min(WINDOW_90D, len(ratio))),
        "slope_180d": slope_log(ratio, min(WINDOW_180D, len(ratio))),
        "slope_12m": slope_log(ratio, min(WINDOW_12M, len(ratio))),
    }


def spike_grade(spike_count: int, max_runup: float) -> str:
    if spike_count >= 4 or max_runup > 0.65:
        return "AVOID"
    if spike_count >= 2 or max_runup > 0.45:
        return "WATCH"
    return "OK"


def strong_health(strong_sym: str, data_map: Dict[str, List[dict]]) -> str:
    strong_rows = data_map.get(strong_sym)
    if not strong_rows or len(strong_rows) < 8:
        return "CAUTION"
    s = np.array([r["close"] for r in strong_rows], dtype=float)
    rets_4h = []
    rets_1d = []
    for sym, rows in data_map.items():
        if sym == strong_sym or sym in LONG_CANDIDATES:
            continue
        if len(rows) < 8:
            continue
        px = np.array([r["close"] for r in rows], dtype=float)
        # 最後のバー数を合わせる
        m = min(len(px), len(s))
        px = px[-m:]
        ss = s[-m:]
        rets_4h.append(px[-1] / px[-2] - 1)
        rets_1d.append(px[-1] / px[-7] - 1)
    if not rets_4h:
        return "CAUTION"
    alt_4h = float(np.mean(rets_4h))
    alt_1d = float(np.mean(rets_1d))
    sr_4h = float(s[-1] / s[-2] - 1) - alt_4h
    sr_1d = float(s[-1] / s[-7] - 1) - alt_1d
    if sr_4h >= 0 and sr_1d >= 0:
        return "OK"
    if sr_4h < 0 and sr_1d < 0:
        return "OFF"
    return "CAUTION"


# ================
# 判定
# ================

def decide_new_action(metrics: dict, corr180: float, s_health: str) -> Tuple[str, str]:
    trend_ok = all((not math.isnan(metrics[k]) and metrics[k] < 0) for k in ("slope_12m", "slope_180d", "slope_90d"))
    spike_g = spike_grade(metrics["spike_count_12m"], metrics["max_runup_12m"])
    if math.isnan(corr180) or corr180 < 0.70 or not trend_ok or spike_g == "AVOID":
        return "SKIP", spike_g
    if s_health == "OFF":
        return "SKIP", spike_g
    if metrics["ratio_ret_1d"] < 0 and metrics["ratio_ret_4h"] < 0:
        return "ENTER", spike_g
    if metrics["ratio_ret_1d"] < 0 and metrics["ratio_ret_4h"] >= 0:
        return "WAIT", spike_g
    return "SKIP", spike_g


def decide_hold_action(pos: dict, pnl_total: Optional[float], metrics: dict, s_health: str) -> Tuple[str, bool, str]:
    dca_ok = False
    if not math.isnan(metrics["ratio_ret_4h"]) and not math.isnan(metrics["atr_pct_ratio_14"]):
        dca_ok = (
            metrics["ratio_ret_4h"] > max(SPIKE_FLOOR, SPIKE_K * metrics["atr_pct_ratio_14"]) and
            metrics["ma90_mult"] <= MA90_MULT_CUT and
            (not math.isnan(metrics["slope_14d"]) and metrics["slope_14d"] <= 0)
        )
    if pnl_total is not None and pnl_total <= HARD_SL_DOLLAR:
        return "EXIT", dca_ok, "HARD_SL"
    if metrics["ma90_mult"] > MA90_MULT_CUT:
        return "EXIT", dca_ok, "SHAPE_SL"
    if pnl_total is not None and pnl_total >= TP_FULL_DOLLAR:
        return "TRIM", dca_ok, "TP_FULL"
    if pnl_total is not None and pnl_total >= TP_HALF_DOLLAR:
        return "TRIM", dca_ok, "TP_HALF"
    if metrics["ratio_ret_1d"] > 0 and s_health == "OFF":
        return "TRIM", dca_ok, "STRONG_OFF"
    return "HOLD", dca_ok, "OK"


# ================
# PnL
# ================

def calc_position_pnl(position: dict, latest_prices: Dict[str, float]) -> dict:
    lp = latest_prices.get(position["long_sym"])
    sp = latest_prices.get(position["short_sym"])
    long_pnl = (lp - position["long_entry"]) * position["long_qty"] if lp else None
    short_pnl = (position["short_entry"] - sp) * position["short_qty"] if sp else None
    total_pnl = (long_pnl + short_pnl) if long_pnl is not None and short_pnl is not None else None
    current_ratio = (sp / lp) if lp and sp else None
    entry_ratio = position.get("entry_ratio")
    ratio_change_pct = ((current_ratio - entry_ratio) / entry_ratio * 100) if current_ratio and entry_ratio else None
    hold_hours = calc_hold_hours(position.get("entry_time", ""))
    return {
        **position,
        "long_current": lp,
        "short_current": sp,
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
        "total_pnl": total_pnl,
        "current_ratio": current_ratio,
        "ratio_change_pct": ratio_change_pct,
        "hold_hours": hold_hours,
    }


# ================
# Backtest (4h)
# ================

def backtest_ratio_grid(ratio: np.ndarray) -> dict:
    # 4h進捗ベースの軽量グリッド
    best = None
    rows = []
    if len(ratio) < 200:
        return {"best": None, "top": []}
    for tp in BT_TP_VALUES:
        for sl in BT_SL_VALUES:
            pnl = 0.0
            wins = 0
            count = 0
            holds = []
            i = 7
            while i < len(ratio) - BT_MAX_HOLD_BARS - 1:
                if ratio[i] / ratio[i - 6] - 1 < 0 and ratio[i] / ratio[i - 1] - 1 < 0:
                    entry = ratio[i]
                    tp_lv = entry * (1 - tp / 100)
                    sl_lv = entry * (1 + sl / 100)
                    exit_ratio = ratio[min(i + BT_MAX_HOLD_BARS, len(ratio) - 1)]
                    hold = BT_MAX_HOLD_BARS
                    for j in range(i + 1, min(i + BT_MAX_HOLD_BARS + 1, len(ratio))):
                        if ratio[j] <= tp_lv or ratio[j] >= sl_lv:
                            exit_ratio = ratio[j]
                            hold = j - i
                            break
                    trade_pnl = -(exit_ratio - entry) / entry * 500.0
                    pnl += trade_pnl
                    wins += 1 if trade_pnl > 0 else 0
                    count += 1
                    holds.append(hold)
                    i += hold + 1
                else:
                    i += 1
            if count < 5:
                continue
            row = {
                "tp": tp,
                "sl": sl,
                "pnl": round(pnl, 2),
                "wr": round(wins / count * 100, 1),
                "count": count,
                "avg_hold_bars": round(float(np.mean(holds)), 1) if holds else None,
            }
            rows.append(row)
            if best is None or row["pnl"] > best["pnl"]:
                best = row
    rows = sorted(rows, key=lambda x: (x["pnl"], x["wr"]), reverse=True)[:10]
    return {"best": best, "top": rows}


# ================
# Claude 整形専用
# ================

def render_with_claude(payload: dict) -> str:
    prompt = f"""あなたはTelegram通知文の整形担当です。判断はすでにPythonが確定しています。\n\nルール:\n- action_hold / action_new / dca_ok を絶対に変更しない\n- 数値を再計算しない\n- 既存ポジションは HOLD/TRIM/EXIT をそのまま表示\n- 新規候補は ENTER/WAIT/SKIP をそのまま表示\n- スマホで読みやすい日本語\n- 4000字以内\n\n入力JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n出力形式:\n1. 実行時刻\n2. 保有中判定\n3. 新規候補\n4. 一言コメント\n"""
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
        raise RuntimeError(f"Claude API error: {data}")
    return data["content"][0]["text"]


# ================
# Telegram
# ================

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }, timeout=30)
    r.raise_for_status()


# ================
# Main
# ================

def main() -> None:
    positions = get_open_positions()
    symbols_needed = sorted(set(UNIVERSE + [p["long_sym"] for p in positions] + [p["short_sym"] for p in positions]))

    print("Fetching Bybit 4h klines...")
    data_map: Dict[str, List[dict]] = {}
    for sym in symbols_needed:
        try:
            data_map[sym] = fetch_bybit_klines(sym, total_limit=2200)
            print(f"  {sym}: {len(data_map[sym])} bars")
        except Exception as e:
            print(f"  {sym}: fetch failed: {e}")
    latest_prices = {sym: rows[-1]["close"] for sym, rows in data_map.items() if rows}

    # 保有中判定
    pos_out = []
    for p in positions:
        if p["long_sym"] not in data_map or p["short_sym"] not in data_map:
            continue
        pnl = calc_position_pnl(p, latest_prices)
        ts, strong, ratio = align_ratio_series(data_map[p["long_sym"]], data_map[p["short_sym"]])
        weak_px = np.array([r["close"] for r in data_map[p["short_sym"]]][-len(strong):])
        corr = calc_corr180(strong, weak_px)
        metrics = calc_ratio_metrics(ts, strong, ratio)
        s_health = strong_health(p["long_sym"], data_map)
        action_hold, dca_ok, reason = decide_hold_action(p, pnl["total_pnl"], metrics, s_health)
        pos_out.append({
            "pair": f"{p['long_sym']}/{p['short_sym']}",
            "strong": p["long_sym"],
            "weak": p["short_sym"],
            "action_hold": action_hold,
            "hold_reason": reason,
            "dca_ok": dca_ok,
            "corr180": round(corr, 3) if not math.isnan(corr) else None,
            "ratio_ret_4h": metrics["ratio_ret_4h"],
            "ratio_ret_1d": metrics["ratio_ret_1d"],
            "ma90_mult": metrics["ma90_mult"],
            "spike_count_12m": metrics["spike_count_12m"],
            "spike_grade": spike_grade(metrics["spike_count_12m"], metrics["max_runup_12m"]),
            "strong_health": s_health,
            "total_pnl": pnl["total_pnl"],
            "long_current": pnl["long_current"],
            "short_current": pnl["short_current"],
            "dca_trigger_4h": max(SPIKE_FLOOR, SPIKE_K * metrics["atr_pct_ratio_14"]) if not math.isnan(metrics["atr_pct_ratio_14"]) else None,
        })

    # 新規候補
    candidates = []
    active_pairs = {(p["long_sym"], p["short_sym"]) for p in positions}
    for strong_sym in LONG_CANDIDATES:
        if strong_sym not in data_map:
            continue
        s_health = strong_health(strong_sym, data_map)
        for weak_sym in SHORT_CANDIDATES:
            if weak_sym == strong_sym or weak_sym not in data_map:
                continue
            if (strong_sym, weak_sym) in active_pairs:
                continue
            try:
                ts, strong, ratio = align_ratio_series(data_map[strong_sym], data_map[weak_sym])
                weak_px = np.array([r["close"] for r in data_map[weak_sym]][-len(strong):])
                corr = calc_corr180(strong, weak_px)
                metrics = calc_ratio_metrics(ts, strong, ratio)
                action_new, s_grade = decide_new_action(metrics, corr, s_health)
                bt = backtest_ratio_grid(ratio)
                candidates.append({
                    "pair": f"{strong_sym}/{weak_sym}",
                    "strong": strong_sym,
                    "weak": weak_sym,
                    "action_new": action_new,
                    "corr180": round(corr, 3) if not math.isnan(corr) else None,
                    "ratio_ret_4h": metrics["ratio_ret_4h"],
                    "ratio_ret_1d": metrics["ratio_ret_1d"],
                    "spike_grade": s_grade,
                    "spike_count_12m": metrics["spike_count_12m"],
                    "strong_health": s_health,
                    "ma90_mult": metrics["ma90_mult"],
                    "backtest_best": bt["best"],
                })
            except Exception as e:
                print(f"candidate failed {strong_sym}/{weak_sym}: {e}")
    # ENTER優先、次にWAIT
    rank = {"ENTER": 0, "WAIT": 1, "SKIP": 2}
    candidates = sorted(
        candidates,
        key=lambda x: (
            rank.get(x["action_new"], 9),
            9 if x["spike_grade"] == "AVOID" else (1 if x["spike_grade"] == "WATCH" else 0),
            -(x["ratio_ret_1d"] if x["ratio_ret_1d"] is not None else -999),
        )
    )[:12]

    payload = {
        "asof": utc_now_str(),
        "positions": pos_out,
        "new_candidates": candidates,
    }

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    text = render_with_claude(payload)
    send_telegram(text)


if __name__ == "__main__":
    main()
