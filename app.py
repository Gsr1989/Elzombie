from flask import Flask, render_template, render_template_string, request, redirect, \
    url_for, flash, session, send_file, abort, jsonify, Response
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
import qrcode
from PIL import Image
from io import BytesIO, StringIO
import csv
import re
import logging
import sys

from werkzeug.middleware.proxy_fix import ProxyFix

# ===================== LOGGING =====================
sys.dont_write_bytecode = True
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ===================== ZONA HORARIA =====================
TZ_CDMX = ZoneInfo("America/Mexico_City")

def now_cdmx() -> datetime:
    return datetime.now(TZ_CDMX)

def today_cdmx() -> date:
    return now_cdmx().date()

def parse_date_any(value) -> date:
    if not value:
        raise ValueError("Fecha vacía")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ_CDMX)
        else:
            value = value.astimezone(TZ_CDMX)
        return value.date()
    s = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CDMX)
    else:
        dt = dt.astimezone(TZ_CDMX)
    return dt.date()

# ===================== FLASK CONFIG =====================
app = Flask(__name__)
app.secret_key = 'clave_muy_segura_123456'
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=2, x_host=2, x_prefix=1)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,
    SEND_FILE_MAX_AGE_DEFAULT=0,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24)
)

# ===================== SUPABASE CONFIG =====================
SUPABASE_URL = "https://xsagwqepoljfsogusubw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===================== CONFIG GENERAL =====================
OUTPUT_DIR           = "documentos"
PLANTILLA_PRINCIPAL  = "cdmxdigital2025ppp.pdf"
PLANTILLA_SECUNDARIA = "elbueno.pdf"
URL_CONSULTA_BASE    = "https://semovidigitalgob.onrender.com"
ENTIDAD              = "cdmx"
PRECIO_PERMISO       = 374
DIAS_PERMISO         = 30
PAGE_SIZE            = 100   # registros por página en admin_tabla

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===================== FOLIOS CDMX =====================
PREFIJO_CDMX = "412"

def generar_folio_automatico_cdmx():
    todos = supabase.table("folios_registrados")\
        .select("folio").like("folio", f"{PREFIJO_CDMX}%").execute().data or []

    consecutivos = []
    for f in todos:
        s = str(f.get('folio', ''))
        if s.startswith(PREFIJO_CDMX) and s[len(PREFIJO_CDMX):].isdigit():
            consecutivos.append(int(s[len(PREFIJO_CDMX):]))

    siguiente = (max(consecutivos) + 1) if consecutivos else 1
    logger.info(f"[FOLIO] siguiente candidato: {PREFIJO_CDMX}{siguiente}")

    for intento in range(10_000_000):
        candidato = f"{PREFIJO_CDMX}{siguiente + intento}"
        existe = supabase.table("folios_registrados")\
            .select("folio").eq("folio", candidato).limit(1).execute().data
        if not existe:
            logger.info(f"[FOLIO] ✅ {candidato} (intento {intento+1})")
            return candidato
        if intento and intento % 10_000 == 0:
            logger.info(f"[FOLIO] buscando... intento {intento}")

    raise Exception("Sin folio disponible tras 10,000,000 intentos")


def guardar_folio_con_reintento(datos, username):
    fexp_date = parse_date_any(datos["fecha_exp"])
    fven_date = parse_date_any(datos["fecha_ven"])

    def _row(folio):
        return {
            "folio":             folio,
            "marca":             datos["marca"],
            "linea":             datos["linea"],
            "anio":              datos["anio"],
            "numero_serie":      datos["numero_serie"],
            "numero_motor":      datos["numero_motor"],
            "nombre":            datos.get("nombre", "SIN NOMBRE"),
            "fecha_expedicion":  fexp_date.isoformat(),
            "fecha_vencimiento": fven_date.isoformat(),
            "entidad":           ENTIDAD,
            "estado":            "ACTIVO",
            "creado_por":        username
        }

    # MANUAL
    if datos.get("folio") and str(datos["folio"]).strip():
        fm = str(datos["folio"]).strip()
        try:
            supabase.table("folios_registrados").insert(_row(fm)).execute()
            datos["folio"] = fm
            logger.info(f"[DB] ✅ Folio MANUAL {fm}")
            return True
        except Exception as e:
            em = str(e).lower()
            if any(k in em for k in ("duplicate", "unique", "23505")):
                logger.error(f"[ERROR] Folio {fm} YA EXISTE")
            else:
                logger.error(f"[ERROR BD] {e}")
            return False

    # AUTO +1
    try:
        base = generar_folio_automatico_cdmx()
    except Exception as e:
        logger.error(f"[ERROR] {e}")
        return False

    num = int(base[len(PREFIJO_CDMX):]) if base[len(PREFIJO_CDMX):].isdigit() else 1

    for intento in range(10_000_000):
        c = f"{PREFIJO_CDMX}{num + intento}"
        try:
            supabase.table("folios_registrados").insert(_row(c)).execute()
            datos["folio"] = c
            logger.info(f"[DB] ✅ Folio AUTO {c} (intento {intento+1})")
            return True
        except Exception as e:
            em = str(e).lower()
            if any(k in em for k in ("duplicate", "unique", "23505")):
                continue
            logger.error(f"[ERROR BD] {e}")
            return False

    logger.error("[ERROR] Sin folio tras 10,000,000 intentos")
    return False

# ===================== QR / PDF =====================
def generar_qr_dinamico_cdmx(folio):
    try:
        url = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                             box_size=4, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img, url
    except Exception as e:
        logger.error(f"[QR] {e}")
        return None, None


