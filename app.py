# app.py
# -------------------------------------------------------------
# Aiogram v2 (FSM) + FastAPI webhook (Render)
# Flujo /permiso: marca → linea → anio → serie → motor → nombre
# PDF desde plantilla cdmxdigital2025ppp.pdf con QR
# Archivos en /tmp (evita reload en Render)
# Folio único con tabla folios_unicos (columna fol; prefijo NOT NULL)
# Guarda datos en borradores_registros (columna folio)
# Supabase con service_role y reintentos básicos
# -------------------------------------------------------------

import os
import re
import time
import unicodedata
import logging
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import fitz               # PyMuPDF
import qrcode
from fastapi import FastAPI, Request

# Aiogram v2
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# Supabase
from supabase import create_client, Client

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("permiso-bot")
logger.info("BOOT permiso-bot v2.2")

# ---------- ENV VARS ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise ValueError("SUPABASE_URL o SUPABASE_SERVICE_KEY no están configurados")

BUCKET = os.getenv("BUCKET", "pdfs")
FOLIO_PREFIX = os.getenv("FOLIO_PREFIX", "05")

# Rutas/archivos
OUTPUT_DIR = "/tmp/pdfs"         # evita reload en Render
os.makedirs(OUTPUT_DIR, exist_ok=True)
PLANTILLA_PDF = os.path.join(os.path.dirname(__file__), "cdmxdigital2025ppp.pdf")
if not os.path.exists(PLANTILLA_PDF):
    raise FileNotFoundError("No se encontró cdmxdigital2025ppp.pdf junto a app.py")

# Tablas
TABLE_FOLIOS = "folios_unicos"            # id (pk), prefijo NOT NULL, fol, entidad, ...
TABLE_REGISTROS = "borradores_registros"  # columna clave: folio

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- BOT ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

# ---------- COORDENADAS ----------
coords_cdmx = {
    "folio":   (87, 130, 14, (1, 0, 0)),
    "fecha":   (130, 145, 12, (0, 0, 0)),
    "marca":   (87, 290, 11, (0, 0, 0)),
    "serie":   (375, 290, 11, (0, 0, 0)),
    "linea":   (87, 307, 11, (0, 0, 0)),
    "motor":   (375, 307, 11, (0, 0, 0)),
    "anio":    (87, 323, 11, (0, 0, 0)),
    "vigencia":(375, 323, 11, (0, 0, 0)),
    "nombre":  (375, 340, 11, (0, 0, 0)),
}

# ---------- HELPERS / FUNCIONES PRO ----------
def _slug(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    s2 = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s2).strip("_") or "archivo"

def _safe_text(s: str, maxlen: int = 80) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:maxlen]

def _valid_year(s: str) -> str:
    s = re.sub(r"\D", "", s or "")
    if len(s) == 2:  # permisivo: 19 → 2019 si parece razonable
        s = "20" + s
    if len(s) != 4:
        return "2025"
    y = int(s)
    if y < 1960 or y > datetime.now().year + 1:
        return "2025"
    return str(y)

async def supabase_insert_retry(table: str, row: dict, attempts: int = 4, delay: float = 0.6):
    last = None
    for i in range(attempts):
        try:
            res = supabase.table(table).insert(row).execute()
            return res.data
        except Exception as e:
            last = e
            logger.warning(f"[Supabase insert retry {i+1}/{attempts}] {e}")
            await asyncio.sleep(delay * (i + 1))
    raise last

async def supabase_update_retry(table: str, match: dict, updates: dict, attempts: int = 4, delay: float = 0.6):
    last = None
    for i in range(attempts):
        try:
            q = supabase.table(table)
            for k, v in match.items():
                q = q.eq(k, v)
            res = q.update(updates).execute()
            return res.data
        except Exception as e:
            last = e
            logger.warning(f"[Supabase update retry {i+1}/{attempts}] {e}")
            await asyncio.sleep(delay * (i + 1))
    raise last

def nuevo_folio(prefix: str = FOLIO_PREFIX) -> str:
    """
    Inserta fila válida (cumple NOT NULL en 'prefijo') y calcula:
    folio = prefix + id (6 dígitos).
    """
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).eq("id", nid).execute()
    except Exception as e:
        logger.warning(f"No pude actualizar 'fol' en {TABLE_FOLIOS}: {e}")
    return folio

