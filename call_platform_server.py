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

from flask import Flask, request, jsonify, g, send_file, Response
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


@app.route("/api/calls/<int:call_id>/audio")
def call_audio(call_id):
    """يقدّم ملف صوت المكالمة عشان نسمعها من المنصة."""
    r = db().execute("SELECT audio_path FROM calls WHERE id=?", (call_id,)).fetchone()
    if not r or not r["audio_path"] or not os.path.exists(r["audio_path"]):
        return jsonify({"error": "audio not found"}), 404
    return send_file(r["audio_path"], mimetype="audio/mpeg")


@app.route("/api/health")
def health():
    n = db().execute("SELECT COUNT(*) c FROM calls").fetchone()["c"]
    return jsonify({"ok": True, "calls": n,
                    "transcription_ready": bool(OPENAI_API_KEY)})


@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html; charset=utf-8")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sales Sakan</title>
<style>
 *{margin:0;padding:0;box-sizing:border-box;font-family:'Segoe UI',Tahoma,Arial,sans-serif}
 body{background:#f4f6fb;color:#1b2440;padding:16px}
 .wrap{max-width:1100px;margin:0 auto}
 .head{background:linear-gradient(135deg,#1e3a8a,#2547b0);color:#fff;border-radius:16px;padding:20px 24px;margin-bottom:16px}
 .head h1{font-size:22px} .head p{font-size:13px;opacity:.9;margin-top:4px}
 .bar{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
 .kpi{background:#fff;border:1px solid #e4e9f2;border-radius:14px;padding:14px 18px;flex:1;min-width:140px}
 .kpi .n{font-size:24px;font-weight:800;color:#1e3a8a} .kpi .l{font-size:12px;color:#64708c;margin-top:3px}
 table{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden}
 th,td{padding:13px 14px;text-align:right;font-size:14px;border-bottom:1px solid #eef1f7}
 th{background:#1a2233;color:#fff;font-size:13px}
 tr:hover td{background:#f6f9ff}
 .tag{padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700}
 .out{background:#e6f0fd;color:#2547b0} .inc{background:#e7f7ee;color:#16a34a}
 .play{background:#1e3a8a;color:#fff;border:none;padding:7px 16px;border-radius:8px;cursor:pointer;font-size:13px}
 .player{position:fixed;bottom:0;left:0;right:0;background:#0f1a3a;color:#fff;padding:12px 20px;display:none;align-items:center;gap:14px}
 .player.show{display:flex} .player b{font-size:14px} .player audio{flex:1}
 .empty{text-align:center;color:#64708c;padding:50px;font-size:15px}
 .refresh{background:#fff;border:1px solid #e4e9f2;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:13px;color:#1e3a8a;font-weight:600}
</style></head><body>
<div class="wrap">
 <div class="head"><h1>Sales Sakan - متابعة المكالمات</h1>
   <p>كل مكالمات فريق السيلز في مكان واحد - اضغطي تشغيل لسماع أي مكالمة</p></div>
 <div class="bar" id="kpis"></div>
 <div style="margin-bottom:10px;text-align:left"><button class="refresh" onclick="load()">تحديث</button></div>
 <table><thead><tr><th>الموظف</th><th>الرقم</th><th>الاتجاه</th><th>المدة</th><th>الوقت</th><th>الحالة</th><th></th></tr></thead>
   <tbody id="rows"><tr><td colspan="7" class="empty">جاري التحميل...</td></tr></tbody></table>
</div>
<div class="player" id="player"><b id="pl-title">-</b><audio id="au" controls></audio>
  <button class="refresh" style="background:#28304a;color:#fff;border:none" onclick="hide()">اغلاق</button></div>
<script>
function fmt(s){s=parseInt(s||0);var m=Math.floor(s/60),x=s%60;return m+':'+(x<10?'0':'')+x}
function dir(d){return d=='incoming'?'<span class="tag inc">وارد</span>':'<span class="tag out">صادر</span>'}
async function load(){
 var rows=document.getElementById('rows');
 try{
  var r=await fetch('/api/calls'); var c=await r.json();
  document.getElementById('kpis').innerHTML=
    '<div class="kpi"><div class="n">'+c.length+'</div><div class="l">اجمالي المكالمات</div></div>'+
    '<div class="kpi"><div class="n">'+new Set(c.map(function(x){return x.rep_name})).size+'</div><div class="l">عدد الموظفين</div></div>'+
    '<div class="kpi"><div class="n">'+c.filter(function(x){return x.direction=='incoming'}).length+'</div><div class="l">مكالمات واردة</div></div>';
  if(!c.length){rows.innerHTML='<tr><td colspan="7" class="empty">لسه مفيش مكالمات - اعملي مكالمة تجريبية</td></tr>';return}
  rows.innerHTML=c.map(function(x){return '<tr><td>'+(x.rep_name||'-')+'</td><td>'+(x.phone_number||'-')+'</td><td>'+dir(x.direction)+'</td><td>'+fmt(x.duration_sec)+'</td><td>'+(x.started_at||'').replace('T',' ').slice(0,16)+'</td><td>'+(x.status||'')+'</td><td><button class="play" onclick="play('+x.id+')">تشغيل</button></td></tr>'}).join('');
 }catch(e){rows.innerHTML='<tr><td colspan="7" class="empty">تعذر الاتصال بالسيرفر</td></tr>'}
}
function play(id){
 var p=document.getElementById('player'),a=document.getElementById('au');
 document.getElementById('pl-title').textContent='تشغيل مكالمة رقم '+id;
 a.src='/api/calls/'+id+'/audio'; p.classList.add('show'); a.play().catch(function(){});
}
function hide(){var a=document.getElementById('au');a.pause();document.getElementById('player').classList.remove('show')}
load();
</script></body></html>"""


if __name__ == "__main__":
    init_db()
    print("Sakan Call Platform running on port", PORT)
    app.run(host="0.0.0.0", port=PORT)
