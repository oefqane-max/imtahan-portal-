"""
ONLAYN İMTAHAN PORTALI  (Python + Streamlit)
=============================================

Quraşdırma:
    pip install "streamlit>=1.37" streamlit-javascript pymupdf pandas

İşə salma:
    streamlit run app.py

Qeydlər:
  * Bütün məlumatlar server yaddaşında (st.cache_resource) saxlanılır – xarici baza lazım deyil.
    Bu yaddaş bütün brauzer sessiyaları arasında ortaqdır, ona görə müəllim şagirdlərin
    nəticələrini öz panelində canlı görür. Server yenidən başladılarsa, məlumatlar silinir.
  * Müəllim şifrəsi: saytda (Streamlit Cloud) Settings → Secrets bölməsinə
        TEACHER_PASSWORD = "sizin_şifrəniz"
    yazılır. Secrets yoxdursa, koddakı TEACHER_PASSWORD (12345) istifadə olunur.
  * PDF faylları səhifə-səhifə şəklə çevrilərək göstərilir (pymupdf). pymupdf yoxdursa,
    PDF brauzerin daxili oxuyucusu ilə göstərilməyə cəhd edilir.
"""

import base64
import json
import re
import threading
import time
from datetime import datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

try:
    from streamlit_javascript import st_javascript

    HAS_STJS = True
except ImportError:  # kitabxana yoxdursa tətbiq yenə işləyir, amma köçürmə izlənmir
    HAS_STJS = False

try:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # köhnə pymupdf versiyaları

    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Baku")
except Exception:  # Windows-da tzdata olmaya bilər
    TZ = None

# ----------------------------------------------------------------------------
# PARAMETRLƏR
# ----------------------------------------------------------------------------
TEACHER_PASSWORD = "12345"  # yalnız öz kompüterdə istifadə üçün; saytda Secrets-dən oxunur


def get_teacher_password():
    """Saytda (Streamlit Cloud) şifrə 'Secrets' bölməsindən oxunur: TEACHER_PASSWORD = "..." """
    try:
        value = st.secrets["TEACHER_PASSWORD"]
        if str(value).strip():
            return str(value).strip()
    except Exception:
        pass
    return TEACHER_PASSWORD
OPTIONS = ["A", "B", "C", "D", "E"]
POLL_SECONDS = 3          # taymer yoxlaması və köçürmə loqlarının oxunma intervalı
WARNING_SHOW_SECONDS = 20  # Streamlit daxilindəki qırmızı xəbərdarlığın görünmə müddəti

STATUS_LABELS = {
    "unused": "İstifadə olunmayıb",
    "in_progress": "İmtahandadır",
    "finished": "Bitirib",
}

