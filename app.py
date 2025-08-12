# app.py
# -------------------------------------------------------------
# Aiogram v2 (FSM) + FastAPI webhook
# Flujo /permiso: marca → linea → anio → serie → motor → nombre
# PDF desde plantilla cdmxdigital2025ppp.pdf con QR
# Archivos en /tmp (evita reload en Render)
# Folio único con tabla folios_unicos (columna fol, prefijo NOT NULL)
# Guarda datos en borradores_registros (columna folio)
# Supabase con service_role y pequeños reintentos
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

# Tablas
TABLE_FOLIOS = "folios_unicos"           # columnas: id (pk), prefijo NOT NULL, fol (texto), entidad, ...
TABLE_REGISTROS = "borradores_registros" # columna clave para join: folio

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

# ---------- UTILS ----------
def _slug(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    s2 = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s2)

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
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).eq("id", nid).execute()
    except Exception as e:
        logger.warning(f"No pude actualizar 'fol' en {TABLE_FOLIOS}: {e}")
    return folio

# ---------- PDF ----------
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

    pg.insert_text(coords_cdmx["folio"][:2], datos["folio"], fontsize=coords_cdmx["folio"][2], color=coords_cdmx["folio"][3])
    pg.insert_text(coords_cdmx["fecha"][:2], fecha_visual, fontsize=coords_cdmx["fecha"][2], color=coords_cdmx["fecha"][3])

    for key in ["marca", "serie", "linea", "motor", "anio"]:
        x, y, s, col = coords_cdmx[key]
        pg.insert_text((x, y), str(datos[key]), fontsize=s, color=col)

    pg.insert_text(coords_cdmx["vigencia"][:2], vigencia_visual, fontsize=coords_cdmx["vigencia"][2], color=coords_cdmx["vigencia"][3])
    pg.insert_text(coords_cdmx["nombre"][:2], datos["nombre"], fontsize=coords_cdmx["nombre"][2], color=coords_cdmx["nombre"][3])

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

def _upload_pdf(path_local: str, nombre_pdf: str) -> str:
    nombre_pdf = _slug(nombre_pdf)
    with open(path_local, "rb") as f:
        data = f.read()
    supabase.storage.from_(BUCKET).upload(nombre_pdf, data, {"contentType": "application/pdf", "upsert": True})
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{nombre_pdf}"

# ---------- FSM ----------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# ---------- HANDLERS ----------
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
    await state.update_data(marca=m.text.strip())
    await m.answer("Línea:")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea)
async def form_linea(m: types.Message, state: FSMContext):
    await state.update_data(linea=m.text.strip())
    await m.answer("Año:")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio)
async def form_anio(m: types.Message, state: FSMContext):
    await state.update_data(anio=m.text.strip())
    await m.answer("Serie:")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie)
async def form_serie(m: types.Message, state: FSMContext):
    await state.update_data(serie=m.text.strip())
    await m.answer("Motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor)
async def form_motor(m: types.Message, state: FSMContext):
    await state.update_data(motor=m.text.strip())
    await m.answer("Nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre)
async def form_nombre(m: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = m.text.strip()

    folio = nuevo_folio(FOLIO_PREFIX)
    datos["folio"] = folio
    fecha_exp = datetime.now().date()
    fecha_ven = fecha_exp + timedelta(days=30)

    await m.answer("⏳ Generando PDF…")

    try:
        path_pdf = await asyncio.to_thread(_make_pdf, datos)
        nombre_pdf = _slug(f"{folio}_cdmx_{int(time.time())}.pdf")
        url_pdf = await asyncio.to_thread(_upload_pdf, path_pdf, nombre_pdf)

        with open(path_pdf, "rb") as f:
            await m.answer_document(f, caption=f"Folio: {folio}\nPDF: {url_pdf}")

        await supabase_insert_retry(TABLE_REGISTROS, {
            "folio": folio,
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": str(datos["anio"]),
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "nombre": datos["nombre"],
            "entidad": "CDMX",
            "url_pdf": url_pdf,
            "fecha_expedicion": fecha_exp.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
        })

        await supabase_update_retry(TABLE_FOLIOS, {"fol": folio}, {
            "url_pdf": url_pdf,
            "fecha_expedicion": fecha_exp.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
        })

    except Exception as e:
        logger.exception("Error generando PDF")
        await m.answer(f"❌ Error: {e}")

    await state.finish()

# ---------- FASTAPI ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook")
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.get("/")
def health():
    return {"ok": True}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    Bot.set_current(bot)
    Dispatcher.set_current(dp)
    update = types.Update(**data)
    await dp.process_update(update)
    return {"ok": True}
