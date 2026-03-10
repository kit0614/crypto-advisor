"""
server.py
ローカルダッシュボードサーバー
起動: python server.py
→ http://localhost:5000 が自動で開く
"""
import json
import logging
import threading
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify
from flask_cors import CORS

from fr_fetcher import fetch_all_fr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# キャッシュ（最後に取得したデータを保持）
_cache = {"data": None, "fetching": False}


# ─── ルート ───────────────────────────────────────
@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/fr")
def api_fr():
    """FRデータ取得 (キャッシュ返却 or 新規取得)"""
    if _cache["data"] is None:
        # 初回: 同期で取得
        _do_fetch()
    return jsonify(_cache["data"])


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """強制再取得（UIの更新ボタン）"""
    if _cache["fetching"]:
        return jsonify({"status": "fetching", "message": "取得中です..."}), 202

    # バックグラウンドで取得開始
    threading.Thread(target=_do_fetch, daemon=True).start()
    return jsonify({"status": "started", "message": "FR取得を開始しました"})


@app.route("/api/status")
def api_status():
    """フェッチ進行状況"""
    return jsonify({
        "fetching": _cache["fetching"],
        "asof": _cache["data"]["asof"] if _cache["data"] else None,
        "opp_count": len(_cache["data"]["opps"]) if _cache["data"] else 0,
    })


def _do_fetch():
    _cache["fetching"] = True
    log.info("=== FR取得開始 ===")
    try:
        data = fetch_all_fr()
        _cache["data"] = data
        log.info(f"=== FR取得完了: {len(data['opps'])} opportunities ===")
    except Exception as e:
        log.error(f"FR取得エラー: {e}")
    finally:
        _cache["fetching"] = False


def open_browser():
    import time
    time.sleep(1.2)
    webbrowser.open("http://localhost:5000")


if __name__ == "__main__":
    print("=" * 50)
    print("  PERP FR ARB Dashboard")
    print("  http://localhost:5000")
    print("  Ctrl+C で停止")
    print("=" * 50)

    # バックグラウンドで初回データ取得
    threading.Thread(target=_do_fetch, daemon=True).start()
    # ブラウザ自動オープン
    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