# Subida a Storage (headers como STRING, no bool → evita 'encode' error)
def _upload_pdf(path_local: str, nombre_pdf: str) -> str:
    nombre_pdf = _slug(nombre_pdf)
    with open(path_local, "rb") as f:
        data = f.read()
    supabase.storage.from_(BUCKET).upload(
        nombre_pdf,
        data,
        {"contentType": "application/pdf", "cacheControl": "3600", "upsert": "true"},
    )
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{nombre_pdf}"

# Generación de PDF
def _make_pdf(datos: dict) -> str:
    out_path = os.path.join(OUTPUT_DIR, f"{datos['folio']}_cdmx.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)
    meses = {
        1:"ENERO",2:"FEBRERO",3:"MARZO",4:"ABRIL",5:"MAYO",6:"JUNIO",
        7:"JULIO",8:"AGOSTO",9:"SEPTIEMBRE",10:"OCTUBRE",11:"NOVIEMBRE",12:"DICIEMBRE"
    }
    fecha_visual = f"{fecha_exp.day:02d} DE {meses[fecha_exp.month]} DEL {fecha_exp.year}"
    vigencia_visual = fecha_ven.strftime("%d/%m/%Y")

    # Texto
    pg.insert_text(coords_cdmx["folio"][:2], datos["folio"], fontsize=coords_cdmx["folio"][2], color=coords_cdmx["folio"][3])
    pg.insert_text(coords_cdmx["fecha"][:2], fecha_visual, fontsize=coords_cdmx["fecha"][2], color=coords_cdmx["fecha"][3])

    for key in ["marca", "serie", "linea", "motor", "anio"]:
        x, y, s, col = coords_cdmx[key]
        pg.insert_text((x, y), str(datos.get(key, "")), fontsize=s, color=col)

    pg.insert_text(coords_cdmx["vigencia"][:2], vigencia_visual, fontsize=coords_cdmx["vigencia"][2], color=coords_cdmx["vigencia"][3])
    pg.insert_text(coords_cdmx["nombre"][:2], datos["nombre"], fontsize=coords_cdmx["nombre"][2], color=coords_cdmx["nombre"][3])

    # QR
    qr_text = (
        f"Folio: {datos['folio']}\n"
        f"Marca: {datos['marca']}\n"
        f"Línea: {datos['linea']}\n"
        f"Año: {datos['anio']}\n"
        f"Serie: {datos['serie']}\n"
        f"Motor: {datos['motor']}\n"
        f"Nombre: {datos['nombre']}\n"
        "SEMOVICDMX DIGITAL"
    )
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=2)
    qr.add_data(qr_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    qr_png = os.path.join(OUTPUT_DIR, f"{datos['folio']}_qr.png")
    img.save(qr_png)

    tam_qr = 1.6 * 28.35
    ancho_pagina = pg.rect.width
    x0 = (ancho_pagina / 2) - (tam_qr / 2) - 19
    x1 = (ancho_pagina / 2) + (tam_qr / 2) - 19
    y0 = 680.17
    y1 = y0 + tam_qr
    pg.insert_image(fitz.Rect(x0, y0, x1, y1), filename=qr_png, keep_proportion=False, overlay=True)

    doc.save(out_path)
    doc.close()
    return out_path

# ---------- FSM ----------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# Rate-limit súper simple por usuario para evitar flujos duplicados
_user_lock = {}
def _is_locked(chat_id: int) -> bool:
    now = time.time()
    exp = _user_lock.get(chat_id, 0)
    if exp > now:
        return True
    _user_lock[chat_id] = now + 10  # 10s de ventana anti-spam
    return False

# ---------- HANDLERS ----------
@dp.message_handler(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("👋 Bot listo.\nUsa /permiso para iniciar el registro.")

@dp.message_handler(Command("permiso"))
async def permiso_init(m: types.Message, state: FSMContext):
    if _is_locked(m.chat.id):
        return await m.answer("⏳ Dame unos segundos…")
    await state.finish()
    await m.answer("Marca del vehículo:")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca, content_types=types.ContentTypes.TEXT)
async def form_marca(m: types.Message, state: FSMContext):
    await state.update_data(marca=_safe_text(m.text))
    await m.answer("Línea (modelo/versión):")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea, content_types=types.ContentTypes.TEXT)
