"""本地电池退化数据应用 —— HTTP 服务入口。

运行：
    cd /workspace && python3 -m backend.server
    （或 bash run.sh），默认 http://127.0.0.1:8000

仅使用 Python 标准 + numpy/scipy/flask，全部计算在本地完成。
"""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path

# 允许 `python backend/server.py` 直接运行
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request, send_file, send_from_directory

from backend.engine.datasets import SCENARIOS, TEMPLATE_CSV, rows_to_csv
from backend.engine.session import Session, jsonable

WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"

app = Flask(__name__, static_folder=None)
SESSION = Session.load()
# 本地单用户，但浏览器双击/并发请求仍可能让两个 accept 同时通过状态检查，
# 用一把锁串行化所有会改变会话状态的写操作。
STATE_LOCK = threading.RLock()


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "battery-degradation-local"})


@app.get("/api/scenarios")
def scenarios():
    return jsonify({"scenarios": [
        {"id": sid, "name": s["name"], "description": s["description"],
         "highlights": s.get("highlights", []),
         "n_rows": len(s["rows"]),
         "default_cell": s.get("default_cell")}
        for sid, s in SCENARIOS.items()]})


@app.post("/api/session/scenario/<sid>")
def load_scenario(sid: str):
    try:
        with STATE_LOCK:
            state = SESSION.load_scenario(sid)
        return jsonify(state)
    except KeyError as e:
        return jsonify({"error": "NOT_FOUND", "message": str(e)}), 404


@app.post("/api/session/import")
def import_csv():
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("csv", "")
    filename = body.get("filename", "导入数据")
    if not text.strip():
        return jsonify({"error": "EMPTY", "message": "没有收到 CSV 内容"}), 400
    with STATE_LOCK:
        state = SESSION.import_csv_text(text, filename)
        errors = list(SESSION.import_errors)
    status = 200 if not errors else 422
    return jsonify(state), status


@app.get("/api/session")
def get_session():
    return jsonify(SESSION.state())


@app.post("/api/session/settings")
def update_settings():
    body = request.get_json(force=True, silent=True) or {}
    with STATE_LOCK:
        state = SESSION.update_settings(
            scopes=body.get("scopes"),
            model_options=body.get("model_options"),
            selected_cell=body.get("selected_cell"),
            eol=body.get("eol"))
    return jsonify(state)


@app.post("/api/profile")
def profile():
    body = request.get_json(force=True, silent=True) or {}
    target = body.get("target") or {"kind": "life",
                                    "cell": SESSION.selected_cell}
    n_grid = int(body.get("n_grid", 21))
    return jsonify(SESSION.get_profile(target, n_grid=n_grid))


@app.post("/api/recommend")
def recommend():
    body = request.get_json(force=True, silent=True) or {}
    with STATE_LOCK:
        rec = SESSION.create_recommendation(body)
    if isinstance(rec, dict) and rec.get("error"):
        return jsonify(rec), 409 if rec["error"] == "FIT_UNAVAILABLE" else 400
    return jsonify(rec)


@app.post("/api/recommendations/<rec_id>/accept")
def accept(rec_id: str):
    body = request.get_json(force=True, silent=True) or {}
    with STATE_LOCK:
        out = SESSION.accept_recommendation(rec_id, body.get("override"))
    if isinstance(out, dict) and out.get("error"):
        code = 409 if out["error"] in (
            "BUDGET_EXCEEDED", "ALREADY_ACCEPTED", "SUPERSEDED") else 400
        return jsonify(out), code
    return jsonify(out)


@app.post("/api/recommendations/<rec_id>/reevaluate")
def reevaluate(rec_id: str):
    with STATE_LOCK:
        out = SESSION.reevaluate(rec_id)
    if out.get("error"):
        return jsonify(out), 409
    return jsonify(out)


@app.post("/api/session/reset")
def reset():
    with STATE_LOCK:
        state = SESSION.reset_demo()
    return jsonify(state)


@app.get("/api/session.csv")
def download_session_csv():
    csv_text = rows_to_csv(SESSION.rows)
    buf = io.BytesIO("﻿".encode("utf-8") + csv_text.encode("utf-8"))
    return send_file(buf, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name="session_data.csv")


@app.get("/api/template.csv")
def template():
    buf = io.BytesIO(TEMPLATE_CSV.encode("utf-8"))
    return send_file(buf, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name="degradation_template.csv")


@app.get("/api/scenarios/<sid>/csv")
def scenario_csv(sid: str):
    if sid not in SCENARIOS:
        return jsonify({"error": "NOT_FOUND"}), 404
    csv_text = rows_to_csv(SCENARIOS[sid]["rows"])
    buf = io.BytesIO("﻿".encode("utf-8") + csv_text.encode("utf-8"))
    return send_file(buf, mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name=f"{sid}.csv")


# ---- 静态页面 ----
@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path: str):
    return send_from_directory(WEB_DIR, path)


def main():
    # 启动时若没有状态，预载标准场景，保证首屏可用
    if not SESSION.rows:
        SESSION.load_scenario("standard")
    port = int(__import__("os").environ.get("PORT", "8000"))
    # 默认只绑定回环地址：这是本地单用户工具，数据不出本机。
    # 如需局域网访问，显式设置 HOST=0.0.0.0。
    host = __import__("os").environ.get("HOST", "127.0.0.1")
    print(f"[battery-degradation] http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
