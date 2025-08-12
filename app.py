# app.py - Bot permisos digitales CDMX con webhook persistente y keepalive

import os
import re
import time
import unicodedata
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import fitz               # PyMuPDF
import qrcode
import requests
from fastapi import FastAPI, Request

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from supabase import create_client, Client

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("permiso-bot")
logger.info("BOOT permiso-bot ⚙️")

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()

# Fallback con service_role expuesta (me pediste así)
SUPABASE_SERVICE_KEY = os.getenv(
    "SUPABASE_SERVICE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0Mzk2Mzc1NSwiZXhwIjoyMDU5NTM5NzU1fQ.aaTWr2E_l20TlWjdZgKp3ddd3bmtnL22jZisvT_aN0w"
)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")
if not SUPABASE_URL:
    raise ValueError("SUPABASE_URL no está configurado")
if not BASE_URL:
    raise ValueError("BASE_URL no está configurado (URL pública de Render)")

BUCKET = os.getenv("BUCKET", "pdfs").strip()
FOLIO_PREFIX = os.getenv("FOLIO_PREFIX", "05").strip()

# ---------- Rutas / Archivos ----------
OUTPUT_DIR = "/tmp/pdfs"
os.makedirs(OUTPUT_DIR, exist_ok=True)
PLANTILLA_PDF = os.path.join(os.path.dirname(__file__), "cdmxdigital2025ppp.pdf")
if not os.path.exists(PLANTILLA_PDF):
    raise FileNotFoundError("No se encontró cdmxdigital2025ppp.pdf junto a app.py")

# ---------- Supabase ----------
TABLE_FOLIOS = "folios_unicos"
TABLE_REGISTROS = "borradores_registros"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- BOT ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

# ---------- Coordenadas ----------
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

# ---------- Utils ----------
def _slug(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    s2 = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s2)

def nuevo_folio(prefix: str = FOLIO_PREFIX) -> str:
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).eq("id", nid).execute()
    except Exception as e:
        logger.warning(f"No pude actualizar 'fol' en {TABLE_FOLIOS}: {e}")
    return folio

def _make_pdf(datos: dict) -> str:
    out_path = os.path.join(OUTPUT_DIR, f"{datos['folio']}_cdmx.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)
    meses = {1:"ENERO",2:"FEBRERO",3:"MARZO",4:"ABRIL",5:"MAYO",6:"JUNIO",
             7:"JULIO",8:"AGOSTO",9:"SEPTIEMBRE",10:"OCTUBRE",11:"NOVIEMBRE",12:"DICIEMBRE"}
    fecha_visual = f"{fecha_exp.day:02d} DE {meses[fecha_exp.month]} DEL {fecha_exp.year}"
    vigencia_visual = fecha_ven.strftime("%d/%m/%Y")

    pg.insert_text(coords_cdmx["folio"][:2], datos["folio"], fontsize=coords_cdmx["folio"][2], color=coords_cdmx["folio"][3])
    pg.insert_text(coords_cdmx["fecha"][:2], fecha_visual, fontsize=coords_cdmx["fecha"][2], color=coords_cdmx["fecha"][3])

    for key in ["marca", "serie", "linea", "motor", "anio"]:
        x, y, s, col = coords_cdmx[key]
        pg.insert_text((x, y), str(datos.get(key, "")), fontsize=s, color=col)

    pg.insert_text(coords_cdmx["vigencia"][:2], vigencia_visual, fontsize=coords_cdmx["vigencia"][2], color=coords_cdmx["vigencia"][3])
    pg.insert_text(coords_cdmx["nombre"][:2], datos.get("nombre", ""), fontsize=coords_cdmx["nombre"][2], color=coords_cdmx["nombre"][3])

    qr_text = (
        f"Folio: {datos['folio']}\n"
        f"Marca: {datos.get('marca','')}\n"
        f"Línea: {datos.get('linea','')}\n"
        f"Año: {datos.get('anio','')}\n"
        f"Serie: {datos.get('serie','')}\n"
        f"Motor: {datos.get('motor','')}\n"
        f"Nombre: {datos.get('nombre','')}\n"
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

def _upload_pdf(path_local: str, nombre_pdf: str) -> str:
    nombre_pdf = _slug(nombre_pdf)
    with open(path_local, "rb") as f:
        data = f.read()
    supabase.storage.from_(BUCKET).upload(
        nombre_pdf,
        data,
        {"contentType": "application/pdf", "upsert": "true"}
    )
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{nombre_pdf}"

# ---------- FSM ----------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# ---------- Handlers ----------
@dp.message_handler(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("👋 Bot listo.\nUsa /permiso para iniciar el registro.")

@dp.message_handler(Command("permiso"))
async def permiso_init(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("Marca del vehículo:")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca)
async def form_marca(m: types.Message, state: FSMContext):
    await state.update_data(marca=(m.text or "").strip())
    await m.answer("Línea:")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea)
async def form_linea(m: types.Message, state: FSMContext):
    await state.update_data(linea=(m.text or "").strip())
    await m.answer("Año:")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio)
async def form_anio(m: types.Message, state: FSMContext):
    await state.update_data(anio=(m.text or "").strip())
    await m.answer("Serie:")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie)
async def form_serie(m: types.Message, state: FSMContext):
    await state.update_data(serie=(m.text or "").strip())
    await m.answer("Motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor)
async def form_motor(m: types.Message, state: FSMContext):
    await state.update_data(motor=(m.text or "").strip())
    await m.answer("Nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre)
async def form_nombre(m: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = (m.text or "").strip()

    folio = nuevo_folio(FOLIO_PREFIX)
    datos["folio"] = folio

    fecha_exp = datetime.now().date()
    fecha_ven = fecha_exp + timedelta(days=30)

    await m.answer("⏳ Generando tu PDF…")

    try:
        path_pdf = await asyncio.to_thread(_make_pdf, datos)
        nombre_pdf = _slug(f"{folio}_cdmx_{int(time.time())}.pdf")
        url_pdf = await asyncio.to_thread(_upload_pdf, path_pdf, nombre_pdf)

        with open(path_pdf, "rb") as f:
            await m.answer_document(f, caption=f"✅ Folio: {folio}\nPDF: {url_pdf}")

        supabase.table(TABLE_REGISTROS).insert({
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
        }).execute()

    except Exception as e:
        logger.exception("Fallo generando/enviando PDF")
        await m.answer(f"❌ Error generando PDF: {e}")

    await state.finish()

# ---------- FastAPI ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Iniciando webhook…")
    try:
        await bot.set_webhook(f"{BASE_URL}/webhook", drop_pending_updates=True)
        logger.info(f"Webhook activo: {BASE_URL}/webhook")
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
    return {"ok": True, "webhook": f"{BASE_URL}/webhook"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    Bot.set_current(bot)
    Dispatcher.set_current(dp)
    update = types.Update(**data)
    await dp.process_update(update)
    return {"ok": True}

# ---------- Keepalive ----------
def keepalive():
    while True:
        try:
            requests.get(f"{BASE_URL}/")
            logger.info("Keepalive enviado ✅")
        except Exception as e:
            logger.warning(f"Keepalive falló: {e}")
        time.sleep(240)  # cada 4 min

threading.Thread(target=keepalive, daemon=True).start()