async def form_linea(m: types.Message, state: FSMContext):
    await state.update_data(linea=_safe_text(m.text))
    await m.answer("Año (4 dígitos):")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio, content_types=types.ContentTypes.TEXT)
async def form_anio(m: types.Message, state: FSMContext):
    await state.update_data(anio=_valid_year(m.text))
    await m.answer("Serie (VIN):")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie, content_types=types.ContentTypes.TEXT)
async def form_serie(m: types.Message, state: FSMContext):
    await state.update_data(serie=_safe_text(m.text, 40))
    await m.answer("Motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor, content_types=types.ContentTypes.TEXT)
async def form_motor(m: types.Message, state: FSMContext):
    await state.update_data(motor=_safe_text(m.text, 40))
    await m.answer("Nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre, content_types=types.ContentTypes.TEXT)
async def form_nombre(m: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = _safe_text(m.text, 60)

    # 1) Folio único (cumple NOT NULL en 'prefijo')
    folio = nuevo_folio(FOLIO_PREFIX)
    datos["folio"] = folio

    # 2) Fechas
    fecha_exp = datetime.now().date()
    fecha_ven = fecha_exp + timedelta(days=30)

    await m.answer("⏳ Generando tu PDF…")

    try:
        # 3) PDF en /tmp
        path_pdf = await asyncio.to_thread(_make_pdf, datos)

        # 4) Subir a Storage
        nombre_pdf = _slug(f"{folio}_cdmx_{int(time.time())}.pdf")
        url_pdf = await asyncio.to_thread(_upload_pdf, path_pdf, nombre_pdf)

        # 5) Enviar PDF
        with open(path_pdf, "rb") as f:
            await m.answer_document(
                f,
                caption=(
                    f"✅ Registro generado\n"
                    f"Folio: {folio}\n"
                    f"{datos.get('marca','')} {datos.get('linea','')} ({datos.get('anio','')})\n"
                    f"PDF: {url_pdf}"
                ),
            )

        # 6) Guardar en borradores_registros
        await supabase_insert_retry(TABLE_REGISTROS, {
            "folio": folio,
            "marca": datos.get("marca", ""),
            "linea": datos.get("linea", ""),
            "anio": str(datos.get("anio", "")),
            "numero_serie": datos.get("serie", ""),
            "numero_motor": datos.get("motor", ""),
            "nombre": datos.get("nombre", ""),
            "entidad": "CDMX",
            "url_pdf": url_pdf,
            "fecha_expedicion": fecha_exp.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
        })

        # 7) Actualizar fila de folios_unicos con url y fechas (opcional)
        try:
            await supabase_update_retry(
                TABLE_FOLIOS,
                {"fol": folio},
                {
                    "url_pdf": url_pdf,
                    "fecha_expedicion": fecha_exp.isoformat(),
                    "fecha_vencimiento": fecha_ven.isoformat(),
                },
            )
        except Exception as e:
            logger.warning(f"No se pudo actualizar {TABLE_FOLIOS}: {e}")

    except Exception as e:
        logger.exception("Fallo generando/enviando PDF")
        await m.answer(f"❌ Error generando PDF: {e}")

    await state.finish()

# Fallback
@dp.message_handler()
async def fallback(m: types.Message):
    await m.answer("No entendí. Usa /permiso para iniciar.")

# ---------- FASTAPI ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Iniciando webhook…")
    try:
        if BASE_URL:
            await bot.set_webhook(f"{BASE_URL}/webhook")
            logger.info(f"Webhook OK: {BASE_URL}/webhook")
        else:
            logger.warning("BASE_URL no configurada; sin webhook.")
    except Exception as e:
        logger.warning(f"No se pudo setear webhook: {e}")
    yield
    try:
        await bot.delete_webhook()
    except Exception:
        pass

app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.get("/")
def health():
    return {"ok": True, "webhook": f"{BASE_URL}/webhook" if BASE_URL else None}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": True, "note": "bad_json"}

    # Aiogram v2 requiere setear el contexto actual
    Bot.set_current(bot)
    Dispatcher.set_current(dp)

    update = types.Update(**data)
    await dp.process_update(update)
    return {"ok": True}
