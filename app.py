# app.py
# -------------------------------------------------------------
# Bot de Telegram (Aiogram v2) + FastAPI (webhook en Render)
# Flujo: /permiso -> pide datos -> genera PDF en plantilla ->
# sube a Supabase Storage -> guarda registro en tabla.
# -------------------------------------------------------------

import os
import re
import logging
import tempfile
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import fitz  # PyMuPDF
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
BASE_URL = os.getenv("BASE_URL", "")  # p.ej. https://tuapp.onrender.com
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")

# Supabase (tú pasaste estas credenciales)
SUPABASE_URL = "https://xsagwqepoljfsogusubw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws"
BUCKET = "pdfs"
OUTPUT_DIR = "static/pdfs"
TEMPLATE_PDF = "cdmxdigital2025ppp.pdf"  # Debe existir en el repo

# Crear cliente de Supabase (¡en su propia línea!)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- BOT ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

# ---------- COORDENADAS (NO TOCAR) ----------
coords_cdmx = {
    "folio": (87, 130, 14, (1, 0, 0)),
    "fecha": (130, 145, 12, (0, 0, 0)),
    "marca": (87, 290, 11, (0, 0, 0)),
    "serie": (375, 290, 11, (0, 0, 0)),
    "linea": (87, 307, 11, (0, 0, 0)),
    "motor": (375, 307, 11, (0, 0, 0)),
    "anio": (87, 323, 11, (0, 0, 0)),
    "vigencia": (375, 323, 11, (0, 0, 0)),
    "nombre": (375, 340, 11, (0, 0, 0)),
}

# ---------- HELPERS SUPABASE ----------
def subir_pdf_supabase(path_local: str, nombre_pdf: str) -> str:
    with open(path_local, "rb") as f:
        data = f.read()
    # Si ya existe, intenta eliminarlo antes
    try:
        supabase.storage.from_(BUCKET).remove([nombre_pdf])
    except Exception:
        pass
    supabase.storage.from_(BUCKET).upload(
        nombre_pdf, data, {"content-type": "application/pdf"}
    )
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{nombre_pdf}"

def guardar_supabase(data: dict):
    supabase.table("borradores_registros").insert(data).execute()

