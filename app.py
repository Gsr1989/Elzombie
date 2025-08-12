# -*- coding: utf-8 -*-
# -------------------------------------------------------------
# Aiogram v2 (FSM) + FastAPI webhook (Render)
# Flujo /permiso: marca → linea → anio → serie → motor → nombre
# Genera PDF desde plantilla "cdmxdigital2025ppp.pdf" (junto a app.py)
# Guarda folio único en "folios_unicos" y registro en "borradores_registros"
# Sube el PDF a Storage (bucket "pdfs").
# Variables requeridas: BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY, BASE_URL
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
log = logging.getLogger("permiso-bot")
log.info("BOOT permiso-bot ⚙️")

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")
if not SUPABASE_URL:
    raise ValueError("SUPABASE_URL no está configurado")
if not SUPABASE_SERVICE_KEY:
    raise ValueError("SUPABASE_SERVICE_KEY no está configurado")

BUCKET = os.getenv("BUCKET", "pdfs").strip()
FOLIO_PREFIX = os.getenv("FOLIO_PREFIX", "05").strip()

# rutas/archivos
OUTPUT_DIR = "/tmp/pdfs"
os.makedirs(OUTPUT_DIR, exist_ok=True)
PLANTILLA_PDF = os.path.join(os.path.dirname(__file__), "cdmxdigital2025ppp.pdf")
if not os.path.exists(PLANTILLA_PDF):
    raise FileNotFoundError("No se encontró cdmxdigital2025ppp.pdf junto a app.py")

# tablas
TABLE_FOLIOS = "folios_unicos"
TABLE_REGISTROS = "borradores_registros"

# Cliente Supabase con service_role (bypass RLS)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- BOT ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

# ---------- COORDENADAS (PDF) ----------
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
    nfkd = unicodedata.normalize("NFKD", s or "")
    s2 = "".join(c for c in nfkd if not unicodedata.combining(c))
    s2 = s2.replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s2)

async def supabase_insert_retry(table: str, row: dict, attempts: int = 4, delay: float = 0.6):
    last = None
    for i in range(attempts):
        try:
            res = supabase.table(table).insert(row).execute()
            return res.data
        except Exception as e:
            last = e
            log.warning(f"[Supabase insert retry {i+1}/{attempts}] {e}")
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
            log.warning(f"[Supabase update retry {i+1}/{attempts}] {e}")
            await asyncio.sleep(delay * (i + 1))
    raise last

def nuevo_folio(prefix: str = FOLIO_PREFIX) -> str:
    """
    Inserta fila cumpliendo NOT NULL 'prefijo' y arma:
    folio = prefix + id (6 dígitos), p.ej. 05000001
    """
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).eq("id", nid).execute()
    except Exception as e:
        log.warning(f"No pude actualizar 'fol' en {TABLE_FOLIOS}: {e}")
    return folio

