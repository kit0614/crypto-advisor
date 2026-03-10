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

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TOP_N     = int(os.environ.get("TOP_N", "5"))
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


def build_message(data: dict) -> str:
    opps   = data.get("opps", [])
    asof   = data.get("asof", "—")
    statuses = data.get("statuses", {})

    live_ex = [ex for ex, st in statuses.items() if st == "ok"]
    err_ex  = [ex for ex, st in statuses.items() if str(st).startswith("error")]

    filtered = [o for o in opps if o["spread"] >= MIN_SPREAD]
    top = filtered[:TOP_N]

    now_jst = datetime.now(timezone.utc).astimezone(JST)

    lines = []
    lines.append(f"⚡ <b>PERP FR ARB ALERT</b>")
    lines.append(f"🕐 {now_jst.strftime('%Y-%m-%d %H:%M JST')} (毎時45分)")
    lines.append(f"📡 取引所: {len(live_ex)}/{len(statuses)} LIVE")
    if err_ex:
        lines.append(f"⚠️ エラー: {', '.join(err_ex)}")
    lines.append("")

    if not top:
        lines.append(f"📭 スプレッド {MIN_SPREAD:.3f}% 以上の機会なし")
    else:
        lines.append(f"🏆 <b>TOP {len(top)} FR差アービトラージ</b>")
        lines.append("─" * 30)
        for i, o in enumerate(top, 1):
            spread_emoji = "🔴" if o["spread"]>=0.05 else "🟠" if o["spread"]>=0.02 else "🟡" if o["spread"]>=0.01 else "🟢"
            lines.append(f"{spread_emoji} <b>#{i} {o['coin']}</b>  差: <code>{o['spread']:.4f}%</code>")
            lines.append(f"   LONG  {o['longEx']:12s} <code>{fmt_fr(o['longFr'])}</code>")
            lines.append(f"   SHORT {o['shortEx']:12s} <code>{fmt_fr(o['shortFr'])}</code>")
            if len(o.get("rates", {})) > 2:
                others = {ex: fr for ex, fr in o["rates"].items()
                          if ex not in (o["longEx"], o["shortEx"])}
                if others:
                    oth_str = "  ".join(f"{ex}:{fmt_fr(fr)}" for ex, fr in list(others.items())[:3])
                    lines.append(f"   他: <code>{oth_str}</code>")
            lines.append("")

    lines.append(f"合計 {len(filtered)} 機会 (≥{MIN_SPREAD:.3f}%) | 全{len(opps)} pairs")
    return "\n".join(lines)


def fmt_fr(v) -> str:
    if v is None: return "—"
    return f"{'+' if v >= 0 else ''}{v:.4f}%"


def main():
    log.info("=== FR取得開始 ===")
    data = fetch_all_fr()
    log.info(f"取得完了: {len(data['opps'])} opportunities")

    msg = build_message(data)
    print("\n" + "="*50)
    print(msg)
    print("="*50 + "\n")

    ok = send_telegram(msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
