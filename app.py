# Tu código original + FIX del bucle infinito. ESO ES TODO.

import os
import re
import time
import unicodedata
import logging
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import fitz
import qrcode
import aiohttp
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("permiso-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
BUCKET = os.getenv("BUCKET", "pdfs").strip()
FOLIO_PREFIX = os.getenv("FOLIO_PREFIX", "05").strip()
FLOW_TTL = int(os.getenv("FLOW_TTL", "300"))

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise ValueError("Variables faltantes")

ACTIVE = {}
OUTPUT_DIR = "/tmp/pdfs"
os.makedirs(OUTPUT_DIR, exist_ok=True)
PLANTILLA_PDF = os.path.join(os.path.dirname(__file__), "cdmxdigital2025ppp.pdf")
TABLE_FOLIOS = "folios_unicos"
TABLE_REGISTROS = "borradores_registros"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=storage)

def _now(): return time.time()
def lock_busy(chat_id: int): return bool(ACTIVE.get(chat_id, 0) > _now())
def lock_acquire(chat_id: int): 
    if lock_busy(chat_id): return False
    ACTIVE[chat_id] = _now() + FLOW_TTL
    return True
def lock_bump(chat_id: int): 
    if chat_id in ACTIVE: ACTIVE[chat_id] = _now() + FLOW_TTL
def lock_release(chat_id: int): ACTIVE.pop(chat_id, None)

async def _sweeper():
    while True:
        try:
            now = _now()
            dead = [cid for cid, dl in ACTIVE.items() if dl <= now]
            for cid in dead: ACTIVE.pop(cid, None)
        except: pass
        await asyncio.sleep(30)

coords_cdmx = {
    "folio": (87, 130, 14, (1, 0, 0)), "fecha": (130, 145, 12, (0, 0, 0)),
    "marca": (87, 290, 11, (0, 0, 0)), "serie": (375, 290, 11, (0, 0, 0)),
    "linea": (87, 307, 11, (0, 0, 0)), "motor": (375, 307, 11, (0, 0, 0)),
    "anio": (87, 323, 11, (0, 0, 0)), "vigencia": (375, 323, 11, (0, 0, 0)),
    "nombre": (375, 340, 11, (0, 0, 0)),
}

def _slug(s): 
    nfkd = unicodedata.normalize("NFKD", s or "")
    s2 = "".join(c for c in nfkd if not unicodedata.combining(c)).replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s2)

async def supabase_insert_retry(table, row, attempts=4, delay=0.6):
    for i in range(attempts):
        try:
            return await asyncio.to_thread(lambda: supabase.table(table).insert(row).execute().data)
        except Exception as e:
            if i == attempts-1: raise e
            await asyncio.sleep(delay * (i + 1))

def nuevo_folio(prefix=FOLIO_PREFIX):
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).match({"id": nid}).execute()
    except: pass
    return folio

def _make_pdf(datos):
    out_path = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_cdmx.pdf")
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
    
    qr_text = f"Folio: {datos['folio']}\nMarca: {datos.get('marca','')}\nLínea: {datos.get('linea','')}\nAño: {datos.get('anio','')}\nSerie: {datos.get('serie','')}\nMotor: {datos.get('motor','')}\nNombre: {datos.get('nombre','')}\nSEMOVICDMX DIGITAL"
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
    y0, y1 = 680.17, 680.17 + tam_qr
    pg.insert_image(fitz.Rect(x0, y0, x1, y1), filename=qr_png, keep_proportion=False, overlay=True)
    
    doc.save(out_path)
    doc.close()
    return out_path

def _upload_pdf(path_local, nombre_pdf):
    nombre_pdf = _slug(nombre_pdf).lstrip("/")
    with open(path_local, "rb") as f:
        data = f.read()
    try:
        supabase.storage.from_(BUCKET).upload(nombre_pdf, data)
    except:
        with open(path_local, "rb") as f2:
            supabase.storage.from_(BUCKET).upload(nombre_pdf, f2)
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{nombre_pdf}"

class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

@dp.message_handler(Command("start"), state="*")
async def cmd_start(m, state):
    await state.finish()
    lock_release(m.chat.id)
    await m.answer("👋 Bot listo.\nUsa /permiso para iniciar el registro.")

@dp.message_handler(commands=["cancel", "stop"], state="*")
async def cmd_cancel(m, state):
    await state.finish()
    lock_release(m.chat.id)
    await m.answer("❎ Flujo cancelado.")

@dp.message_handler(Command("permiso"), state="*")
async def permiso_init(m, state):
    current_state = await state.get_state()
    if current_state is not None:
        await m.answer("⚠️ Ya tienes un registro en curso. Manda /cancel para empezar de nuevo.")
        return
    if not lock_acquire(m.chat.id):
        await m.answer("⚠️ Espera unos minutos.")
        return
    await m.answer("📋 Iniciando registro.\n\n🚗 Marca del vehículo:")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca, content_types=types.ContentTypes.TEXT)
