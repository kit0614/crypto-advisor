"""
notify_telegram.py
毎時45分にGitHub Actionsで実行。
ベストFR差をTelegramに通知する。

環境変数:
  TELEGRAM_BOT_TOKEN  - BotのAPIトークン
  TELEGRAM_CHAT_ID    - 通知先のChat ID
  TOP_N               - 上位何件を通知するか (default: 5)
  MIN_SPREAD          - 最小スプレッド% (default: 0.005)
"""
import os
import sys
import logging
from datetime import datetime, timezone, timedelta

import requests

from fr_fetcher import fetch_all_fr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N      = int(os.environ.get("TOP_N", "5"))
MIN_SPREAD = float(os.environ.get("MIN_SPREAD", "0.005"))


def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID が未設定です")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    res = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    if res.ok:
        log.info("Telegram送信成功")
        return True
    log.error(f"Telegram送信失敗: {res.text}")
    return False


def fmt_fr(v) -> str:
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else ''}{v:.4f}%"


def build_per_exchange_best(opps: list) -> dict:
    """
    各取引所ごとに「自分が絡む最大スプレッド機会」を返す
    戻り値: { "Bitget": {"coin":"BTC","spread":0.01,"role":"LONG","counter":"Hyperliquid","myFr":...,"counterFr":...}, ... }
    """
    best: dict = {}
    for o in opps:
        for ex in set([o["longEx"], o["shortEx"]]):
            if ex not in best or o["spread"] > best[ex]["spread"]:
                role = "LONG" if ex == o["longEx"] else "SHORT"
                counter = o["shortEx"] if role == "LONG" else o["longEx"]
                my_fr  = o["longFr"] if role == "LONG" else o["shortFr"]
                ctr_fr = o["shortFr"] if role == "LONG" else o["longFr"]
                best[ex] = {
                    "coin":      o["coin"],
                    "spread":    o["spread"],
                    "role":      role,
                    "counter":   counter,
                    "myFr":      my_fr,
                    "counterFr": ctr_fr,
                }
    return best


def build_message(data: dict) -> str:
    opps     = data.get("opps", [])
    statuses = data.get("statuses", {})

    live_ex = [ex for ex, st in statuses.items() if st == "ok"]
    err_ex  = [ex for ex, st in statuses.items() if str(st).startswith("error")]

    filtered = [o for o in opps if o["spread"] >= MIN_SPREAD]
    top      = filtered[:TOP_N]

    now_jst = datetime.now(timezone.utc).astimezone(JST)

    lines = []

    # ── ヘッダー ──
    lines.append("⚡ <b>PERP FR ARB ALERT</b>")
    lines.append(f"🕐 {now_jst.strftime('%Y-%m-%d %H:%M JST')}")
    lines.append(f"📡 取引所: {len(live_ex)}/{len(statuses)} LIVE")
    if err_ex:
        lines.append(f"⚠️ エラー: {', '.join(err_ex)}")
    lines.append("")

    # ── TOP N 全体ランキング ──
    if not top:
        lines.append(f"📭 スプレッド {MIN_SPREAD:.3f}% 以上の機会なし")
    else:
        lines.append(f"🏆 <b>TOP {len(top)} FR差アービトラージ</b>")
        lines.append("─" * 28)
        for i, o in enumerate(top, 1):
            emoji = "🔴" if o["spread"] >= 0.05 else "🟠" if o["spread"] >= 0.02 else "🟡" if o["spread"] >= 0.01 else "🟢"
            lines.append(f"{emoji} <b>#{i} {o['coin']}</b>  差:<code>{o['spread']:.4f}%</code>")
            lines.append(f"   LONG  {o['longEx']:12s} <code>{fmt_fr(o['longFr'])}</code>")
            lines.append(f"   SHORT {o['shortEx']:12s} <code>{fmt_fr(o['shortFr'])}</code>")
            # 他取引所のFRも最大3件表示
            others = {ex: fr for ex, fr in o.get("rates", {}).items()
                      if ex not in (o["longEx"], o["shortEx"])}
            if others:
                oth_str = "  ".join(
                    f"{ex}:<code>{fmt_fr(fr)}</code>"
                    for ex, fr in list(others.items())[:3]
                )
                lines.append(f"   他: {oth_str}")
            lines.append("")

    # ── 取引所別ベスト機会 ──
    per_ex = build_per_exchange_best(filtered if filtered else opps)

    if per_ex:
        lines.append("─" * 28)
        lines.append("📊 <b>取引所別 ベスト機会</b>")
        lines.append("─" * 28)

        # スプレッド降順で並べる
        sorted_ex = sorted(per_ex.items(), key=lambda x: -x[1]["spread"])
        for ex, b in sorted_ex:
            emoji = "🔴" if b["spread"] >= 0.05 else "🟠" if b["spread"] >= 0.02 else "🟡" if b["spread"] >= 0.01 else "🟢"
            role_arrow = "📈" if b["role"] == "LONG" else "📉"
            lines.append(
                f"{emoji} <b>{ex}</b> × {b['counter']}"
            )
            lines.append(
                f"   {role_arrow}{b['role']} <b>{b['coin']}</b>  差:<code>{b['spread']:.4f}%</code>"
            )
            lines.append(
                f"   自:<code>{fmt_fr(b['myFr'])}</code>  相手:<code>{fmt_fr(b['counterFr'])}</code>"
            )
        lines.append("")

    # ── フッター ──
    lines.append(f"合計 {len(filtered)} 機会 (≥{MIN_SPREAD:.3f}%) | 全{len(opps)} pairs")

    return "\n".join(lines)


def main():
    log.info("=== FR取得開始 ===")
    data = fetch_all_fr()
    log.info(f"取得完了: {len(data['opps'])} opportunities")

    msg = build_message(data)
    print("\n" + "=" * 50)
    print(msg)
    print("=" * 50 + "\n")

    ok = send_telegram(msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
