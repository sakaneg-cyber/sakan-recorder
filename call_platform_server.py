"""
Sakan Hub — باب استقبال وتحليل المكالمات (Backend)
=====================================================
منصة مركزية واحدة: كل موبايلات السيلز بترفع تسجيلاتها هنا تلقائياً،
والسيرفر يفرّغ + يلخّص + يقيّم كل مكالمة ويربطها بالسيلز والليد.

يشتغل على نفس سيرفر Sakan Hub. المطوّر بيركّبه مرة واحدة.

طريقة التشغيل:
    pip install flask flask-cors requests
    export OPENAI_API_KEY="..."        # للتفريغ (Whisper) + التلخيص/التقييم
    python call_platform_server.py
    # السيرفر يشتغل على المنفذ 8090

نقاط النهاية (Endpoints):
    POST /api/calls/upload   <- أب الموبايل يرفع التسجيل + بياناته هنا
    GET  /api/calls          <- تاب الـ Hub يقرأ قائمة المكالمات
    GET  /api/calls/<id>     <- تفاصيل مكالمة (تفريغ + ملخص + تقييم)
    GET  /api/health         <- فحص الحالة
"""

import os
import json
import sqlite3
import threading
import datetime as dt
from pathlib import Path

from flask import Flask, request, jsonify, g
from flask_cors import CORS
import requests

# ------------------------------------------------------------------ الإعدادات
BASE_DIR      = Path(__file__).resolve().parent
STORAGE_DIR   = BASE_DIR / "call_recordings"        # مكان حفظ ملفات الصوت
DB_PATH       = BASE_DIR / "calls.db"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")   # <-- حطّ المفتاح هنا أو كـ env
UPLOAD_TOKEN   = os.environ.get("UPLOAD_TOKEN", "sakan-secret-token")  # حماية بسيطة للرفع
PORT           = int(os.environ.get("PORT", 8090))

STORAGE_DIR.mkdir(exist_ok=True)

# معايير تقييم مكالمة السيلز (تقدر تعدّلها من هنا)
EVAL_CRITERIA = [
    "الافتتاح والتعريف",
    "اكتشاف احتياج العميل",
    "عرض المنتج والأسعار",
    "التعامل مع الاعتراضات",
    "الإغلاق وتحديد الخطوة التالية",
]

app = Flask(__name__)
CORS(app)   # عشان تاب الـ Hub يقدر يقرأ من المتصفح


# ------------------------------------------------------------------ قاعدة البيانات
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id        TEXT,        -- الموظف (الجهاز مربوط بيه)
            rep_name      TEXT,
            phone_number  TEXT,        -- رقم الطرف التاني
            direction     TEXT,        -- outgoing / incoming
            started_at    TEXT,
            duration_sec  INTEGER,
            audio_path    TEXT,
            status        TEXT DEFAULT 'received',  -- received/processing/done/error
            lead_id       TEXT,        -- الليد في Profit CRM (يتطابق بالرقم)
            transcript    TEXT,
            summary       TEXT,        -- JSON list
            evaluation    TEXT,        -- JSON dict
            overall_score REAL,
            recommendation TEXT,
            created_at    TEXT
        )""")
    con.commit()
    con.close()


# ------------------------------------------------------------------ رفع المكالمة
@app.route("/api/calls/upload", methods=["POST"])
def upload_call():
    # حماية بسيطة: توكن في الهيدر
    if request.headers.get("X-Upload-Token") != UPLOAD_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    audio = request.files.get("audio")
    if not audio:
        return jsonify({"error": "no audio file"}), 400

    meta = {
        "rep_id":       request.form.get("rep_id", ""),
        "rep_name":     request.form.get("rep_name", ""),
        "phone_number": request.form.get("phone_number", ""),
        "direction":    request.form.get("direction", "outgoing"),
        "started_at":   request.form.get("started_at", dt.datetime.now().isoformat()),
        "duration_sec": int(request.form.get("duration_sec", 0) or 0),
    }

    # حفظ ملف الصوت
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_num = "".join(ch for ch in meta["phone_number"] if ch.isdigit()) or "unknown"
    fname = f"{meta['rep_id'] or 'rep'}_{safe_num}_{ts}_{audio.filename}"
    fpath = STORAGE_DIR / fname
    audio.save(fpath)

    con = db()
    cur = con.execute("""
        INSERT INTO calls (rep_id, rep_name, phone_number, direction, started_at,
                           duration_sec, audio_path, status, created_at)
        VALUES (?,?,?,?,?,?,?, 'received', ?)""",
        (meta["rep_id"], meta["rep_name"], meta["phone_number"], meta["direction"],
         meta["started_at"], meta["duration_sec"], str(fpath),
         dt.datetime.now().isoformat()))
    con.commit()
    call_id = cur.lastrowid

    # شغّل التحليل في الخلفية عشان الرد يرجع بسرعة للموبايل
    threading.Thread(target=process_call, args=(call_id,), daemon=True).start()

    return jsonify({"ok": True, "call_id": call_id, "status": "received"}), 201


# ------------------------------------------------------------------ العقل: التحليل
def process_call(call_id):
    """تفريغ -> تلخيص -> تقييم. يشتغل في الخلفية."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.execute("UPDATE calls SET status='processing' WHERE id=?", (call_id,))
        con.commit()
        row = con.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()

        transcript = transcribe(row["audio_path"])
        analysis   = summarize_and_evaluate(transcript, row["rep_name"] or "الموظف")

        con.execute("""
            UPDATE calls SET status='done', transcript=?, summary=?, evaluation=?,
                             overall_score=?, recommendation=? WHERE id=?""",
            (transcript,
             json.dumps(analysis["summary"], ensure_ascii=False),
             json.dumps(analysis["evaluation"], ensure_ascii=False),
             analysis["overall_score"],
             analysis["recommendation"],
             call_id))
        con.commit()

        # (اختياري) دفع النتيجة لـ Profit CRM هنا عبر الـ API بتاعه
        # push_to_crm(row["phone_number"], analysis)

    except Exception as e:
        con.execute("UPDATE calls SET status='error', recommendation=? WHERE id=?",
                    (f"خطأ في التحليل: {e}", call_id))
        con.commit()
    finally:
        con.close()