async def form_marca(m, state):
    if not (texto := (m.text or "").strip()):
        await m.answer("❌ La marca no puede estar vacía:")
        return
    lock_bump(m.chat.id)
    await state.update_data(marca=texto)
    await m.answer("📱 Línea (modelo/versión):")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea, content_types=types.ContentTypes.TEXT)
async def form_linea(m, state):
    if not (texto := (m.text or "").strip()):
        await m.answer("❌ La línea no puede estar vacía:")
        return
    lock_bump(m.chat.id)
    await state.update_data(linea=texto)
    await m.answer("📅 Año (4 dígitos):")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio, content_types=types.ContentTypes.TEXT)
async def form_anio(m, state):
    if not (texto := (m.text or "").strip()) or not texto.isdigit() or len(texto) != 4:
        await m.answer("❌ Año válido de 4 dígitos:")
        return
    lock_bump(m.chat.id)
    await state.update_data(anio=texto)
    await m.answer("🔢 Serie (VIN):")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie, content_types=types.ContentTypes.TEXT)
async def form_serie(m, state):
    if not (texto := (m.text or "").strip()):
        await m.answer("❌ La serie no puede estar vacía:")
        return
    lock_bump(m.chat.id)
    await state.update_data(serie=texto)
    await m.answer("🔧 Motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor, content_types=types.ContentTypes.TEXT)
async def form_motor(m, state):
    if not (texto := (m.text or "").strip()):
        await m.answer("❌ El motor no puede estar vacío:")
        return
    lock_bump(m.chat.id)
    await state.update_data(motor=texto)
    await m.answer("👤 Nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre, content_types=types.ContentTypes.TEXT)
async def form_nombre(m, state):
    if not (texto := (m.text or "").strip()):
        await m.answer("❌ El nombre no puede estar vacío:")
        return
    
    datos = await state.get_data()
    datos["nombre"] = texto
    
    try:
        await m.answer("⏳ Generando folio...")
        folio = await asyncio.to_thread(nuevo_folio)
        datos["folio"] = folio
        
        await m.answer("📄 Generando PDF...")
        path_pdf = await asyncio.to_thread(_make_pdf, datos)
        
        await m.answer("☁️ Subiendo...")
        nombre_pdf = f"{_slug(folio)}_cdmx_{int(time.time())}.pdf"
        url_pdf = await asyncio.to_thread(_upload_pdf, path_pdf, nombre_pdf)
        
        caption = f"✅ Registro generado\n📋 Folio: {folio}\n🚗 {datos.get('marca','')} {datos.get('linea','')} ({datos.get('anio','')})\n👤 {datos.get('nombre','')}\n🔗 {url_pdf}"
        
        try:
            with open(path_pdf, "rb") as f:
                await m.answer_document(f, caption=caption)
        except:
            await m.answer(caption)
        
        await m.answer("💾 Guardando...")
        fecha_exp = datetime.now().date()
        await supabase_insert_retry(TABLE_REGISTROS, {
            "folio": folio, "marca": datos.get("marca", ""), "linea": datos.get("linea", ""),
            "anio": str(datos.get("anio", "")), "numero_serie": datos.get("serie", ""),
            "numero_motor": datos.get("motor", ""), "nombre": datos.get("nombre", ""),
            "entidad": "CDMX", "url_pdf": url_pdf, "fecha_expedicion": fecha_exp.isoformat(),
            "fecha_vencimiento": (fecha_exp + timedelta(days=30)).isoformat(),
        })
        
        await m.answer("🎉 ¡Listo! Usa /permiso para otro.")
        
    except Exception as e:
        await m.answer(f"❌ Error: {e}")
    finally:
        await state.finish()
        lock_release(m.chat.id)

@dp.message_handler()
async def fallback(m, state):
    current_state = await state.get_state()
    if current_state:
        await m.answer("❌ Texto válido o /cancel")
    else:
        await m.answer("👋 Usa /permiso para iniciar")

async def keep_alive():
    if not BASE_URL: return
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BASE_URL}/", timeout=10): pass
        except: pass
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app):
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            await bot.set_webhook(f"{BASE_URL}/webhook", drop_pending_updates=True, allowed_updates=["message"])
        asyncio.create_task(keep_alive())
        asyncio.create_task(_sweeper())
    except: pass
    yield
    try:
        await bot.delete_webhook()
    except: pass

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    try:
        info = await bot.get_webhook_info()
        return {"ok": True, "webhook": info.url, "pending": info.pending_update_count}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        Bot.set_current(bot)
        Dispatcher.set_current(dp)
        asyncio.create_task(dp.process_update(types.Update(**data)))
        return {"ok": True}
    except:
        return {"ok": True}