def generar_folio_automatico(prefijo: str = "05") -> str:
    res = (
        supabase.table("borradores_registros")
        .select("folio")
        .order("folio", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        ultimo = res.data[0]["folio"]
        try:
            num = int(ultimo[len(prefijo):]) + 1
        except Exception:
            num = 1
    else:
        num = 1
    return f"{prefijo}{num}"

# ---------- FSM ----------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# ---------- PDF ----------
MESES_ES = {
    "January": "ENERO", "February": "FEBRERO", "March": "MARZO",
    "April": "ABRIL", "May": "MAYO", "June": "JUNIO",
    "July": "JULIO", "August": "AGOSTO", "September": "SEPTIEMBRE",
    "October": "OCTUBRE", "November": "NOVIEMBRE", "December": "DICIEMBRE",
}

def _make_pdf(datos: dict) -> str:
    """
    Rellena la plantilla 'cdmxdigital2025ppp.pdf' con coords_cdmx y genera QR.
    Devuelve la ruta local del PDF generado.
    """
    if not os.path.exists(TEMPLATE_PDF):
        raise FileNotFoundError(f"No se encontró la plantilla: {TEMPLATE_PDF}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path_out = os.path.join(OUTPUT_DIR, f"{datos['folio']}_cdmx.pdf")

    doc = fitz.open(TEMPLATE_PDF)
    pg = doc[0]

    # Fechas
    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)
    fecha_visual = f"{fecha_exp.day:02d} DE {MESES_ES[fecha_exp.strftime('%B')]} DEL {fecha_exp.year}"
    vigencia_visual = fecha_ven.strftime("%d/%m/%Y")

    # Texto
    pg.insert_text(coords_cdmx["folio"][:2], datos["folio"],
                   fontsize=coords_cdmx["folio"][2], color=coords_cdmx["folio"][3])
    pg.insert_text(coords_cdmx["fecha"][:2], fecha_visual,
                   fontsize=coords_cdmx["fecha"][2], color=coords_cdmx["fecha"][3])

    for key in ["marca", "serie", "linea", "motor", "anio"]:
        x, y, s, col = coords_cdmx[key]
        pg.insert_text((x, y), str(datos[key]), fontsize=s, color=col)

    pg.insert_text(coords_cdmx["vigencia"][:2], vigencia_visual,
                   fontsize=coords_cdmx["vigencia"][2], color=coords_cdmx["vigencia"][3])
    pg.insert_text(coords_cdmx["nombre"][:2], datos["nombre"],
                   fontsize=coords_cdmx["nombre"][2], color=coords_cdmx["nombre"][3])

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
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(qr_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    qr_path = os.path.join(OUTPUT_DIR, f"{datos['folio']}_qr.png")
    img.save(qr_path)

    tam_qr = 1.6 * 28.35
    ancho_pagina = pg.rect.width
    x0 = (ancho_pagina / 2) - (tam_qr / 2) - 19
    x1 = (ancho_pagina / 2) + (tam_qr / 2) - 19
    y0 = 680.17
    y1 = y0 + tam_qr
    qr_rect = fitz.Rect(x0, y0, x1, y1)
    pg.insert_image(qr_rect, filename=qr_path, keep_proportion=False, overlay=True)

    doc.save(path_out)
    doc.close()
    return path_out

# ---------- HANDLERS ----------
@dp.message_handler(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("👋 Bot listo.\n\nUsa /permiso para iniciar el registro.")

@dp.message_handler(Command("permiso"))
async def permiso_init(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("Marca del vehículo:")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca, content_types=types.ContentTypes.TEXT)
async def form_marca(m: types.Message, state: FSMContext):
    await state.update_data(marca=m.text.strip())
    await m.answer("Línea del vehículo:")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea, content_types=types.ContentTypes.TEXT)
async def form_linea(m: types.Message, state: FSMContext):
    await state.update_data(linea=m.text.strip())
    await m.answer("Año:")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio, content_types=types.ContentTypes.TEXT)
async def form_anio(m: types.Message, state: FSMContext):
    await state.update_data(anio=m.text.strip())
    await m.answer("Serie (VIN):")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie, content_types=types.ContentTypes.TEXT)
async def form_serie(m: types.Message, state: FSMContext):
    await state.update_data(serie=m.text.strip())
    await m.answer("Motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor, content_types=types.ContentTypes.TEXT)
async def form_motor(m: types.Message, state: FSMContext):
    await state.update_data(motor=m.text.strip())
    await m.answer("Nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre, content_types=types.ContentTypes.TEXT)
async def form_nombre(m: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = m.text.strip()
    datos["folio"] = generar_folio_automatico("05")

    try:
        # Generar PDF y subirlo
        path_pdf = _make_pdf(datos)
        nombre_pdf = os.path.basename(path_pdf)
        url_pdf = subir_pdf_supabase(path_pdf, nombre_pdf)

        # Guardar registro
        guardar_supabase({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "nombre": datos["nombre"],
            "entidad": "CDMX",
            "url_pdf": url_pdf
        })

        caption = (
            f"✅ Registro creado\n"
            f"Folio: {datos['folio']}\n"
            f"{datos['marca']} {datos['linea']} ({datos['anio']})\n"
            f"PDF: {url_pdf}"
        )
        with open(path_pdf, "rb") as f:
            await m.answer_document(f, caption=caption)

    except Exception as e:
        logger.exception("Error generando/enviando PDF")
        await m.answer(f"❌ Ocurrió un error generando el PDF: {e}")

    await state.finish()

# ---------- LIFESPAN ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Iniciando bot (lifespan)...")
    try:
        if BASE_URL:
            await bot.set_webhook(f"{BASE_URL}/webhook")
            logger.info(f"Webhook configurado: {BASE_URL}/webhook")
        else:
            logger.warning("BASE_URL no configurada. Sin webhook.")
        yield
    finally:
        logger.info("Cerrando bot...")
        try:
            await bot.delete_webhook()
        except Exception:
            pass
        try:
            session = await bot.get_session()
            await session.close()
        except Exception:
            pass

# ---------- FASTAPI ----------
app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.get("/")
def health():
    return {
        "ok": True,
        "service": "Bot Permisos Digitales",
        "webhook_url": f"{BASE_URL}/webhook" if BASE_URL else "no configurado",
    }

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Render llama esta ruta con los updates de Telegram.
    Siempre devolvemos 200/JSON para evitar reintentos.
    """
    try:
        data = await request.json()
    except Exception:
        return {"ok": True, "note": "bad_json"}

    try:
        update = types.Update(**data)  # pydantic v1
    except Exception:
        return {"ok": True, "note": "parse_failed"}

    try:
        # Aiogram v2 necesita setear el contexto actual
        Bot.set_current(bot)
        Dispatcher.set_current(dp)
        await dp.process_update(update)
    except Exception as e:
        logger.exception(f"Error procesando update: {e}")
        return {"ok": True, "note": "handler_failed"}

    return {"ok": True}
