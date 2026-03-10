"""
fr_fetcher.py
全取引所のFunding Rate を取得して統一フォーマットで返す
戻り値: { "exchange_name": { "SYMBOL": fr_1h_percent, ... }, ... }
"""
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
import requests

log = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))
X18 = 10 ** 18
TIMEOUT = 20
HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (FR-ARB-Dashboard)",
}


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def x18_to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(int(str(x).strip())) / X18
    except Exception:
        return None


def get(url: str, params=None) -> Any:
    r = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def post(url: str, payload: dict) -> Any:
    r = requests.post(url, headers=HEADERS, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def normalize_symbol(s: Any) -> Optional[str]:
    if not s:
        return None
    import re
    s = str(s).upper()
    s = re.sub(r'[-_]?(USD[TC]?|PERP|USDC\.E|_UMCBL)$', '', s)
    return s.strip() or None


# ─────────────────────────────────────────
# Variational
# ─────────────────────────────────────────
def fetch_variational() -> Dict[str, float]:
    """annual decimal → 1h% = decimal*100 / (365*24)"""
    BASE = "https://omni-client-api.prod.ap-northeast-1.variational.io"
    try:
        data = get(f"{BASE}/metadata/stats")
        result = {}
        for m in data.get("listings", []):
            sym = normalize_symbol(m.get("ticker"))
            fr_dec = to_float(m.get("funding_rate"))
            if sym and fr_dec is not None:
                result[sym] = (fr_dec * 100.0) / (365 * 24)
        log.info(f"Variational: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"Variational error: {e}")
        return {}


# ─────────────────────────────────────────
# Lighter
# ─────────────────────────────────────────
def fetch_lighter() -> Dict[str, float]:
    """8h interval decimal → 1h% = (rate/8)*100"""
    FUNDING_URL = "https://mainnet.zklighter.elliot.ai/api/v1/funding-rates"
    INTERVAL = 8
    try:
        js = get(FUNDING_URL)
        rows = js if isinstance(js, list) else (
            js.get("funding_rates") or js.get("data") or js.get("results") or []
        )
        result = {}
        for x in rows:
            if not isinstance(x, dict):
                continue
            ex = (x.get("exchange") or "").lower()
            if ex and ex != "lighter":
                continue
            sym = normalize_symbol(x.get("symbol") or x.get("ticker") or x.get("name"))
            rate = to_float(x.get("rate"))
            if sym and rate is not None:
                result[sym] = (rate / INTERVAL) * 100.0
        log.info(f"Lighter: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"Lighter error: {e}")
        return {}


# ─────────────────────────────────────────
# NADO
# ─────────────────────────────────────────
def fetch_nado() -> Dict[str, float]:
    """x18 24h decimal → 1h% = (fr_24h_dec*100)/24"""
    GATEWAY = "https://gateway.prod.nado.xyz/v1/query"
    ARCHIVE = "https://archive.prod.nado.xyz/v1"
    try:
        prod_js = get(GATEWAY, {"type": "all_products"})
        sym_js  = get(GATEWAY, {"type": "symbols", "product_type": "perp"})

        perp_products = prod_js.get("data", {}).get("perp_products", [])
        ids = [int(p["product_id"]) for p in perp_products if p.get("product_id")]

        pid_to_sym = {}
        for v in (sym_js.get("data", {}).get("symbols") or {}).values():
            if str(v.get("type", "")).lower() == "perp":
                try:
                    pid_to_sym[int(v["product_id"])] = v["symbol"]
                except Exception:
                    pass

        if not ids:
            return {}

        fr_js = post(ARCHIVE, {"funding_rates": {"product_ids": ids}})

        result = {}
        for pid in ids:
            f = fr_js.get(str(pid), {})
            fr_24h = x18_to_float(f.get("funding_rate_x18"))
            if fr_24h is None:
                continue
            sym = normalize_symbol(pid_to_sym.get(pid, f"PID-{pid}"))
            if sym:
                result[sym] = (fr_24h * 100.0) / 24.0
        log.info(f"NADO: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"NADO error: {e}")
        return {}


# ─────────────────────────────────────────
# Extended
# ─────────────────────────────────────────
def fetch_extended() -> Dict[str, float]:
    """1h decimal → 1h% = decimal*100"""
    URL = "https://api.starknet.extended.exchange/api/v1/info/markets"
    try:
        js = get(URL)
        result = {}
        for m in (js.get("data") or []):
            sym = normalize_symbol(m.get("name"))
            fr  = to_float((m.get("marketStats") or {}).get("fundingRate"))
            if sym and fr is not None:
                result[sym] = fr * 100.0
        log.info(f"Extended: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"Extended error: {e}")
        return {}


# ─────────────────────────────────────────
# GRVT
# ─────────────────────────────────────────
def fetch_grvt() -> Dict[str, float]:
    """fr2 / funding_interval_hours = 1h%"""
    BASE = "https://market-data.grvt.io"

    def _walk(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from _walk(v)
        elif isinstance(obj, list):
            for it in obj:
                yield from _walk(it)

    try:
        inst_js = post(f"{BASE}/lite/v1/all_instruments", {})
        perps, seen = [], set()
        for d in _walk(inst_js.get("data") or inst_js.get("result") or inst_js):
            kind = str(d.get("kind") or d.get("k") or d.get("instrument_kind") or "").upper()
            if "PERPETUAL" not in kind and kind != "PERP":
                continue
            inst = d.get("instrument") or d.get("i") or d.get("name") or d.get("n")
            if not inst or inst in seen:
                continue
            seen.add(inst)
            fi = to_float(d.get("fi") or d.get("funding_interval_hours"))
            perps.append({"instrument": inst, "fi": fi})

        result = {}
        for p in perps[:80]:  # rate-limit guard
            try:
                t_js = post(f"{BASE}/lite/v1/ticker", {"i": p["instrument"]})
                fr2 = None
                for d in _walk(t_js.get("data") or t_js.get("result") or t_js):
                    v = d.get("fr2") or d.get("fundingRate") or d.get("funding_rate")
                    if v is not None:
                        fr2 = to_float(v)
                        break
                fi = p["fi"]
                if fr2 is not None and fi and fi > 0:
                    sym = normalize_symbol(p["instrument"].split("_")[0])
                    if sym:
                        result[sym] = fr2 / fi
                time.sleep(0.03)
            except Exception:
                pass

        log.info(f"GRVT: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"GRVT error: {e}")
        return {}


# ─────────────────────────────────────────
# Ethereal
# ─────────────────────────────────────────
def fetch_ethereal() -> Dict[str, float]:
    """fundingRate1h decimal → 1h% = decimal*100"""
    URL = "https://api.ethereal.trade/v1/product"
    try:
        js = get(URL, {"order": "asc", "orderBy": "createdAt"})
        result = {}
        for p in (js.get("data") or []):
            sym = normalize_symbol(p.get("displayTicker") or p.get("ticker"))
            fr  = to_float(p.get("fundingRate1h"))
            if sym and fr is not None:
                result[sym] = fr * 100.0
        log.info(f"Ethereal: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"Ethereal error: {e}")
        return {}


# ─────────────────────────────────────────
# 01Exchange
# ─────────────────────────────────────────
def fetch_01exchange() -> Dict[str, float]:
    """perpStats.funding_rate 1h decimal → 1h%"""
    BASE = "https://zo-mainnet.n1.xyz"
    try:
        info = get(f"{BASE}/info")
        markets = info.get("markets", [])
        result = {}
        for m in markets[:60]:
            mid = m.get("marketId")
            if not mid:
                continue
            try:
                stats = get(f"{BASE}/market/{mid}/stats")
                fr = to_float((stats.get("perpStats") or {}).get("funding_rate"))
                if fr is None:
                    continue
                sym = normalize_symbol(m.get("symbol"))
                if sym:
                    result[sym] = fr * 100.0
                time.sleep(0.03)
            except Exception:
                pass
        log.info(f"01Exchange: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"01Exchange error: {e}")
        return {}


# ─────────────────────────────────────────
# Pacifica
# ─────────────────────────────────────────
def fetch_pacifica() -> Dict[str, float]:
    """funding / next_funding 1h decimal → 1h%"""
    URL = "https://api.pacifica.fi/api/v1/info/prices"
    try:
        js = get(URL)
        if not js.get("success"):
            return {}
        result = {}
        for d in (js.get("data") or []):
            sym = normalize_symbol(d.get("symbol"))
            fr  = to_float(d.get("funding") if d.get("funding") is not None else d.get("next_funding"))
            if sym and fr is not None:
                result[sym] = fr * 100.0
        log.info(f"Pacifica: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"Pacifica error: {e}")
        return {}


# ─────────────────────────────────────────
# Hyperliquid
# ─────────────────────────────────────────
def fetch_hyperliquid() -> Dict[str, float]:
    """funding decimal → 1h%"""
    try:
        js = post("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
        universe, ctxs = js[0]["universe"], js[1]
        result = {}
        for asset, ctx in zip(universe, ctxs):
            fr = to_float(ctx.get("funding"))
            if fr is not None:
                result[normalize_symbol(asset["name"])] = fr * 100.0
        log.info(f"Hyperliquid: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"Hyperliquid error: {e}")
        return {}


# ─────────────────────────────────────────
# Bitget
# ─────────────────────────────────────────
def fetch_bitget() -> Dict[str, float]:
    """fundingRate は8時間分のdecimal → 1h% = decimal * 100 / 8"""
    INTERVAL_H = 8
    try:
        js = get("https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES")
        result = {}
        for item in (js.get("data") or []):
            sym = normalize_symbol(item.get("symbol"))
            fr  = to_float(item.get("fundingRate"))
            if sym and fr is not None:
                result[sym] = fr * 100.0 / INTERVAL_H
        log.info(f"Bitget: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"Bitget error: {e}")
        return {}


# ─────────────────────────────────────────
# Paradex
# ─────────────────────────────────────────
def fetch_paradex() -> Dict[str, float]:
    """Paradex public market summary API → 1h FR%"""
    try:
        js = get("https://api.prod.paradex.trade/v1/markets/summary")
        result = {}
        for m in (js.get("results") or []):
            sym = normalize_symbol(m.get("market"))
            # funding_rate is 8h rate
            fr_8h = to_float(m.get("funding_rate"))
            if sym and fr_8h is not None:
                result[sym] = (fr_8h / 8.0) * 100.0
        log.info(f"Paradex: {len(result)} pairs")
        return result
    except Exception as e:
        log.warning(f"Paradex error: {e}")
        return {}


# ─────────────────────────────────────────
# Master fetch + opportunity computation
# ─────────────────────────────────────────
FETCHERS = {
    "Variational": fetch_variational,
    "Lighter":     fetch_lighter,
    "NADO":        fetch_nado,
    "Extended":    fetch_extended,
    "GRVT":        fetch_grvt,
    "Ethereal":    fetch_ethereal,
    "01Exchange":  fetch_01exchange,
    "Pacifica":    fetch_pacifica,
    "Hyperliquid": fetch_hyperliquid,
    "Bitget":      fetch_bitget,
    "Paradex":     fetch_paradex,
}


def fetch_all_fr(exchanges=None) -> dict:
    """全取引所FRを取得してopportunity付きで返す"""
    targets = {k: v for k, v in FETCHERS.items() if not exchanges or k in exchanges}
    ex_data = {}
    statuses = {}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fn): name for name, fn in targets.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                data = fut.result()
                ex_data[name] = data
                statuses[name] = "ok" if data else "empty"
            except Exception as e:
                ex_data[name] = {}
                statuses[name] = f"error: {e}"

    opps = compute_opportunities(ex_data)
    now_jst = datetime.now(timezone.utc).astimezone(JST)

    return {
        "asof": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "exchanges": list(targets.keys()),
        "exData": ex_data,
        "statuses": statuses,
        "opps": opps,
    }


def compute_opportunities(ex_data: dict, min_spread: float = 0) -> list:
    all_coins = set()
    for d in ex_data.values():
        all_coins.update(d.keys())

    opps = []
    for coin in all_coins:
        rates = {
            ex: fr for ex, d in ex_data.items()
            if (fr := d.get(coin)) is not None and isinstance(fr, float)
        }
        if len(rates) < 2:
            continue

        rate_list = list(rates.items())
        best_spread = -1
        short_ex = long_ex = None

        for i, (ex_i, fr_i) in enumerate(rate_list):
            for j, (ex_j, fr_j) in enumerate(rate_list):
                if i == j:
                    continue
                spread = fr_i - fr_j  # short ex_i (高FR), long ex_j (低FR)
                if spread > best_spread:
                    best_spread = spread
                    short_ex = (ex_i, fr_i)
                    long_ex  = (ex_j, fr_j)

        if best_spread >= min_spread and short_ex and long_ex:
            opps.append({
                "coin":    coin,
                "spread":  round(best_spread, 6),
                "shortEx": short_ex[0],
                "shortFr": round(short_ex[1], 6),
                "longEx":  long_ex[0],
                "longFr":  round(long_ex[1], 6),
                "rates":   {ex: round(fr, 6) for ex, fr in rates.items()},
            })

    opps.sort(key=lambda x: -x["spread"])
    return opps