def generar_pdf_unificado_cdmx(datos: dict) -> str:
    fol          = datos["folio"]
    fecha_exp_dt = datos["fecha_exp"]
    fecha_ven_dt = datos["fecha_ven"]

    if fecha_exp_dt.tzinfo is None:
        fecha_exp_dt = fecha_exp_dt.replace(tzinfo=TZ_CDMX)
    else:
        fecha_exp_dt = fecha_exp_dt.astimezone(TZ_CDMX)

    if isinstance(fecha_ven_dt, str):
        fecha_ven_str = fecha_ven_dt
    else:
        if fecha_ven_dt.tzinfo is None:
            fecha_ven_dt = fecha_ven_dt.replace(tzinfo=TZ_CDMX)
        else:
            fecha_ven_dt = fecha_ven_dt.astimezone(TZ_CDMX)
        fecha_ven_str = fecha_ven_dt.strftime("%d/%m/%Y")

    out = os.path.join(OUTPUT_DIR, f"{fol}.pdf")

    try:
        # ── PÁGINA 1 ──────────────────────────────────────────────────────────
        doc1 = fitz.open(PLANTILLA_PRINCIPAL)
        pg1  = doc1[0]

        meses = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
                 7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"}
        fecha_texto = f"{fecha_exp_dt.day} de {meses[fecha_exp_dt.month]} del {fecha_exp_dt.year}"

        pg1.insert_text((50,  130), "FOLIO: ",             fontsize=12, fontname="helv", color=(0,0,0))
        pg1.insert_text((100, 130), fol,                   fontsize=12, fontname="helv", color=(1,0,0))
        pg1.insert_text((130, 145), fecha_texto,           fontsize=12, fontname="helv", color=(0,0,0))
        pg1.insert_text((87,  290), datos["marca"],        fontsize=11, fontname="helv", color=(0,0,0))
        pg1.insert_text((375, 290), datos["numero_serie"], fontsize=11, fontname="helv", color=(0,0,0))
        pg1.insert_text((87,  307), datos["linea"],        fontsize=11, fontname="helv", color=(0,0,0))
        pg1.insert_text((375, 307), datos["numero_motor"], fontsize=11, fontname="helv", color=(0,0,0))
        pg1.insert_text((87,  323), str(datos["anio"]),    fontsize=11, fontname="helv", color=(0,0,0))
        pg1.insert_text((375, 323), fecha_ven_str,         fontsize=11, fontname="helv", color=(0,0,0))
        if datos.get("nombre"):
            pg1.insert_text((375, 340), datos["nombre"],   fontsize=11, fontname="helv", color=(0,0,0))

        img_qr, _ = generar_qr_dinamico_cdmx(fol)
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            pg1.insert_image(fitz.Rect(49, 653, 145, 749),
                             pixmap=fitz.Pixmap(buf.read()), overlay=True)

        # ── PÁGINA 2 ──────────────────────────────────────────────────────────
        if os.path.exists(PLANTILLA_SECUNDARIA):
            doc2 = fitz.open(PLANTILLA_SECUNDARIA)
            pg2  = doc2[0]

            titulo_p2 = (f"IMPUESTO POR DERECHO DE AUTOMOVIL Y MOTOCICLETAS "
                         f"(PERMISO PARA CIRCULAR {DIAS_PERMISO} DIAS)")
            anio_str  = str(datos["anio"])

            pg2.insert_text((135, 170), titulo_p2,                          fontsize=6,  fontname="hebo", color=(0,0,0))
            pg2.insert_text((135, 194), datos["numero_serie"],               fontsize=6,  fontname="hebo", color=(0,0,0))
            pg2.insert_text((135, 202), anio_str,                            fontsize=6,  fontname="hebo", color=(0,0,0))
            pg2.insert_text((385, 411), f"${PRECIO_PERMISO}",               fontsize=16, fontname="hebo", color=(0,0,0))
            pg2.insert_text((190, 324), fecha_exp_dt.strftime('%d/%m/%Y'),   fontsize=6,  fontname="hebo", color=(0,0,0))

            doc1.insert_pdf(doc2)
            doc2.close()

        doc1.save(out)
        doc1.close()
        logger.info(f"[PDF] ✅ {out}")

    except Exception as e:
        logger.error(f"[PDF ERROR] {e}")
        fb = fitz.open()
        fb.new_page().insert_text((50, 50), f"ERROR - {fol}", fontsize=12)
        fb.save(out)
        fb.close()

    return out

# ===================== TABLAS DISPONIBLES =====================
TABLAS_DISPONIBLES = {
    'folios_registrados': {
        'nombre':        'Folios Registrados',
        'pk_col':        'folio',
        'search_cols':   ['folio', 'marca', 'linea', 'numero_serie', 'numero_motor',
                          'nombre', 'estado', 'creado_por', 'entidad'],
    },
    'verificaciondigitalcdmx': {
        'nombre':      'Usuarios del Sistema',
        'pk_col':      'id',
        'search_cols': ['username', 'password'],
    }
}