st.set_page_config(page_title="Onlayn İmtahan Portalı", page_icon="📝", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.6rem; padding-bottom: 1rem;}
      div[role="radiogroup"] label p {font-size: 1.05rem; font-weight: 600;}
      .cheat-box {
          background:#b00020; color:#fff; padding:18px 22px; border-radius:12px;
          font-size:1.35rem; font-weight:800; text-align:center; margin:6px 0 12px 0;
          box-shadow:0 4px 18px rgba(176,0,32,.35);
      }
      .role-card {
          border:1px solid rgba(128,128,128,.35); border-radius:16px; padding:26px;
          text-align:center; min-height:150px;
      }
      .role-card h2 {margin:0 0 6px 0;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------
# ORTAQ YADDAŞ (bütün istifadəçilər üçün eyni)
# ----------------------------------------------------------------------------
@st.cache_resource
def get_store():
    return {
        "lock": threading.RLock(),
        "exam": None,      # aktiv imtahan
        "codes": {},       # kod -> {name, status, started_at, deadline, exam_id, answers}
        "results": {},     # kod -> nəticə
        "logs": [],        # köçürmə loqları
        "log_ids": set(),  # təkrar yazılmanın qarşısını almaq üçün
    }


STORE = get_store()
LOCK = STORE["lock"]


# ----------------------------------------------------------------------------
# KÖMƏKÇİ FUNKSİYALAR
# ----------------------------------------------------------------------------
def fmt_time(ts, with_date=False):
    if not ts:
        return ""
    fmt = "%d.%m.%Y %H:%M:%S" if with_date else "%H:%M:%S"
    return datetime.fromtimestamp(ts, TZ).strftime(fmt)


KEY_PAIR_RE = re.compile(
    r"(?:^|(?<=[,;\s]))(\d+)\s*[-–—:=).]\s*(.+?)\s*(?=[,;\s]+\d+\s*[-–—:=).]|[,;\s]*$)",
    re.S,
)


def is_choice(value):
    """Cavab açarındakı dəyər A–E hərfidirsə, sual variantlıdır; əks halda açıq cavablıdır."""
    return isinstance(value, str) and value.strip().upper() in OPTIONS


def normalize_answer(value):
    v = str(value or "").strip().lower().replace(" ", "").replace(",", ".")
    v = v.rstrip("%").rstrip(".")
    return v


def answers_match(given, correct):
    if not given:
        return False
    if is_choice(correct):
        return str(given).strip().upper() == correct.strip().upper()
    g, c = normalize_answer(given), normalize_answer(correct)
    try:
        return abs(float(g) - float(c)) < 1e-9
    except ValueError:
        return g == c


def parse_answer_key(text, n):
    """'1-A, 2-C, 16-4, 22-46%' və ya 'ACB...' formatını oxuyur.
    A–E hərfi olan suallar variantlı, qalanları (rəqəm/mətn) açıq cavablı sayılır."""
    text = (text or "").strip()
    key = {}
    for m in KEY_PAIR_RE.finditer(text):
        value = m.group(2).strip().rstrip(",;").strip()
        if value:
            key[int(m.group(1))] = value.upper() if is_choice(value) else value
    if not key:
        letters = re.findall(r"[A-E]", text.upper())
        key = {i + 1: letter for i, letter in enumerate(letters)}
    answer_list = [key.get(i) for i in range(1, n + 1)]
    missing = [i for i in range(1, n + 1) if i not in key]
    extra = sorted(i for i in key if i > n)
    return answer_list, missing, extra


def parse_codes(text):
    """Hər sətirdə bir kod; istəyə görə vergül/nöqtəli vergüldən sonra şagirdin adı."""
    out, bad = {}, []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[,;\t]", line, maxsplit=1)
        code = parts[0].strip().upper()
        name = parts[1].strip() if len(parts) > 1 else ""
        if re.fullmatch(r"[A-Z0-9_-]{3,20}", code):
            out[code] = name
        else:
            bad.append(line)
    return out, bad


@st.cache_data(show_spinner="PDF hazırlanır...")
def pdf_to_images(data: bytes):
    doc = fitz.open(stream=data, filetype="pdf")
    pages = [page.get_pixmap(dpi=120).tobytes("png") for page in doc]
    doc.close()
    return pages


def violation_events(code):
    with LOCK:
        return [l for l in STORE["logs"] if l["code"] == code and l["kind"] != "return"]


def reset_student_session():
    for k in list(st.session_state.keys()):
        if k.startswith(("stu_", "ans_", "guard_")) or k in ("seen_events", "warn_until", "confirm_finish"):
            del st.session_state[k]


def html_iframe(html, height):
    """HTML+JS-i iframe-də göstərir (yeni Streamlit-də st.iframe, köhnədə components.html)."""
    if hasattr(st, "iframe"):
        st.iframe(html, height=max(int(height), 1))
    else:
        components.html(html, height=height)


def deactivate_guard():
    """İmtahan bitəndə brauzerdəki izləməni söndürür və xəbərdarlıq pəncərəsini silir."""
    html_iframe(
        """<script>
        try {
          window.parent.__examActive = false;
          const o = window.parent.document.getElementById('exam-cheat-overlay');
          if (o) o.remove();
        } catch (e) {}
        </script>""",
        height=0,
    )


# ----------------------------------------------------------------------------
# KÖÇÜRMƏ İZLƏMƏ (JavaScript)
# ----------------------------------------------------------------------------
# Bu skript əsas Streamlit səhifəsinin (window.parent) içinə yerləşdirilir və
# visibilitychange / blur / focus hadisələrini dinləyir.
PARENT_GUARD_SCRIPT = r"""
(function () {
  const w = window, d = document;
  w.__examEvents = w.__examEvents || [];
  w.__examSeq = w.__examSeq || 0;
  let awaySince = null;

  function push(kind, extra) {
    w.__examSeq += 1;
    const ev = Object.assign(
      { id: (w.__examCode || '') + '-' + w.__examSeq + '-' + Date.now(), kind: kind, ts: Date.now() },
      extra || {}
    );
    w.__examEvents.push(ev);
  }

  function showOverlay() {
    let o = d.getElementById('exam-cheat-overlay');
    if (!o) {
      o = d.createElement('div');
      o.id = 'exam-cheat-overlay';
      o.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:rgba(176,0,32,0.95);' +
        'display:flex;align-items:center;justify-content:center;flex-direction:column;color:#fff;' +
        'font-family:system-ui,sans-serif;text-align:center;padding:24px;';
      o.innerHTML =
        '<div style="font-size:72px;line-height:1">⚠️</div>' +
        '<div style="font-size:clamp(24px,4.2vw,46px);font-weight:900;max-width:950px;line-height:1.25;margin-top:12px">' +
        'Diqqət! Səhifədən çıxmaq qadağandır. Bu hal müəllimə bildiriləcək!</div>' +
        '<div id="exam-cheat-count" style="margin-top:18px;font-size:20px;opacity:.95"></div>' +
        '<button id="exam-cheat-btn" style="margin-top:26px;padding:14px 30px;font-size:18px;font-weight:700;' +
        'border:none;border-radius:10px;background:#fff;color:#b00020;cursor:pointer">' +
        'Başa düşdüm, imtahana qayıt</button>';
      d.body.appendChild(o);
      o.querySelector('#exam-cheat-btn').onclick = function () { o.style.display = 'none'; };
    }
    const cnt = w.__examEvents.filter(function (e) { return e.kind !== 'return'; }).length;
    o.querySelector('#exam-cheat-count').textContent = 'Qeydə alınan səhifədən çıxış sayı: ' + cnt;
    o.style.display = 'flex';
  }

  function leave(kind) {
    if (!w.__examActive || awaySince !== null) return;
    awaySince = Date.now();
    push(kind);
    showOverlay();
  }

  function back() {
    if (awaySince === null) return;
    const secs = Math.round((Date.now() - awaySince) / 1000);
    awaySince = null;
    if (w.__examActive) push('return', { away: secs });
  }

  d.addEventListener('visibilitychange', function () {
    if (d.hidden) leave('hidden'); else back();
  });

  w.addEventListener('blur', function () {
    setTimeout(function () {
      // Səhifənin öz daxili iframe-lərinə (taymer və s.) klik köçürmə sayılmır
      const ae = d.activeElement;
      if (ae && ae.tagName === 'IFRAME') return;
      if (d.hasFocus()) return;
      leave('blur');
    }, 150);
  });

  w.addEventListener('focus', back);

  w.addEventListener('beforeunload', function (e) {
    if (w.__examActive) { e.preventDefault(); e.returnValue = ''; }
  });
})();
"""


def build_poll_js(code, tick):
    # Kod "(" ilə başlamalıdır: st_javascript onu ifadə kimi hesablayır.
    return (
        "(() => {"
        "  const w = window.parent; const d = w.document;"
        f"  const CODE = {json.dumps(code)};"
        "  if (w.__examCode !== CODE) { w.__examCode = CODE; w.__examEvents = []; w.__examSeq = 0; }"
        "  w.__examActive = true;"
        "  if (!w.__examGuardInstalled) {"
        "    w.__examGuardInstalled = true;"
        "    const s = d.createElement('script');"
        f"    s.textContent = {json.dumps(PARENT_GUARD_SCRIPT)};"
        "    d.head.appendChild(s);"
        "  }"
        "  return JSON.stringify(w.__examEvents || []);"
        f"}})() /* tick {tick} */"
    )


def record_events(code, events):
    """Brauzerdən gələn hadisələri ortaq loqa yazır. Yeni pozuntu varsa True qaytarır."""
    new_violation = False
    with LOCK:
        rec = STORE["codes"].get(code)
        if not rec or rec["status"] != "in_progress":
            return False
        for ev in events:
            ev_id = str(ev.get("id", ""))
            if not ev_id or ev_id in STORE["log_ids"]:
                continue
            STORE["log_ids"].add(ev_id)
            ts = float(ev.get("ts", time.time() * 1000)) / 1000.0
            minute = max(1, int((ts - rec["started_at"]) // 60) + 1)
            kind = ev.get("kind")
            if kind == "hidden":
                text = "Köçürmə cəhdi / Səhifədən çıxış: başqa tab-a keçid və ya brauzer kiçildildi"
                new_violation = True
            elif kind == "blur":
                text = "Köçürmə cəhdi / Səhifədən çıxış: pəncərədən çıxış (başqa proqram/pəncərə)"
                new_violation = True
            elif kind == "return":
                text = f"Səhifəyə qayıtdı ({int(ev.get('away', 0))} saniyə kənarda qaldı)"
            else:
                continue
            STORE["logs"].append(
                {
                    "id": ev_id,
                    "code": code,
                    "name": rec.get("name", ""),
                    "ts": ts,
                    "minute": minute,
                    "kind": kind,
                    "text": text,
                }
            )
    return new_violation


# ----------------------------------------------------------------------------
# İMTAHANIN YEKUNLAŞDIRILMASI
# ----------------------------------------------------------------------------
def finalize_exam(code, reason):
    ss = st.session_state
    with LOCK:
        exam = STORE["exam"]
        rec = STORE["codes"].get(code)
        if rec is None or exam is None:
            return
        if rec["status"] == "finished":
            ss.stu_result = STORE["results"].get(code)
            ss.stu_finished = True
            return

        n = exam["num_questions"]
        answers = [(str(ss.get(f"ans_{code}_{i}") or "").strip() or None) for i in range(1, n + 1)]
        key = exam["answer_key"]
        correct = sum(1 for a, k in zip(answers, key) if answers_match(a, k))
        blank = sum(1 for a in answers if not a)
        wrong = n - correct - blank
        score = round(correct * 100 / n, 2)
        now = time.time()

        rec["status"] = "finished"
        rec["answers"] = answers
        result = {
            "code": code,
            "name": rec.get("name", ""),
            "correct": correct,
            "wrong": wrong,
            "blank": blank,
            "total": n,
            "score": score,
            "answers": answers,
            "started_at": rec["started_at"],
            "finished_at": min(now, rec["deadline"]) if "Vaxt" in reason else now,
            "reason": reason,
        }
        STORE["results"][code] = result

    ss.stu_result = result
    ss.stu_finished = True
    ss.confirm_finish = False


# ----------------------------------------------------------------------------
# GİRİŞ / ROL SEÇİMİ
# ----------------------------------------------------------------------------
def role_selection():
    st.markdown("<h1 style='text-align:center'>📝 Onlayn İmtahan Portalı</h1>", unsafe_allow_html=True)
    st.markdown(
        "<p style='text-align:center;opacity:.75'>Davam etmək üçün rolunuzu seçin</p>",
        unsafe_allow_html=True,
    )
    st.write("")
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown(
            "<div class='role-card'><h2>👨‍🏫 Müəllim Paneli</h2>"
            "<p>İmtahan yaradın, giriş kodları verin, nəticələri və köçürmə loqlarını izləyin.</p></div>",
            unsafe_allow_html=True,
        )
        if st.button("Müəllim Paneli", use_container_width=True, type="primary", key="role_teacher"):
            st.session_state.role = "teacher"
            st.rerun()
    with c2:
        st.markdown(
            "<div class='role-card'><h2>🎓 Şagird Paneli</h2>"
            "<p>Müəllimin verdiyi unikal kodla imtahana daxil olun.</p></div>",
            unsafe_allow_html=True,
        )
        if st.button("Şagird Paneli", use_container_width=True, type="primary", key="role_student"):
            st.session_state.role = "student"
            st.rerun()


# ----------------------------------------------------------------------------
# MÜƏLLİM PANELİ
# ----------------------------------------------------------------------------
def teacher_panel():
    ss = st.session_state
    if not ss.get("teacher_ok"):
        st.title("👨‍🏫 Müəllim girişi")
        with st.form("teacher_login"):
            pw = st.text_input("Şifrə", type="password")
            ok = st.form_submit_button("Daxil ol", type="primary")
        if ok:
            if pw == get_teacher_password():
                ss.teacher_ok = True
                st.rerun()
            else:
                st.error("Şifrə yanlışdır.")
        return

    st.title("👨‍🏫 Müəllim Paneli")
    tab_exam, tab_codes, tab_results, tab_logs = st.tabs(
        ["📄 İmtahanın yaradılması", "🔑 Giriş kodları", "📊 Nəticələr", "🚨 Köçürmə loqları"]
    )
    with tab_exam:
        teacher_exam_tab()
    with tab_codes:
        teacher_codes_tab()
    with tab_results:
        teacher_results_tab()
    with tab_logs:
        teacher_logs_tab()


def teacher_exam_tab():
    exam = STORE["exam"]
    if exam:
        st.success(
            f"**Aktiv imtahan:** {exam['title']}  •  {exam['num_questions']} sual  •  "
            f"{exam['duration_min']} dəqiqə  •  {len(exam['files'])} fayl  •  "
            f"yaradılıb: {fmt_time(exam['id'], with_date=True)}"
        )
        with st.expander("Cavab açarına bax"):
            key_df = pd.DataFrame(
                {
                    "Sual": list(range(1, exam["num_questions"] + 1)),
                    "Növ": ["Variantlı" if is_choice(k) else "Açıq cavab" for k in exam["answer_key"]],
                    "Düzgün cavab": exam["answer_key"],
                }
            )
            st.dataframe(key_df, hide_index=True)
        st.warning("Yeni imtahan yadda saxlanılarsa, köhnə nəticələr və loqlar silinəcək, kodlar sıfırlanacaq.")
    else:
        st.info("Hələ imtahan yaradılmayıb.")

    st.subheader("Yeni imtahan")
    with st.form("exam_form"):
        title = st.text_input("İmtahanın adı", value=exam["title"] if exam else "", placeholder="Məs: Riyaziyyat – 9-cu sinif")
        c1, c2 = st.columns(2)
        num_q = c1.number_input("Sual sayı", min_value=1, max_value=200, value=exam["num_questions"] if exam else 10, step=1)
        duration = c2.number_input("İmtahan müddəti (dəqiqə)", min_value=1, max_value=600, value=exam["duration_min"] if exam else 60, step=1)
        files = st.file_uploader(
            "Sual faylları (PDF və ya şəkil — bir neçə fayl seçmək olar)",
            type=["pdf", "png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
        )
        text_questions = st.text_area(
            "Və ya sualları mətn şəklində yazın (istəyə görə)",
            height=160,
            placeholder="1. 2 + 2 = ?\nA) 3  B) 4  C) 5  D) 6  E) 7\n\n2. ...",
        )
        answer_key_text = st.text_area(
            "Düzgün cavab açarı",
            height=110,
            placeholder="1-A, 2-C, 3-B, ... 15-A, 16-4, 17-6, 22-46%, 25-41",
            help="A–E hərfi yazılan suallar variantlı olur. Rəqəm və ya mətn yazılan suallar "
                 "açıq cavablı olur — şagird cavabı özü yazır (məs. 16-4, 22-46%).",
        )
        st.caption("Variantlı: `1-A`. Açıq cavablı: `16-4`, `22-46%`, `23-2,5`. "
                   "Açıq cavablarda boşluq, %-işarəsi və vergül/nöqtə fərqi nəzərə alınmır.")
        submitted = st.form_submit_button("💾 İmtahanı yadda saxla", type="primary")

    if submitted:
        num_q = int(num_q)
        errors = []
        if not title.strip():
            errors.append("İmtahanın adını daxil edin.")
        if not files and not text_questions.strip():
            errors.append("Ən azı bir PDF/şəkil faylı yükləyin və ya sualları mətn şəklində yazın.")
        key, missing, extra = parse_answer_key(answer_key_text, num_q)
        if missing:
            errors.append("Bu sualların cavabı yoxdur: " + ", ".join(map(str, missing)))
        if extra:
            errors.append(f"Sual sayı {num_q}-dir, amma bu nömrələr üçün də cavab yazılıb: " + ", ".join(map(str, extra)))
        if errors:
            for e in errors:
                st.error(e)
            return

        new_exam = {
            "id": time.time(),
            "title": title.strip(),
            "num_questions": num_q,
            "duration_min": int(duration),
            "answer_key": key,
            "files": [{"name": f.name, "type": f.type or "", "data": f.getvalue()} for f in (files or [])],
            "text_questions": text_questions.strip(),
        }
        with LOCK:
            STORE["exam"] = new_exam
            STORE["results"].clear()
            STORE["logs"].clear()
            STORE["log_ids"].clear()
            for rec in STORE["codes"].values():
                rec.update(status="unused", started_at=None, deadline=None, exam_id=None, answers=None)
        st.success("İmtahan yadda saxlanıldı! İndi «Giriş kodları» bölməsindən şagird kodlarını əlavə edin.")
        st.rerun()


def teacher_codes_tab():
    with LOCK:
        codes = {c: dict(r) for c, r in STORE["codes"].items()}

    st.caption("Hər sətirdə bir kod yazın. İstəyə görə vergüldən sonra şagirdin adını əlavə edin. Məs: `STU001, Əli Məmmədov`")
    default_text = "\n".join(f"{c}, {r['name']}" if r.get("name") else c for c, r in codes.items())
    with st.form("codes_form"):
        txt = st.text_area("Giriş kodları", value=default_text, height=220, placeholder="STU001, Əli Məmmədov\nSTU002, Leyla Həsənova\nSTU003")
        save = st.form_submit_button("💾 Kodları yadda saxla", type="primary")
    if save:
        parsed, bad = parse_codes(txt)
        if bad:
            st.error("Bu sətirlər düzgün deyil (kod 3-20 simvol, yalnız hərf/rəqəm/-/_): " + "; ".join(bad))
        else:
            with LOCK:
                for c in list(STORE["codes"].keys()):
                    if c not in parsed:
                        del STORE["codes"][c]
                for c, name in parsed.items():
                    if c in STORE["codes"]:
                        STORE["codes"][c]["name"] = name or STORE["codes"][c].get("name", "")
                    else:
                        STORE["codes"][c] = {"name": name, "status": "unused", "started_at": None,
                                             "deadline": None, "exam_id": None, "answers": None}
            st.success(f"{len(parsed)} kod yadda saxlanıldı.")
            st.rerun()

    with st.expander("⚙️ Kodları avtomatik yarat"):
        c1, c2, c3 = st.columns([1, 1, 1])
        prefix = c1.text_input("Prefiks", value="STU").strip().upper() or "STU"
        count = c2.number_input("Neçə kod?", min_value=1, max_value=500, value=10)
        c3.write("")
        c3.write("")
        if c3.button("Yarat", use_container_width=True):
            with LOCK:
                existing = [int(m.group(1)) for c in STORE["codes"] if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", c))]
                start = max(existing, default=0) + 1
                for i in range(start, start + int(count)):
                    STORE["codes"][f"{prefix}{i:03d}"] = {"name": "", "status": "unused", "started_at": None,
                                                         "deadline": None, "exam_id": None, "answers": None}
            st.rerun()

    if codes:
        st.subheader("Kodların vəziyyəti")
        df = pd.DataFrame(
            [
                {
                    "Kod": c,
                    "Şagird": r.get("name", ""),
                    "Status": STATUS_LABELS.get(r["status"], r["status"]),
                    "Başlama vaxtı": fmt_time(r.get("started_at")),
                }
                for c, r in codes.items()
            ]
        )
        st.dataframe(df, hide_index=True)

        st.markdown("**Kodu sıfırla** (məs. şagirdin brauzeri bağlandısa, ona yenidən giriş imkanı vermək üçün)")
        c1, c2 = st.columns([2, 1])
        sel = c1.selectbox("Kod", list(codes.keys()), label_visibility="collapsed")
        if c2.button("🔄 Sıfırla", use_container_width=True):
            with LOCK:
                if sel in STORE["codes"]:
                    STORE["codes"][sel].update(status="unused", started_at=None, deadline=None, exam_id=None, answers=None)
                STORE["results"].pop(sel, None)
                STORE["logs"] = [l for l in STORE["logs"] if l["code"] != sel]
            st.success(f"{sel} sıfırlandı.")
            st.rerun()


def teacher_results_tab():
    c1, c2 = st.columns([1, 4])
    if c1.button("🔄 Yenilə", key="refresh_results", use_container_width=True):
        st.rerun()
    exam = STORE["exam"]
    with LOCK:
        codes = {c: dict(r) for c, r in STORE["codes"].items()}
        results = {c: dict(r) for c, r in STORE["results"].items()}
        logs = list(STORE["logs"])

    if not codes:
        st.info("Hələ kod əlavə edilməyib.")
        return

    rows = []
    for c, r in codes.items():
        res = results.get(c)
        viol = [l for l in logs if l["code"] == c and l["kind"] != "return"]
        rows.append(
            {
                "Kod": c,
                "Şagird": (res or r).get("name", ""),
                "Status": STATUS_LABELS.get(r["status"], r["status"]),
                "Düz": res["correct"] if res else None,
                "Səhv": res["wrong"] if res else None,
                "Boş": res["blank"] if res else None,
                "Bal (100)": res["score"] if res else None,
                "Başladı": fmt_time(r.get("started_at")),
                "Bitirdi": fmt_time(res["finished_at"]) if res else "",
                "Bitmə səbəbi": res["reason"] if res else "",
                "Səhifədən çıxış sayı": len(viol),
                "Çıxış vaxtları": ", ".join(f"{fmt_time(l['ts'])} ({l['minute']}-ci dəq.)" for l in viol) or "Yoxdur",
            }
        )
    df = pd.DataFrame(rows)

    finished = df[df["Bal (100)"].notna()]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Ümumi kod", len(df))
    m2.metric("Bitirən", len(finished))
    m3.metric("Orta bal", f"{finished['Bal (100)'].mean():.1f}" if len(finished) else "—")
    m4.metric("Köçürmə şübhəsi olan", int((df["Səhifədən çıxış sayı"] > 0).sum()))

    def highlight(row):
        color = "background-color: rgba(176,0,32,0.18)" if row["Səhifədən çıxış sayı"] > 0 else ""
        return [color] * len(row)

    st.dataframe(df.style.apply(highlight, axis=1), hide_index=True)
    st.download_button(
        "⬇️ Nəticələri CSV kimi yüklə",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name="imtahan_neticeleri.csv",
        mime="text/csv",
    )

    if exam and results:
        with st.expander("🔍 Şagirdin cavablarına ətraflı bax"):
            sel = st.selectbox("Şagird", list(results.keys()),
                               format_func=lambda c: f"{c} — {results[c].get('name', '')}")
            res = results[sel]
            detail = pd.DataFrame(
                {
                    "Sual": list(range(1, res["total"] + 1)),
                    "Şagirdin cavabı": [a or "—" for a in res["answers"]],
                    "Düzgün cavab": exam["answer_key"][: res["total"]],
                    "Nəticə": [
                        "Boş" if not a else ("✅ Düz" if answers_match(a, k) else "❌ Səhv")
                        for a, k in zip(res["answers"], exam["answer_key"])
                    ],
                }
            )
            st.dataframe(detail, hide_index=True)


def teacher_logs_tab():
    if st.button("🔄 Yenilə", key="refresh_logs"):
        st.rerun()
    if not HAS_STJS:
        st.error("`streamlit-javascript` quraşdırılmayıb — köçürmə izlənmir. `pip install streamlit-javascript`")
    with LOCK:
        logs = sorted(STORE["logs"], key=lambda l: l["ts"])
    if not logs:
        st.success("Hələ heç bir köçürmə/səhifədən çıxış qeydə alınmayıb.")
        return
    df = pd.DataFrame(
        [
            {
                "Kod": l["code"],
                "Şagird": l["name"],
                "Vaxt": fmt_time(l["ts"]),
                "İmtahanın dəqiqəsi": l["minute"],
                "Hadisə": l["text"],
            }
            for l in logs
        ]
    )
    only_viol = st.toggle("Yalnız səhifədən çıxışları göstər", value=False)
    if only_viol:
        df = df[~df["Hadisə"].str.startswith("Səhifəyə qayıtdı")]
    st.dataframe(df, hide_index=True)
    st.download_button(
        "⬇️ Loqları CSV kimi yüklə",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name="kocurme_loqlari.csv",
        mime="text/csv",
    )


# ----------------------------------------------------------------------------
# ŞAGİRD PANELİ
# ----------------------------------------------------------------------------
def student_panel():
    ss = st.session_state
    exam = STORE["exam"]

    if ss.get("stu_finished"):
        student_result_screen()
        return

    if exam is None:
        st.title("🎓 Şagird Paneli")
        st.info("Hazırda aktiv imtahan yoxdur. Müəllim imtahanı yaratdıqdan sonra yenidən yoxlayın.")
        return

    if not ss.get("stu_code"):
        student_login(exam)
        return

    student_exam_screen(exam)


def student_login(exam):
    st.title("🎓 Şagird Paneli")
    st.subheader(f"İmtahan: {exam['title']}")
    st.caption(f"{exam['num_questions']} sual • {exam['duration_min']} dəqiqə")
    st.warning(
        "⚠️ İmtahan zamanı başqa tab-a keçmək, brauzeri kiçiltmək və ya başqa pəncərəyə keçmək "
        "qadağandır — hər çıxış vaxtı ilə birlikdə müəllimə bildirilir. Kod yalnız **1 dəfə** istifadə oluna bilər; "
        "imtahan zamanı səhifəni yeniləməyin."
    )
    with st.form("student_login"):
        code = st.text_input("Giriş kodu", placeholder="STU001").strip().upper()
        name = st.text_input("Adınız və soyadınız (müəllim kodla birlikdə adınızı veribsə, boş saxlaya bilərsiniz)")
        go = st.form_submit_button("İmtahana başla", type="primary")

    if not go:
        return
    if not code:
        st.error("Kodu daxil edin.")
        return
    with LOCK:
        rec = STORE["codes"].get(code)
        if rec is None:
            st.error("Belə kod mövcud deyil.")
            return
        if rec["status"] != "unused":
            st.error("Bu kod artıq istifadə olunub. Hər kod yalnız 1 dəfə istifadə oluna bilər.")
            return
        final_name = rec.get("name") or name.strip()
        if not final_name:
            st.error("Adınızı daxil edin.")
            return
        now = time.time()
        rec.update(
            name=final_name,
            status="in_progress",
            started_at=now,
            deadline=now + exam["duration_min"] * 60,
            exam_id=exam["id"],
            answers=None,
        )
    reset_student_session()
    st.session_state.stu_code = code
    st.session_state.stu_exam_id = exam["id"]
    st.rerun()


def render_timer(deadline):
    end_ms = int(deadline * 1000)
    html_iframe(
        f"""
        <div id="timer" style="font-family:system-ui,sans-serif;font-size:30px;font-weight:800;
             text-align:center;padding:10px 0;border-radius:12px;background:#0f5132;color:#fff;
             letter-spacing:1px;">--:--</div>
        <script>
          const end = {end_ms};
          const el = document.getElementById('timer');
          function tick() {{
            const s = Math.max(0, Math.floor((end - Date.now()) / 1000));
            const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
            const pad = (x) => String(x).padStart(2, '0');
            const txt = (h > 0 ? pad(h) + ':' : '') + pad(m) + ':' + pad(sec);
            el.textContent = '⏱ Qalan vaxt: ' + txt;
            if (s <= 300) el.style.background = '#b00020';
            else if (s <= 600) el.style.background = '#b35c00';
            if (s <= 0) {{ el.textContent = '⏰ Vaxt bitdi! İmtahan yekunlaşdırılır...'; return; }}
            setTimeout(tick, 250);
          }}
          tick();
        </script>
        """,
        height=68,
    )


@st.fragment(run_every=POLL_SECONDS)
def live_guard(code, deadline):
    """Hər POLL_SECONDS saniyədən bir: vaxtı yoxlayır və brauzerdən köçürmə hadisələrini oxuyur."""
    ss = st.session_state

    # 1) Vaxt bitibsə — avtomatik yekunlaşdır
    if time.time() >= deadline:
        finalize_exam(code, "Vaxt bitdi (avtomatik)")
        st.rerun()

    # 2) Köçürmə izləmə
    if HAS_STJS:
        tick = int(time.time() // POLL_SECONDS)
        raw = st_javascript(build_poll_js(code, tick), key=f"guard_{code}_{tick}")
        if isinstance(raw, str) and raw.startswith("["):
            try:
                events = json.loads(raw)
            except json.JSONDecodeError:
                events = []
            if events and record_events(code, events):
                ss.warn_until = time.time() + WARNING_SHOW_SECONDS
    else:
        st.error("`streamlit-javascript` quraşdırılmayıb — köçürmə izlənmir.")

    if ss.get("warn_until", 0) > time.time():
        st.markdown(
            "<div class='cheat-box'>⚠️ Diqqət! Səhifədən çıxmaq qadağandır. Bu hal müəllimə bildiriləcək!</div>",
            unsafe_allow_html=True,
        )
    n_viol = len(violation_events(code))
    if n_viol:
        st.caption(f"🚨 Qeydə alınan səhifədən çıxış sayı: **{n_viol}**")


def render_questions(exam):
    if not exam["files"] and not exam["text_questions"]:
        st.info("Sual faylı yoxdur.")
    for f in exam["files"]:
        is_pdf = f["name"].lower().endswith(".pdf") or "pdf" in f["type"]
        if is_pdf:
            if HAS_FITZ:
                try:
                    pages = pdf_to_images(f["data"])
                    for i, img in enumerate(pages, 1):
                        st.image(img, caption=f"{f['name']} — səhifə {i}/{len(pages)}")
                except Exception as e:
                    st.error(f"PDF açıla bilmədi: {e}")
            else:
                b64 = base64.b64encode(f["data"]).decode()
                st.markdown(
                    f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="900" '
                    f'style="border:none"></iframe>',
                    unsafe_allow_html=True,
                )
                st.caption("PDF görünmürsə, serverdə `pip install pymupdf` quraşdırın.")
        else:
            st.image(f["data"], caption=f["name"])
    if exam["text_questions"]:
        st.markdown(exam["text_questions"].replace("\n", "  \n"))


def student_exam_screen(exam):
    ss = st.session_state
    code = ss.stu_code
    with LOCK:
        rec = STORE["codes"].get(code)
        rec = dict(rec) if rec else None

    if rec is None or rec.get("exam_id") != exam["id"] or ss.get("stu_exam_id") != exam["id"]:
        st.error("İmtahan sessiyanız etibarsızdır (müəllim imtahanı və ya kodunuzu yeniləyib).")
        if st.button("Ana səhifəyə qayıt"):
            reset_student_session()
            st.rerun()
        return
    if rec["status"] == "finished":
        finalize_exam(code, "Artıq bitirilib")
        st.rerun()

    deadline = rec["deadline"]
    n = exam["num_questions"]

    # --- Yuxarı hissə: başlıq + canlı taymer ---
    h1, h2 = st.columns([3, 2])
    with h1:
        st.markdown(f"### 📝 {exam['title']}")
        st.caption(f"Şagird: **{rec['name']}** ({code}) • Başladı: {fmt_time(rec['started_at'])}")
    with h2:
        render_timer(deadline)

    live_guard(code, deadline)

    # --- Ekran iki hissəyə bölünür ---
    left, right = st.columns([3, 2], gap="medium")
    with left:
        st.markdown("#### Suallar")
        with st.container(height=760, border=True):
            render_questions(exam)

    with right:
        st.markdown("#### Cavab vərəqi")
        with st.container(height=640, border=True):
            for i in range(1, n + 1):
                if is_choice(exam["answer_key"][i - 1]):
                    st.radio(f"**{i}.**", OPTIONS, index=None, horizontal=True, key=f"ans_{code}_{i}")
                else:
                    st.text_input(f"**{i}.** (cavabı yazın)", key=f"ans_{code}_{i}",
                                  placeholder="Cavabınız...")

        answers = [(str(ss.get(f"ans_{code}_{i}") or "").strip() or None) for i in range(1, n + 1)]
        answered = sum(1 for a in answers if a)
        with LOCK:
            if code in STORE["codes"]:
                STORE["codes"][code]["answers"] = answers  # müəllim üçün ara yaddaş
        st.progress(answered / n, text=f"Cavablandırılıb: {answered}/{n}")

        if not ss.get("confirm_finish"):
            if st.button("✅ İmtahanı Bitir", type="primary", use_container_width=True):
                ss.confirm_finish = True
                st.rerun()
        else:
            if answered < n:
                st.warning(f"{n - answered} sual cavabsızdır.")
            st.write("İmtahanı bitirmək istədiyinizə əminsiniz? Sonra cavabları dəyişmək olmaz.")
            b1, b2 = st.columns(2)
            if b1.button("Bəli, bitir", type="primary", use_container_width=True):
                finalize_exam(code, "Şagird bitirdi")
                st.rerun()
            if b2.button("Ləğv et", use_container_width=True):
                ss.confirm_finish = False
                st.rerun()


def student_result_screen():
    ss = st.session_state
    res = ss.get("stu_result")
    deactivate_guard()
    st.title("🎉 İmtahan yekunlaşdı")
    if not res:
        st.info("Nəticə tapılmadı.")
    else:
        if res["reason"].startswith("Vaxt"):
            st.warning("⏰ Vaxt bitdi — imtahan avtomatik olaraq yekunlaşdırıldı.")
        st.subheader(f"{res['name']} ({res['code']})")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("✅ Düzgün", res["correct"])
        c2.metric("❌ Səhv", res["wrong"])
        c3.metric("⬜ Boş", res["blank"])
        c4.metric("🏆 Bal", f"{res['score']} / 100")
        st.progress(res["score"] / 100)
        st.caption(f"Hər düzgün cavab {round(100 / res['total'], 2)} bal dəyərindədir.")
        n_viol = len(violation_events(res["code"]))
        if n_viol:
            st.error(f"İmtahan zamanı {n_viol} dəfə səhifədən çıxış qeydə alınıb və müəllimə bildirilib.")
    if st.button("Çıxış"):
        reset_student_session()
        ss.role = None
        st.rerun()


# ----------------------------------------------------------------------------
# ƏSAS
# ----------------------------------------------------------------------------
def main():
    ss = st.session_state
    ss.setdefault("role", None)

    in_exam = ss.get("role") == "student" and ss.get("stu_code") and not ss.get("stu_finished")

    with st.sidebar:
        st.markdown("## 📝 İmtahan Portalı")
        if in_exam:
            st.info("İmtahan davam edir. Bitirmədən çıxmaq olmaz.")
        elif ss.role:
            if st.button("🏠 Ana səhifə", use_container_width=True):
                ss.role = None
                ss.teacher_ok = False
                reset_student_session()
                st.rerun()
            if ss.role == "teacher" and ss.get("teacher_ok"):
                if st.button("🚪 Müəllim çıxışı", use_container_width=True):
                    ss.teacher_ok = False
                    ss.role = None
                    st.rerun()

    if ss.role == "teacher":
        teacher_panel()
    elif ss.role == "student":
        student_panel()
    else:
        role_selection()


main()
