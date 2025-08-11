# app.py
# -------------------------------------------------------------
# Bot de Telegram (Aiogram v2) + FastAPI (webhook en Render)
# Genera PDF (PyMuPDF/fitz) con QR, sube a Supabase Storage
# Guarda registro en Supabase (usa service_role, bypass RLS)
# Usa tabla public.folios_unicos para folio y almacenamiento
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
BASE_URL = os.getenv("BASE_URL", "")  # ej: https://tuapp.onrender.com
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")  # service_role
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise ValueError("SUPABASE_URL y/o SUPABASE_SERVICE_KEY no están configurados")

# Storage
BUCKET = os.getenv("BUCKET", "pdfs")
OUTPUT_DIR = "/tmp/pdfs"  # /tmp para evitar reloads del server
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Supabase client (service_role -> bypass RLS)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- CONFIG BD ----------
FOLIOS_TABLE = "folios_unicos"

# columnas que con seguridad existen en public.folios_unicos
ALLOWED_COLUMNS = {
    "prefijo",
    "folio",
    "marca",
    "linea",
    "anio",
    "numero_serie",
    "numero_motor",
    "nombre",
    "entidad",
    "url_pdf",
    "fecha_expedicion",
    "fecha_vencimiento",
}

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
def _slug_filename(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_only)
    return safe

def nuevo_folio(prefijo: str = "05") -> str:
    """
    Genera folio único insertando en public.folios_unicos (id bigserial).
    IMPORTANTE: insertar 'prefijo' porque la columna es NOT NULL.
    """
    ins = supabase.table(FOLIOS_TABLE).insert({"prefijo": prefijo}).execute()
    nid = int(ins.data[0]["id"])
    return f"{prefijo}{nid:06d}"  # 05000001, 05000002, ...

def subir_pdf_supabase(path_local: str, nombre_pdf: str) -> str:
    """Sube el PDF al bucket y devuelve la URL pública."""
    nombre_pdf = _slug_filename(nombre_pdf)
    with open(path_local, "rb") as f:
        data = f.read()
    supabase.storage.from_(BUCKET).upload(
        nombre_pdf,
        data,
        {"contentType": "application/pdf"}
    )
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{nombre_pdf}"

def guardar_supabase(data: dict) -> None:
    """
    Inserta SOLO las columnas que existen en folios_unicos
    (si tu tabla no tiene 'color', etc., esto evita el 400).
    """
    filtered = {k: v for k, v in data.items() if k in ALLOWED_COLUMNS}
    supabase.table(FOLIOS_TABLE).insert(filtered).execute()

# ---------- FSM ----------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# ---------- PDF ----------
def _make_pdf(datos: dict) -> str:
    plantilla = os.path.join(os.path.dirname(__file__), "cdmxdigital2025ppp.pdf")
    if not os.path.exists(plantilla):
        raise FileNotFoundError(f"No se encontró la plantilla: {plantilla}")

    out_path = os.path.join(OUTPUT_DIR, f"{datos['folio']}_cdmx.pdf")

    doc = fitz.open(plantilla)
    pg = doc[0]

    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)
    meses = {
        1:"ENERO",2:"FEBRERO",3:"MARZO",4:"ABRIL",5:"MAYO",6:"JUNIO",
        7:"JULIO",8:"AGOSTO",9:"SEPTIEMBRE",10:"OCTUBRE",11:"NOVIEMBRE",12:"DICIEMBRE"
    }
    fecha_visual = f"{fecha_exp.day:02d} DE {meses[fecha_exp.month]} DEL {fecha_exp.year}"
    vigencia_visual = fecha_ven.strftime("%d/%m/%Y")

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

    qr_png = os.path.join(OUTPUT_DIR, f"{datos['folio']}_qr.png")
    img.save(qr_png)

    # Colocar QR
    tam_qr = 1.6 * 28.35  # ~1.6 cm en puntos
    ancho_pagina = pg.rect.width
    x0 = (ancho_pagina / 2) - (tam_qr / 2) - 19
    x1 = (ancho_pagina / 2) + (tam_qr / 2) - 19
    y0 = 680.17
    y1 = y0 + tam_qr
    pg.insert_image(fitz.Rect(x0, y0, x1, y1), filename=qr_png,
                    keep_proportion=False, overlay=True)

    doc.save(out_path)
    doc.close()
    return out_path

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
    datos["folio"]  = nuevo_folio("05")  # folio único

    # Fechas (ISO) para la BD
    fecha_exp = datetime.now().date()
    fecha_ven = (datetime.now() + timedelta(days=30)).date()

    await m.answer("⏳ Generando tu PDF, dame unos segundos...")

    try:
        # 1) Generar PDF SIN bloquear el event loop
        path_pdf = await asyncio.to_thread(_make_pdf, datos)

        # 2) Subir a Storage
        nombre_pdf = _slug_filename(f"{datos['folio']}_cdmx_{int(time.time())}.pdf")
        url_pdf    = await asyncio.to_thread(subir_pdf_supabase, path_pdf, nombre_pdf)

        # 3) Enviar PDF al usuario
        caption = (
            f"✅ Registro creado\n"
            f"Folio: {datos['folio']}\n"
            f"{datos.get('marca','?')} {datos.get('linea','?')} ({datos.get('anio','?')})\n"
            f"PDF: {url_pdf}"
        )
        with open(path_pdf, "rb") as f:
            await m.answer_document(f, caption=caption)

        # 4) Guardar en Supabase (filtrado a columnas existentes)
        payload = {
            "prefijo": "05",
            "folio": datos["folio"],
            "marca": datos.get("marca", None),
            "linea": datos.get("linea", None),
            "anio": str(datos.get("anio", "")),
            "numero_serie": datos.get("serie", None),
            "numero_motor": datos.get("motor", None),
            "nombre": datos.get("nombre", None),
            "entidad": "CDMX",
            "url_pdf": url_pdf,
            "fecha_expedicion": fecha_exp.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
        }
        try:
            await asyncio.to_thread(guardar_supabase, payload)
        except Exception as e:
            logger.warning(f"No se pudo guardar en Supabase: {e}")
            await m.answer("⚠️ No se pudo guardar en la base, pero tu PDF ya fue generado.")

    except Exception as e:
        logger.exception("Error generando/enviando PDF")
        await m.answer(f"❌ Error generando PDF: {e}")
    finally:
        await state.finish()

# Fallback
@dp.message_handler()
async def fallback(m: types.Message):
    await m.answer("No entendí. Usa /permiso para iniciar.")

# ---------- LIFESPAN ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Iniciando bot...")
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook")
    else:
        logger.warning("BASE_URL no configurada; sin webhook.")
    yield
    try:
        await bot.delete_webhook()
    except Exception:
        pass

# ---------- FASTAPI ----------
app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.get("/")
def health():
    return {"ok": True, "webhook": f"{BASE_URL}/webhook" if BASE_URL else None}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    logger.info(f"permiso-bot:UPDATE: {data}")
    update = types.Update(**data)

    # Requerido por aiogram v2 en webhook manual
    Bot.set_current(bot)
    Dispatcher.set_current(dp)

    await dp.process_update(update)
    return {"ok": True}