# ---------- PDF ----------
def _make_pdf(datos: dict) -> str:
    out_path = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_cdmx.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)
    meses = {1:"ENERO",2:"FEBRERO",3:"MARZO",4:"ABRIL",5:"MAYO",6:"JUNIO",
             7:"JULIO",8:"AGOSTO",9:"SEPTIEMBRE",10:"OCTUBRE",11:"NOVIEMBRE",12:"DICIEMBRE"}
    fecha_visual = f"{fecha_exp.day:02d} DE {meses[fecha_exp.month]} DEL {fecha_exp.year}"
    vigencia_visual = fecha_ven.strftime("%d/%m/%Y")

    pg.insert_text(coords_cdmx["folio"][:2], datos["folio"],
                   fontsize=coords_cdmx["folio"][2], color=coords_cdmx["folio"][3])
    pg.insert_text(coords_cdmx["fecha"][:2], fecha_visual,
                   fontsize=coords_cdmx["fecha"][2], color=coords_cdmx["fecha"][3])

    for key in ["marca", "serie", "linea", "motor", "anio"]:
        x, y, s, col = coords_cdmx[key]
        pg.insert_text((x, y), str(datos.get(key, "")), fontsize=s, color=col)

    pg.insert_text(coords_cdmx["vigencia"][:2], vigencia_visual,
                   fontsize=coords_cdmx["vigencia"][2], color=coords_cdmx["vigencia"][3])
    pg.insert_text(coords_cdmx["nombre"][:2], datos.get("nombre", ""),
                   fontsize=coords_cdmx["nombre"][2], color=coords_cdmx["nombre"][3])

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

    qr_png = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_qr.png")
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
    nombre_pdf = _slug(nombre_pdf).lstrip("/")        # SIN espacios y sin slash inicial
    with open(path_local, "rb") as f:
        data = f.read()
    # Algunas versiones requieren header 'x-upsert' como string "true"
    supabase.storage.from_(BUCKET).upload(
        nombre_pdf,
        data,
        file_options={"contentType": "application/pdf"},
        headers={"x-upsert": "true"}
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

# ---------- HANDLERS ----------
@dp.message_handler(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("👋 Bot listo.\nUsa /permiso para iniciar el registro.")

@dp.message_handler(Command("permiso"))
async def permiso_init(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("Marca del vehículo:")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca, content_types=types.ContentTypes.TEXT)
async def form_marca(m: types.Message, state: FSMContext):
    await state.update_data(marca=(m.text or "").strip())
    await m.answer("Línea (modelo/versión):")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea, content_types=types.ContentTypes.TEXT)
async def form_linea(m: types.Message, state: FSMContext):
    await state.update_data(linea=(m.text or "").strip())
    await m.answer("Año (4 dígitos):")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio, content_types=types.ContentTypes.TEXT)
async def form_anio(m: types.Message, state: FSMContext):
    await state.update_data(anio=(m.text or "").strip())
    await m.answer("Serie (VIN):")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie, content_types=types.ContentTypes.TEXT)
async def form_serie(m: types.Message, state: FSMContext):
    await state.update_data(serie=(m.text or "").strip())
    await m.answer("Motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor, content_types=types.ContentTypes.TEXT)
async def form_motor(m: types.Message, state: FSMContext):
    await state.update_data(motor=(m.text or "").strip())
    await m.answer("Nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre, content_types=types.ContentTypes.TEXT)
async def form_nombre(m: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = (m.text or "").strip()

    # 1) Folio único
    folio = nuevo_folio(FOLIO_PREFIX)
    datos["folio"] = folio

    # 2) Fechas
    fecha_exp = datetime.now().date()
    fecha_ven = fecha_exp + timedelta(days=30)

    await m.answer("⏳ Generando tu PDF…")

    try:
        # 3) PDF en /tmp
        path_pdf = await asyncio.to_thread(_make_pdf, datos)

        # 4) Subir a Storage (nombre sin espacios)
        nombre_pdf = f"{_slug(folio)}_cdmx_{int(time.time())}.pdf"
        url_pdf = await asyncio.to_thread(_upload_pdf, path_pdf, nombre_pdf)

        # 5) Enviar PDF al chat
        with open(path_pdf, "rb") as f:
            await m.answer_document(
                f,
                caption=(
                    f"✅ Registro generado\n"
                    f"Folio: {folio}\n"
                    f"{datos.get('marca','')} {datos.get('linea','')} ({datos.get('anio','')})\n"
                    f"{url_pdf}"
                )
            )

        # 6) Guardar registro
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

        # 7) Actualizar fila del folio (opcional)
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
            log.warning(f"No se pudo actualizar {TABLE_FOLIOS}: {e}")

    except Exception as e:
        log.exception("Fallo generando/enviando PDF")
        await m.answer(f"❌ Error generando PDF: {e}")

    await state.finish()

# fallback
@dp.message_handler()
async def fallback(m: types.Message):
    await m.answer("No entendí. Usa /permiso para iniciar.")

# ---------- FASTAPI ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Iniciando webhook…")
    try:
        if BASE_URL:
            await bot.set_webhook(f"{BASE_URL}/webhook")
            log.info(f"Webhook OK: {BASE_URL}/webhook")
        else:
            log.warning("BASE_URL no configurada; sin webhook.")
    except Exception as e:
        log.warning(f"No se pudo setear webhook: {e}")
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

    # Aiogram v2 requiere setear contexto
    Bot.set_current(bot)
    Dispatcher.set_current(dp)

    update = types.Update(**data)
    await dp.process_update(update)
    return {"ok": True}