def transcribe(audio_path):
    """تفريغ عربي عبر Whisper API."""
    if not OPENAI_API_KEY:
        return "[لم يتم ضبط مفتاح التفريغ بعد — أضف OPENAI_API_KEY]"
    with open(audio_path, "rb") as f:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": f},
            data={"model": "whisper-1", "language": "ar"},
            timeout=300,
        )
    r.raise_for_status()
    return r.json().get("text", "")


def summarize_and_evaluate(transcript, rep_name):
    """تلخيص + تقييم عبر نموذج لغوي، ويرجّع JSON منظّم."""
    if not OPENAI_API_KEY or transcript.startswith("["):
        # قيمة افتراضية لو المفتاح مش متضبوط (عشان تختبري الشكل)
        return {
            "summary": ["(مثال) لم يتم التحليل — أضف المفتاح لتفعيل التلخيص الحقيقي"],
            "evaluation": {c: 0 for c in EVAL_CRITERIA},
            "overall_score": 0,
            "recommendation": "أضف OPENAI_API_KEY لتفعيل التقييم.",
        }

    criteria_txt = "، ".join(EVAL_CRITERIA)
    prompt = f"""أنت خبير تقييم مكالمات مبيعات عقارية. حلّل المكالمة التالية للموظف "{rep_name}".
النص:
\"\"\"{transcript}\"\"\"

أرجع JSON فقط بالشكل ده بالظبط:
{{
  "summary": ["نقطة 1","نقطة 2","نقطة 3"],
  "evaluation": {{ {", ".join(f'"{c}": 0' for c in EVAL_CRITERIA)} }},
  "overall_score": 0,
  "recommendation": "توصية للموظف لتحسين الأداء"
}}
كل درجة من 0 لـ 10. overall_score متوسط الدرجات."""

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": prompt}],
              "response_format": {"type": "json_object"},
              "temperature": 0.3},
        timeout=120,
    )
    r.raise_for_status()
    data = json.loads(r.json()["choices"][0]["message"]["content"])
    # ضمان وجود كل المفاتيح
    data.setdefault("summary", [])
    data.setdefault("evaluation", {c: 0 for c in EVAL_CRITERIA})
    if not data.get("overall_score"):
        vals = list(data["evaluation"].values()) or [0]
        data["overall_score"] = round(sum(vals) / len(vals), 1)
    data.setdefault("recommendation", "")
    return data


# ------------------------------------------------------------------ قراءة المكالمات (للـ Hub)
@app.route("/api/calls")
def list_calls():
    rep = request.args.get("rep_id")
    q = "SELECT id, rep_name, phone_number, direction, started_at, duration_sec, " \
        "status, overall_score, lead_id FROM calls"
    params = ()
    if rep:
        q += " WHERE rep_id=?"; params = (rep,)
    q += " ORDER BY id DESC LIMIT 200"
    rows = db().execute(q, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/calls/<int:call_id>")
def call_detail(call_id):
    r = db().execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    if not r:
        return jsonify({"error": "not found"}), 404
    d = dict(r)
    d["summary"]    = json.loads(d["summary"] or "[]")
    d["evaluation"] = json.loads(d["evaluation"] or "{}")
    d.pop("audio_path", None)   # ما نكشفش المسار الداخلي
    return jsonify(d)


@app.route("/api/health")
def health():
    n = db().execute("SELECT COUNT(*) c FROM calls").fetchone()["c"]
    return jsonify({"ok": True, "calls": n,
                    "transcription_ready": bool(OPENAI_API_KEY)})


if __name__ == "__main__":
    init_db()
    print(f"✅ Sakan Call Platform — يعمل على المنفذ {PORT}")
    print(f"   التفريغ {'مفعّل' if OPENAI_API_KEY else 'غير مفعّل (أضف OPENAI_API_KEY)'}")
    app.run(host="0.0.0.0", port=PORT)