# ===================== TEMPLATE TABLA — celdas como texto, input solo al hacer click ===
TABLE_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ info_tabla.nombre }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Courier New',monospace;background:#0d0d0d;color:#e2e8f0;min-height:100vh;font-size:12px}
a{text-decoration:none}
header{background:#111827;border-bottom:1px solid #1f2937;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:300;gap:8px;flex-wrap:wrap}
.title{font-size:12px;font-weight:700;letter-spacing:2px;color:#f1f5f9}
.dot{width:7px;height:7px;border-radius:50%;background:#10b981;box-shadow:0 0 6px #10b981;display:inline-block;margin-right:8px;flex-shrink:0}
.hbtns{display:flex;gap:6px;flex-shrink:0}
.btn{background:transparent;border:1px solid #374151;color:#9ca3af;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:10px;font-family:inherit;letter-spacing:1px;white-space:nowrap;transition:all .15s}
.btn:hover{border-color:#6b7280;color:#e2e8f0}
.btn-blue{border-color:#1d4ed8;color:#60a5fa}
.btn-blue:hover{background:#1e3a5f}

/* ── BARRA BÚSQUEDA ── */
.bar{display:flex;gap:8px;align-items:center;padding:10px 16px;background:#0d1117;border-bottom:1px solid #1f2937;flex-wrap:wrap}
.sbox{flex:1;min-width:200px;background:#1f2937;border:1px solid #374151;color:#e2e8f0;border-radius:5px;padding:8px 12px;font-family:inherit;font-size:12px;outline:none;transition:border .2s}
.sbox:focus{border-color:#3b82f6}
.sbox::placeholder{color:#4b5563}
.sbtn{background:#1d4ed8;border:none;color:#fff;border-radius:5px;padding:8px 16px;cursor:pointer;font-family:inherit;font-size:11px;letter-spacing:1px;white-space:nowrap}
.sbtn:hover{background:#2563eb}
.counter{font-size:10px;color:#6b7280;white-space:nowrap}
.clear-btn{color:#6b7280;font-size:11px;cursor:pointer;background:none;border:none;font-family:inherit;padding:4px 8px}
.clear-btn:hover{color:#e2e8f0}

/* ── TABLA ── */
.wrap{overflow-x:auto;padding:0 0 80px}
table{width:100%;border-collapse:collapse;table-layout:auto}
thead th{background:#1f2937;color:#6b7280;padding:8px 8px;text-align:left;font-size:10px;letter-spacing:1px;font-weight:700;border-bottom:1px solid #374151;white-space:nowrap;position:sticky;top:0;z-index:100}
tbody tr:nth-child(odd){background:#111827}
tbody tr:nth-child(even){background:#0d0d0d}
tbody tr:hover{background:#1a2335}
td{padding:5px 6px;border-bottom:1px solid #0d1117;vertical-align:middle;white-space:nowrap}
.num{color:#374151;font-size:10px;user-select:none;min-width:28px;text-align:center}

/* ── CELDA ── */
.cv{display:block;min-width:70px;max-width:220px;overflow:hidden;text-overflow:ellipsis;color:#9ca3af;cursor:text;padding:3px 5px;border-radius:3px;border:1px solid transparent}
.cv:hover{border-color:#374151;background:#1f2937}
.cv.folio{color:#fbbf24}
.cv.estado{color:#f59e0b}
.cv.nombre,.cv.contribuyente,.cv.username{color:#e2e8f0}
.cv.null-val{color:#374151;font-style:italic}

/* ── INPUT (solo aparece al editar) ── */
.cell-input{background:#1e3a5f;border:1px solid #3b82f6;color:#93c5fd;border-radius:4px;padding:4px 6px;font-size:11px;font-family:inherit;min-width:120px;max-width:280px;outline:none}
.cell-input.saving{background:#064e3b;border-color:#10b981;color:#6ee7b7}
.cell-input.err{background:#7f1d1d;border-color:#ef4444;color:#fca5a5}

/* ── BOTÓN DEL ── */
.del-btn{background:transparent;border:1px solid #374151;color:#ef4444;border-radius:4px;padding:3px 7px;cursor:pointer;font-size:10px;font-family:inherit}
.del-btn:hover{background:#7f1d1d}

/* ── PAGINACIÓN ── */
.pag{display:flex;gap:8px;align-items:center;justify-content:center;padding:16px;border-top:1px solid #1f2937;background:#0d1117;position:sticky;bottom:0}
.pag a{background:transparent;border:1px solid #374151;color:#9ca3af;border-radius:5px;padding:6px 14px;font-size:11px;letter-spacing:1px}
.pag a:hover{border-color:#6b7280;color:#e2e8f0}
.pag .cur{background:#1f2937;border-color:#374151;color:#f1f5f9;font-weight:700;border-radius:5px;padding:6px 14px;font-size:11px}
.pag .info{color:#6b7280;font-size:11px}
.empty{text-align:center;color:#4b5563;padding:60px;letter-spacing:1px}

/* ── TOAST ── */
.toast{position:fixed;bottom:70px;right:16px;z-index:999;padding:9px 16px;border-radius:7px;font-size:11px;letter-spacing:1px;opacity:0;transition:opacity .25s;pointer-events:none;max-width:280px}
.toast.show{opacity:1}
.toast.ok{background:#064e3b;border:1px solid #10b981;color:#6ee7b7}
.toast.err{background:#7f1d1d;border:1px solid #ef4444;color:#fca5a5}
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center;min-width:0">
    <span class="dot"></span>
    <span class="title">{{ info_tabla.nombre | upper }}</span>
    <span class="counter" style="margin-left:10px">{{ total }} total · pág {{ page }}/{{ total_pages }}</span>
  </div>
  <div class="hbtns">
    <a href="/descargar_tabla/{{ nombre_tabla }}" class="btn btn-blue">⬇ CSV</a>
    <a href="/admin_tablas" class="btn">← Tablas</a>
  </div>
</header>

<!-- BÚSQUEDA SERVER-SIDE -->
<div class="bar">
  <form method="GET" action="" style="display:flex;gap:8px;flex:1;flex-wrap:wrap" id="search-form">
    <input
      type="text"
      name="q"
      class="sbox"
      id="search-input"
      value="{{ q }}"
      placeholder="Busca por cualquier campo: folio, nombre, serie, estado..."
      autocomplete="off"
      autocorrect="off"
      spellcheck="false"
    >
    <input type="hidden" name="page" value="1">
    <button type="submit" class="sbtn">BUSCAR</button>
    {% if q %}
    <a href="/admin_tabla/{{ nombre_tabla }}" class="clear-btn">✕ limpiar</a>
    {% endif %}
  </form>
  <span class="counter">{{ registros|length }} en esta página</span>
</div>

<!-- TABLA -->
<div class="wrap">
  {% if registros %}
  <table id="tbl">
    <thead>
      <tr>
        <th class="num">#</th>
        {% for col in columnas %}
        <th>{{ col }}</th>
        {% endfor %}
        <th>DEL</th>
      </tr>
    </thead>
    <tbody>
      {% for reg in registros %}
      <tr id="row-{{ loop.index }}">
        <td class="num">{{ offset + loop.index }}</td>
        {% for col in columnas %}
        <td>
          {% set val = reg.get(col) %}
          <span
            class="cv {{ col }}{% if val is none or val == '' %} null-val{% endif %}"
            data-col="{{ col }}"
            data-pk="{{ reg.get(pk_col, '') }}"
            data-val="{{ val if val is not none else '' }}"
            onclick="editCell(this)"
            title="Click para editar · {{ col }}"
          >{{ val if val is not none else 'null' }}</span>
        </td>
        {% endfor %}
        <td>
          <button class="del-btn"
            onclick="deleteRow(this,'{{ reg.get(pk_col, '') }}','row-{{ loop.index }}')"
            title="Eliminar">✕</button>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">
    {% if q %}
    Sin resultados para "{{ q }}"
    {% else %}
    Sin registros en esta tabla
    {% endif %}
  </div>
  {% endif %}
</div>

<!-- PAGINACIÓN -->
{% if total_pages > 1 %}
<div class="pag">
  {% if page > 1 %}
  <a href="?q={{ q }}&page={{ page - 1 }}">← Ant</a>
  {% endif %}

  <span class="pag info">
    {% if page > 2 %}<a href="?q={{ q }}&page=1">1</a>{% endif %}
    {% if page > 3 %}<span style="color:#374151">…</span>{% endif %}
    {% if page > 1 %}<a href="?q={{ q }}&page={{ page - 1 }}">{{ page - 1 }}</a>{% endif %}
    <span class="cur">{{ page }}</span>
    {% if page < total_pages %}<a href="?q={{ q }}&page={{ page + 1 }}">{{ page + 1 }}</a>{% endif %}
    {% if page < total_pages - 2 %}<span style="color:#374151">…</span>{% endif %}
    {% if page < total_pages - 1 %}<a href="?q={{ q }}&page={{ total_pages }}">{{ total_pages }}</a>{% endif %}
  </span>

  {% if page < total_pages %}
  <a href="?q={{ q }}&page={{ page + 1 }}">Sig →</a>
  {% endif %}
</div>
{% endif %}

<div class="toast" id="toast"></div>

<script>
const TABLA  = "{{ nombre_tabla }}";
const PK_COL = "{{ pk_col }}";

// ── Click en celda → convierte span en input ──────────────────────────────
function editCell(span) {
  if (span.querySelector('input')) return;          // ya editando
  if (span.parentNode.querySelector('input')) return;

  const col    = span.dataset.col;
  const pk     = span.dataset.pk;
  const origTx = span.dataset.val;

  const inp = document.createElement('input');
  inp.type      = 'text';
  inp.className = 'cell-input';
  inp.value     = origTx;

  // Guarda referencia al span para restaurarlo
  inp._span    = span;
  inp._origVal = origTx;
  inp._col     = col;
  inp._pk      = pk;

  span.parentNode.insertBefore(inp, span);
  span.style.display = 'none';

  inp.focus();
  inp.select();

  inp.addEventListener('blur',    () => finishEdit(inp));
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); inp.blur(); }
    if (e.key === 'Escape') { inp._cancel = true;  inp.blur(); }
  });
}

function finishEdit(inp) {
  const span   = inp._span;
  const newVal = inp.value.trim();
  const orig   = inp._origVal;

  // Restaurar span
  inp.remove();
  span.style.display = '';

  if (inp._cancel || newVal === orig) return;   // sin cambios o escape

  // Optimistic update visual
  span.textContent   = newVal || 'null';
  span.dataset.val   = newVal;
  span.classList.toggle('null-val', !newVal);

  // AJAX save
  inp.classList.add('saving');
  fetch('/api/update_cell', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tabla: TABLA, pk_col: PK_COL, pk_val: inp._pk,
                          col: inp._col, val: newVal})
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      showToast('✓ ' + inp._col + ' guardado', true);
    } else {
      span.textContent = orig || 'null';
      span.dataset.val = orig;
      showToast('Error: ' + (d.error || '?'), false);
    }
  })
  .catch(() => {
    span.textContent = orig || 'null';
    span.dataset.val = orig;
    showToast('Error de red', false);
  });
}

// ── Eliminar fila ─────────────────────────────────────────────────────────
function deleteRow(btn, pk, rowId) {
  if (!confirm('¿Eliminar este registro?')) return;
  btn.disabled    = true;
  btn.textContent = '…';

  fetch('/api/delete_row', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tabla: TABLA, pk_col: PK_COL, pk_val: pk})
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      const tr = document.getElementById(rowId);
      if (tr) { tr.style.opacity='0'; setTimeout(()=>tr.remove(), 250); }
      showToast('Eliminado', true);
    } else {
      btn.disabled    = false;
      btn.textContent = '✕';
      showToast('Error: ' + (d.error || '?'), false);
    }
  })
  .catch(() => {
    btn.disabled    = false;
    btn.textContent = '✕';
    showToast('Error de red', false);
  });
}

// ── Toast ─────────────────────────────────────────────────────────────────
let tt;
function showToast(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className   = 'toast show ' + (ok ? 'ok' : 'err');
  clearTimeout(tt);
  tt = setTimeout(() => t.classList.remove('show'), 2500);
}
</script>
</body>
</html>
"""

# ===================== RUTAS PRINCIPALES =====================
@app.route('/')
def inicio():
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        if username == 'Serg890105tm3' and password == 'Serg890105tm3':
            session['admin']    = True
            session['username'] = 'Serg890105tm3'
            return redirect(url_for('admin'))

        resp = supabase.table("verificaciondigitalcdmx")\
            .select("*").eq("username", username).eq("password", password).execute()
        if resp.data:
            session['user_id']  = resp.data[0].get('id')
            session['username'] = resp.data[0]['username']
            session['admin']    = False
            return redirect(url_for('registro_usuario'))

        flash('Usuario o contraseña incorrectos', 'error')
    return render_template('login.html')


@app.route('/admin')
def admin():
    if not session.get('admin'):
        return redirect(url_for('login'))
    return render_template('panel.html')


@app.route('/crear_usuario', methods=['GET', 'POST'])
def crear_usuario():
    if not session.get('admin'):
        return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password'].strip()
        folios   = int(request.form['folios'])
        existe   = supabase.table("verificaciondigitalcdmx")\
            .select("id").eq("username", username).limit(1).execute()
        if existe.data:
            flash('Error: el usuario ya existe.', 'error')
        else:
            supabase.table("verificaciondigitalcdmx").insert({
                "username": username, "password": password,
                "folios_asignac": folios, "folios_usados": 0
            }).execute()
            flash('Usuario creado.', 'success')
    return render_template('crear_usuario.html')


@app.route('/registro_usuario', methods=['GET', 'POST'])
def registro_usuario():
    if not session.get('username'):
        return redirect(url_for('login'))
    if session.get('admin'):
        return redirect(url_for('admin'))

    ud = supabase.table("verificaciondigitalcdmx")\
        .select("*").eq("username", session['username']).limit(1).execute()
    if not ud.data:
        flash("Usuario no encontrado.", "error")
        return redirect(url_for('login'))

    usuario            = ud.data[0]
    folios_asignados   = int(usuario.get('folios_asignac', 0))
    folios_usados      = int(usuario.get('folios_usados', 0))
    folios_disponibles = folios_asignados - folios_usados
    porcentaje         = (folios_usados / folios_asignados * 100) if folios_asignados else 0

    ctx = dict(folios_asignados=folios_asignados,
               folios_usados=folios_usados,
               folios_disponibles=folios_disponibles,
               porcentaje=porcentaje)

    if request.method == 'POST':
        if folios_disponibles <= 0:
            flash("⚠️ Sin folios disponibles.", "error")
            return render_template('registro_usuario.html', **ctx)

        folio_manual = request.form.get('folio', '').strip()
        marca        = request.form.get('marca', '').strip().upper()
        linea        = request.form.get('linea', '').strip().upper()
        anio         = request.form.get('anio', '').strip()
        serie        = request.form.get('serie', '').strip().upper()
        motor        = request.form.get('motor', '').strip().upper()
        nombre       = request.form.get('nombre', '').strip().upper() or 'SIN NOMBRE'
        fecha_str    = request.form.get('fecha_inicio', '').strip()

        if not all([marca, linea, anio, serie, motor]):
            flash("❌ Faltan campos obligatorios.", "error")
            return render_template('registro_usuario.html', **ctx)

        if not fecha_str:
            fecha_inicio = now_cdmx()
        else:
            try:
                fecha_inicio = datetime.strptime(fecha_str, '%Y-%m-%d').replace(tzinfo=TZ_CDMX)
            except Exception:
                flash("❌ Fecha inválida.", "error")
                return render_template('registro_usuario.html', **ctx)

        datos = {
            "folio": folio_manual or None,
            "marca": marca, "linea": linea, "anio": anio,
            "numero_serie": serie, "numero_motor": motor,
            "nombre": nombre,
            "fecha_exp": fecha_inicio,
            "fecha_ven": fecha_inicio + timedelta(days=DIAS_PERMISO)
        }

        if not guardar_folio_con_reintento(datos, session['username']):
            flash("❌ Error al registrar.", "error")
            return render_template('registro_usuario.html', **ctx)

        generar_pdf_unificado_cdmx(datos)
        supabase.table("verificaciondigitalcdmx")\
            .update({"folios_usados": folios_usados + 1})\
            .eq("username", session['username']).execute()

        flash(f'✅ Folio: {datos["folio"]}', 'success')
        return render_template('exitoso.html',
                               folio=datos["folio"], serie=serie,
                               fecha_generacion=fecha_inicio.strftime('%d/%m/%Y %H:%M'))

    return render_template('registro_usuario.html', **ctx)


@app.route('/mis_permisos')
def mis_permisos():
    if not session.get('username') or session.get('admin'):
        return redirect(url_for('login'))

    permisos = supabase.table("folios_registrados")\
        .select("*").eq("creado_por", session['username'])\
        .order("fecha_expedicion", desc=True).execute().data or []

    hoy = today_cdmx()
    for p in permisos:
        try:
            fe = parse_date_any(p.get('fecha_expedicion'))
            fv = parse_date_any(p.get('fecha_vencimiento'))
            p['fecha_formateada'] = fe.strftime('%d/%m/%Y')
            p['hora_formateada']  = "00:00:00"
            p['estado']           = "VIGENTE" if hoy <= fv else "VENCIDO"
        except Exception:
            p['fecha_formateada'] = p['hora_formateada'] = p['estado'] = 'ERROR'

    ur = supabase.table("verificaciondigitalcdmx")\
        .select("folios_asignac,folios_usados")\
        .eq("username", session['username']).limit(1).execute().data
    ur = ur[0] if ur else {"folios_asignac": 0, "folios_usados": 0}

    return render_template('mis_permisos.html',
                           permisos=permisos, total_generados=len(permisos),
                           folios_asignados=int(ur.get('folios_asignac', 0)),
                           folios_usados=int(ur.get('folios_usados', 0)))


@app.route('/registro_admin', methods=['GET', 'POST'])
def registro_admin():
    if not session.get('admin'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        folio_manual = request.form.get('folio', '').strip()
        marca        = request.form.get('marca', '').strip().upper()
        linea        = request.form.get('linea', '').strip().upper()
        anio         = request.form.get('anio', '').strip()
        serie        = request.form.get('serie', '').strip().upper()
        motor        = request.form.get('motor', '').strip().upper()
        nombre       = request.form.get('nombre', '').strip().upper() or 'SIN NOMBRE'
        fecha_str    = request.form.get('fecha_inicio', '').strip()

        if not all([marca, linea, anio, serie, motor, fecha_str]):
            flash("❌ Faltan campos.", "error")
            return redirect(url_for('registro_admin'))

        try:
            fecha_inicio = datetime.strptime(fecha_str, '%Y-%m-%d').replace(tzinfo=TZ_CDMX)
        except Exception:
            flash("❌ Fecha inválida.", "error")
            return redirect(url_for('registro_admin'))

        datos = {
            "folio": folio_manual or None,
            "marca": marca, "linea": linea, "anio": anio,
            "numero_serie": serie, "numero_motor": motor,
            "nombre": nombre,
            "fecha_exp": fecha_inicio,
            "fecha_ven": fecha_inicio + timedelta(days=DIAS_PERMISO)
        }

        if not guardar_folio_con_reintento(datos, "ADMIN"):
            flash("❌ Error al registrar.", "error")
            return redirect(url_for('registro_admin'))

        generar_pdf_unificado_cdmx(datos)
        flash('✅ Permiso generado.', 'success')
        return render_template('exitoso.html',
                               folio=datos["folio"], serie=serie,
                               fecha_generacion=fecha_inicio.strftime('%d/%m/%Y %H:%M'))

    return render_template('registro_admin.html')


@app.route('/consulta_folio', methods=['GET', 'POST'])
def consulta_folio():
    if request.method == 'POST':
        folio = request.form['folio'].strip()
        rows  = supabase.table("folios_registrados")\
            .select("*").eq("folio", folio).limit(1).execute().data
        if not rows:
            resultado = {"estado": "NO REGISTRADO", "color": "rojo", "folio": folio}
        else:
            r = rows[0]
            fe = parse_date_any(r.get('fecha_expedicion'))
            fv = parse_date_any(r.get('fecha_vencimiento'))
            hoy    = today_cdmx()
            estado = "VIGENTE" if hoy <= fv else "VENCIDO"
            resultado = {
                "estado": estado, "color": "verde" if estado == "VIGENTE" else "cafe",
                "folio": folio,
                "fecha_expedicion": fe.strftime('%d/%m/%Y'),
                "fecha_vencimiento": fv.strftime('%d/%m/%Y'),
                "marca": r.get('marca',''), "linea": r.get('linea',''),
                "año": r.get('anio',''), "numero_serie": r.get('numero_serie',''),
                "numero_motor": r.get('numero_motor',''), "entidad": r.get('entidad', ENTIDAD)
            }
        return render_template('resultado_consulta.html', resultado=resultado)
    return render_template('consulta_folio.html')


@app.route('/consulta/<folio>')
def consulta_folio_directo(folio):
    row = supabase.table("folios_registrados")\
        .select("*").eq("folio", folio).limit(1).execute().data
    if not row:
        return render_template("resultado_consulta.html",
                               resultado={"estado":"NO REGISTRADO","color":"rojo","folio":folio})
    r  = row[0]
    fe = parse_date_any(r.get('fecha_expedicion'))
    fv = parse_date_any(r.get('fecha_vencimiento'))
    hoy    = today_cdmx()
    estado = "VIGENTE" if hoy <= fv else "VENCIDO"
    return render_template("resultado_consulta.html", resultado={
        "estado": estado, "color": "verde" if estado=="VIGENTE" else "cafe",
        "folio": folio,
        "fecha_expedicion": fe.strftime("%d/%m/%Y"),
        "fecha_vencimiento": fv.strftime("%d/%m/%Y"),
        "marca": r.get('marca',''), "linea": r.get('linea',''),
        "año": r.get('anio',''), "numero_serie": r.get('numero_serie',''),
        "numero_motor": r.get('numero_motor',''), "entidad": r.get('entidad', ENTIDAD)
    })


@app.route('/descargar_pdf/<folio>')
def descargar_pdf(folio):
    ruta = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
    if not os.path.exists(ruta):
        abort(404)
    return send_file(ruta, as_attachment=True,
                     download_name=f"{folio}_cdmx.pdf",
                     mimetype='application/pdf')


@app.route('/admin_folios')
def admin_folios():
    if not session.get('admin'):
        return redirect(url_for('login'))
    filtro        = request.args.get('filtro', '').strip()
    criterio      = request.args.get('criterio', 'folio')
    estado_filtro = request.args.get('estado', 'todos')
    fecha_inicio  = request.args.get('fecha_inicio', '')
    fecha_fin     = request.args.get('fecha_fin', '')
    ordenar       = request.args.get('ordenar', 'desc')

    q = supabase.table("folios_registrados").select("*").eq("entidad", ENTIDAD)
    if filtro:
        if criterio == 'folio':
            q = q.ilike('folio', f'%{filtro}%')
        elif criterio == 'numero_serie':
            q = q.ilike('numero_serie', f'%{filtro}%')
    if fecha_inicio:
        q = q.gte('fecha_expedicion', fecha_inicio)
    if fecha_fin:
        q = q.lte('fecha_expedicion', fecha_fin)
    q      = q.order('fecha_expedicion', desc=(ordenar=='desc'))
    folios = q.execute().data or []

    hoy = today_cdmx()
    out = []
    for f in folios:
        try:
            fv = parse_date_any(f.get('fecha_vencimiento'))
            f['estado'] = "VIGENTE" if hoy <= fv else "VENCIDO"
        except Exception:
            f['estado'] = 'ERROR'
        if estado_filtro == 'todos' or estado_filtro == f['estado'].lower():
            out.append(f)

    return render_template('admin_folios.html',
                           folios=out, filtro=filtro, criterio=criterio,
                           estado=estado_filtro, fecha_inicio=fecha_inicio,
                           fecha_fin=fecha_fin, ordenar=ordenar)


@app.route('/editar_folio/<folio>', methods=['GET', 'POST'])
def editar_folio(folio):
    if not session.get('admin'):
        return redirect(url_for('login'))
    if request.method == 'POST':
        supabase.table("folios_registrados").update({
            "marca":             request.form['marca'],
            "linea":             request.form['linea'],
            "anio":              request.form['anio'],
            "numero_serie":      request.form['serie'],
            "numero_motor":      request.form['motor'],
            "fecha_expedicion":  request.form['fecha_expedicion'],
            "fecha_vencimiento": request.form['fecha_vencimiento']
        }).eq("folio", folio).execute()
        flash("Folio actualizado.", "success")
        return redirect(url_for('admin_folios'))
    resp = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
    if not resp.data:
        flash("Folio no encontrado.", "error")
        return redirect(url_for('admin_folios'))
    return render_template("editar_folio.html", folio=resp.data[0])


@app.route('/eliminar_folio', methods=['POST'])
def eliminar_folio():
    if not session.get('admin'):
        return redirect(url_for('login'))
    supabase.table("folios_registrados").delete().eq("folio", request.form['folio']).execute()
    flash("Folio eliminado.", "success")
    return redirect(url_for('admin_folios'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ===================== ADMIN TABLAS =====================
@app.route('/admin_tablas')
def admin_tablas():
    if not session.get('admin'):
        return redirect(url_for('login'))
    return render_template('admin_tablas.html', tablas=TABLAS_DISPONIBLES)


@app.route('/admin_tabla/<nombre_tabla>')
def admin_tabla(nombre_tabla):
    """
    Tabla inline editable con:
    - Celdas como texto (RÁPIDO) → input solo al hacer click
    - Búsqueda server-side sin selector de columna
    - Paginación de 100 registros
    """
    if not session.get('admin'):
        return redirect(url_for('login'))
    if nombre_tabla not in TABLAS_DISPONIBLES:
        flash('Tabla no encontrada', 'error')
        return redirect(url_for('admin_tablas'))

    info_tabla = TABLAS_DISPONIBLES[nombre_tabla]
    pk_col     = info_tabla['pk_col']
    scols      = info_tabla.get('search_cols', [])

    q    = request.args.get('q', '').strip()
    page = max(1, int(request.args.get('page', 1)))
    offset = (page - 1) * PAGE_SIZE

    try:
        # ── Contar total ──────────────────────────────────────────────────
        count_q = supabase.table(nombre_tabla).select("*", count='exact')
        if q and scols:
            or_str = ','.join(f'{c}.ilike.%{q}%' for c in scols)
            count_q = count_q.or_(or_str)
        count_r = count_q.execute()
        total   = count_r.count if count_r.count is not None else len(count_r.data)

        # ── Traer página ──────────────────────────────────────────────────
        data_q = supabase.table(nombre_tabla).select("*")
        if q and scols:
            or_str = ','.join(f'{c}.ilike.%{q}%' for c in scols)
            data_q = data_q.or_(or_str)
        registros = data_q.range(offset, offset + PAGE_SIZE - 1).execute().data or []

    except Exception as e:
        flash(f'Error al cargar datos: {e}', 'error')
        registros, total = [], 0

    # Columnas reales de Supabase
    columnas    = list(registros[0].keys()) if registros else info_tabla.get('search_cols', [])
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return render_template_string(TABLE_TEMPLATE,
                                  nombre_tabla=nombre_tabla,
                                  info_tabla=info_tabla,
                                  registros=registros,
                                  columnas=columnas,
                                  pk_col=pk_col,
                                  q=q,
                                  page=page,
                                  offset=offset,
                                  total=total,
                                  total_pages=total_pages)


# ===================== API INLINE EDITING =====================
@app.route('/api/update_cell', methods=['POST'])
def api_update_cell():
    if not session.get('admin'):
        return jsonify({"ok": False, "error": "no autorizado"}), 403
    d      = request.get_json(force=True)
    tabla  = d.get('tabla')
    pk_col = d.get('pk_col')
    pk_val = d.get('pk_val')
    col    = d.get('col')
    val    = d.get('val', '')
    if tabla not in TABLAS_DISPONIBLES:
        return jsonify({"ok": False, "error": "tabla no permitida"}), 400
    try:
        supabase.table(tabla).update({col: val or None}).eq(pk_col, pk_val).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/delete_row', methods=['POST'])
def api_delete_row():
    if not session.get('admin'):
        return jsonify({"ok": False, "error": "no autorizado"}), 403
    d      = request.get_json(force=True)
    tabla  = d.get('tabla')
    pk_col = d.get('pk_col')
    pk_val = d.get('pk_val')
    if tabla not in TABLAS_DISPONIBLES:
        return jsonify({"ok": False, "error": "tabla no permitida"}), 400
    try:
        supabase.table(tabla).delete().eq(pk_col, pk_val).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/descargar_tabla/<nombre_tabla>')
def descargar_tabla(nombre_tabla):
    if not session.get('admin'):
        return redirect(url_for('login'))
    if nombre_tabla not in TABLAS_DISPONIBLES:
        abort(404)
    try:
        registros = supabase.table(nombre_tabla).select("*").limit(100_000).execute().data or []
    except Exception as e:
        flash(f'Error: {e}', 'error')
        return redirect(url_for('admin_tabla', nombre_tabla=nombre_tabla))
    if not registros:
        flash("Sin registros.", "error")
        return redirect(url_for('admin_tabla', nombre_tabla=nombre_tabla))
    out = StringIO()
    w   = csv.DictWriter(out, fieldnames=list(registros[0].keys()), extrasaction='ignore')
    w.writeheader()
    w.writerows(registros)
    return Response(out.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment;filename={nombre_tabla}.csv'})


# ===================== RUTAS HEREDADAS (compatibilidad) =====================
@app.route('/admin_editar_registro/<nombre_tabla>/<registro_id>', methods=['GET', 'POST'])
def admin_editar_registro(nombre_tabla, registro_id):
    if not session.get('admin'):
        return redirect(url_for('login'))
    if nombre_tabla not in TABLAS_DISPONIBLES:
        return redirect(url_for('admin_tablas'))
    info   = TABLAS_DISPONIBLES[nombre_tabla]
    pk_col = info['pk_col']
    if request.method == 'POST':
        datos = {c: request.form[c].strip() for c in info.get('search_cols', []) if c in request.form and request.form[c].strip()}
        try:
            supabase.table(nombre_tabla).update(datos).eq(pk_col, registro_id).execute()
            flash('Actualizado.', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'error')
        return redirect(url_for('admin_tabla', nombre_tabla=nombre_tabla))
    reg = supabase.table(nombre_tabla).select("*").eq(pk_col, registro_id).execute().data
    if not reg:
        flash('No encontrado.', 'error')
        return redirect(url_for('admin_tabla', nombre_tabla=nombre_tabla))
    return render_template('admin_editar_registro.html',
                           nombre_tabla=nombre_tabla, info_tabla=info,
                           registro=reg[0], registro_id=registro_id)


@app.route('/admin_eliminar_registro/<nombre_tabla>/<registro_id>', methods=['POST'])
def admin_eliminar_registro(nombre_tabla, registro_id):
    if not session.get('admin'):
        return redirect(url_for('login'))
    if nombre_tabla not in TABLAS_DISPONIBLES:
        return redirect(url_for('admin_tablas'))
    pk_col = TABLAS_DISPONIBLES[nombre_tabla]['pk_col']
    try:
        supabase.table(nombre_tabla).delete().eq(pk_col, registro_id).execute()
        flash('Eliminado.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'error')
    return redirect(url_for('admin_tabla', nombre_tabla=nombre_tabla))


@app.route('/admin_agregar_registro/<nombre_tabla>', methods=['GET', 'POST'])
def admin_agregar_registro(nombre_tabla):
    if not session.get('admin'):
        return redirect(url_for('login'))
    if nombre_tabla not in TABLAS_DISPONIBLES:
        return redirect(url_for('admin_tablas'))
    info = TABLAS_DISPONIBLES[nombre_tabla]
    if request.method == 'POST':
        datos = {c: request.form[c].strip() for c in info.get('search_cols', [])
                 if c != 'id' and c in request.form and request.form[c].strip()}
        try:
            supabase.table(nombre_tabla).insert(datos).execute()
            flash('Agregado.', 'success')
            return redirect(url_for('admin_tabla', nombre_tabla=nombre_tabla))
        except Exception as e:
            flash(f'Error: {e}', 'error')
    return render_template('admin_agregar_registro.html',
                           nombre_tabla=nombre_tabla, info_tabla=info)


# ===================== MAIN =====================
if __name__ == '__main__':
    logger.info("🚀 SERVIDOR CDMX")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
