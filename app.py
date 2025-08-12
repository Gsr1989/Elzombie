# -*- coding: utf-8 -*-
# -------------------------------------------------------------
# Aiogram v2 (FSM) + FastAPI webhook (Render)
# Flujo /permiso: marca → linea → anio → serie → motor → nombre
# Genera PDF desde "cdmxdigital2025ppp.pdf" (junto a app_final.py)
# Folio único en "folios_unicos" y registro en "borradores_registros"
# Sube PDF a Supabase Storage (bucket "pdfs")
# Env: BOT_TOKEN, BASE_URL, SUPABASE_URL, SUPABASE_SERVICE_KEY, [FLOW_TTL]
# Start (Render): uvicorn app_final:app --host 0.0.0.0 --port $PORT
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
import aiohttp
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("permiso-bot")
log.info("🚀 BOOT permiso-bot WEBHOOK VERSION")

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

# == Anti–SPAM / Lock por chat ==
FLOW_TTL = int(os.getenv("FLOW_TTL", "300"))  # 5 min default
ACTIVE = {}  # chat_id -> deadline (epoch seg)

def _now(): return time.time()

def lock_busy(chat_id: int) -> bool:
    dl = ACTIVE.get(chat_id)
    return bool(dl and dl > _now())

def lock_acquire(chat_id: int) -> bool:
    if lock_busy(chat_id):
        return False
    ACTIVE[chat_id] = _now() + FLOW_TTL
    return True

def lock_bump(chat_id: int):
    if chat_id in ACTIVE:
        ACTIVE[chat_id] = _now() + FLOW_TTL

def lock_release(chat_id: int):
    ACTIVE.pop(chat_id, None)

async def _sweeper():
    """Limpia locks expirados cada 30 segundos"""
    while True:
        try:
            now = _now()
            dead = [cid for cid, dl in ACTIVE.items() if dl <= now]
            for cid in dead:
                ACTIVE.pop(cid, None)
            if dead:
                log.info(f"Limpiados {len(dead)} locks expirados")
        except Exception as e:
            log.warning(f"sweeper error: {e}")
        await asyncio.sleep(30)

# Rutas/archivos
OUTPUT_DIR = "/tmp/pdfs"
os.makedirs(OUTPUT_DIR, exist_ok=True)
PLANTILLA_PDF = os.path.join(os.path.dirname(__file__), "cdmxdigital2025ppp.pdf")
if not os.path.exists(PLANTILLA_PDF):
    raise FileNotFoundError("No se encontró cdmxdigital2025ppp.pdf junto a app_final.py")

# Tablas
TABLE_FOLIOS = "folios_unicos"
TABLE_REGISTROS = "borradores_registros"

# Cliente Supabase service_role (bypass RLS)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- BOT ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
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
            return await asyncio.to_thread(lambda: supabase.table(table).insert(row).execute().data)
        except Exception as e:
            last = e
            log.warning(f"[Supabase insert retry {i+1}/{attempts}] {e}")
            await asyncio.sleep(delay * (i + 1))
    raise last

async def supabase_update_retry(table: str, match: dict, updates: dict, attempts: int = 4, delay: float = 0.6):
    last = None
    for i in range(attempts):
        try:
            return await asyncio.to_thread(
                lambda: supabase.table(table).update(updates).match(match).execute().data
            )
        except Exception as e:
            last = e
            log.warning(f"[Supabase update retry {i+1}/{attempts}] {e}")
            await asyncio.sleep(delay * (i + 1))
    raise last

def nuevo_folio(prefix: str = FOLIO_PREFIX) -> str:
    """Inserta fila y arma folio = prefix + id (6 dígitos), p.ej. 05000001."""
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).match({"id": nid}).execute()
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
    nombre_pdf = _slug(nombre_pdf).lstrip("/")
    with open(path_local, "rb") as f:
        data = f.read()
    try:
        supabase.storage.from_(BUCKET).upload(nombre_pdf, data)
    except Exception as e:
        log.warning(f"Upload method 1 failed: {e}, trying alternative...")
        with open(path_local, "rb") as f2:
            supabase.storage.from_(BUCKET).upload(nombre_pdf, f2)
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
@dp.message_handler(Command("start"), state="*")
async def cmd_start(m: types.Message, state: FSMContext):
    log.info(f"/start <- chat:{m.chat.id}")
    # Limpiar cualquier estado activo
    await state.finish()
    lock_release(m.chat.id)
    await m.answer(
        "🎯 <b>Bot de Permisos Digitales CDMX</b>\n\n"
        "📋 Usa /permiso para iniciar el registro\n"
        "❌ Usa /cancel para abortar un flujo\n\n"
        "🚗 Genera permisos con QR y PDF automáticamente"
    )

@dp.message_handler(commands=["cancel", "stop"], state="*")
async def cmd_cancel(m: types.Message, state: FSMContext):
    log.info(f"/cancel <- chat:{m.chat.id}")
    await state.finish()
    lock_release(m.chat.id)
    await m.answer("❎ Flujo cancelado. Usa /permiso para iniciar de nuevo.")

@dp.message_handler(Command("permiso"), state="*")
async def permiso_init(m: types.Message, state: FSMContext):
    log.info(f"/permiso <- chat:{m.chat.id}")
    
    # Verificar si ya está en un flujo activo
    current_state = await state.get_state()
    if current_state is not None:
        log.warning(f"chat:{m.chat.id} intentó /permiso pero ya está en estado: {current_state}")
        await m.answer("⚠️ Ya tienes un registro en curso. Termina el actual o manda /cancel para empezar de nuevo.")
        return
    
    # Verificar lock adicional
    if lock_busy(m.chat.id):
        log.warning(f"chat:{m.chat.id} intentó /permiso pero tiene lock activo")
        await m.answer("⚠️ Ya tienes un registro en curso. Espera unos minutos o manda /cancel.")
        return
    
    # Adquirir lock
    if not lock_acquire(m.chat.id):
        log.warning(f"chat:{m.chat.id} no pudo adquirir lock")
        await m.answer("⚠️ No se pudo iniciar el registro. Intenta en unos minutos.")
        return
    
    log.info(f"chat:{m.chat.id} iniciando flujo /permiso")
    await m.answer("📋 <b>Iniciando registro de permiso</b>\n\n🚗 Marca del vehículo:")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca, content_types=types.ContentTypes.TEXT)
async def form_marca(m: types.Message, state: FSMContext):
    texto = (m.text or "").strip()
    if not texto:
        await m.answer("❌ La marca no puede estar vacía. Intenta de nuevo:")
        return
    
    log.info(f"marca <- chat:{m.chat.id} texto:{texto}")
    lock_bump(m.chat.id)
    await state.update_data(marca=texto)
    await m.answer("📱 Línea (modelo/versión):")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea, content_types=types.ContentTypes.TEXT)
async def form_linea(m: types.Message, state: FSMContext):
    texto = (m.text or "").strip()
    if not texto:
        await m.answer("❌ La línea no puede estar vacía. Intenta de nuevo:")
        return
    
    log.info(f"linea <- chat:{m.chat.id} texto:{texto}")
    lock_bump(m.chat.id)
    await state.update_data(linea=texto)
    await m.answer("📅 Año (4 dígitos):")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio, content_types=types.ContentTypes.TEXT)
async def form_anio(m: types.Message, state: FSMContext):
    texto = (m.text or "").strip()
    if not texto or not texto.isdigit() or len(texto) != 4:
        await m.answer("❌ Ingresa un año válido de 4 dígitos (ej: 2020):")
        return
    
    log.info(f"anio <- chat:{m.chat.id} texto:{texto}")
    lock_bump(m.chat.id)
    await state.update_data(anio=texto)
    await m.answer("🔢 Serie (VIN):")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie, content_types=types.ContentTypes.TEXT)
async def form_serie(m: types.Message, state: FSMContext):
    texto = (m.text or "").strip()
    if not texto:
        await m.answer("❌ La serie no puede estar vacía. Intenta de nuevo:")
        return
    
    log.info(f"serie <- chat:{m.chat.id} texto:{texto}")
    lock_bump(m.chat.id)
    await state.update_data(serie=texto)
    await m.answer("🔧 Motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor, content_types=types.ContentTypes.TEXT)
async def form_motor(m: types.Message, state: FSMContext):
    texto = (m.text or "").strip()
    if not texto:
        await m.answer("❌ El motor no puede estar vacío. Intenta de nuevo:")
        return
    
    log.info(f"motor <- chat:{m.chat.id} texto:{texto}")
    lock_bump(m.chat.id)
    await state.update_data(motor=texto)
    await m.answer("👤 Nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre, content_types=types.ContentTypes.TEXT)
async def form_nombre(m: types.Message, state: FSMContext):
    texto = (m.text or "").strip()
    if not texto:
        await m.answer("❌ El nombre no puede estar vacío. Intenta de nuevo:")
        return
    
    log.info(f"nombre <- chat:{m.chat.id} texto:{texto}")
    lock_bump(m.chat.id)
    datos = await state.get_data()
    datos["nombre"] = texto

    try:
        # 1) Folio único (en thread)
        await m.answer("⏳ Generando folio único...")
        folio = await asyncio.to_thread(nuevo_folio, FOLIO_PREFIX)
        datos["folio"] = folio
        log.info(f"Folio generado: {folio} para chat:{m.chat.id}")

        # 2) Fechas
        fecha_exp = datetime.now().date()
        fecha_ven = fecha_exp + timedelta(days=30)

        await m.answer("📄 Generando tu PDF...")

        # 3) PDF en /tmp
        path_pdf = await asyncio.to_thread(_make_pdf, datos)
        log.info(f"PDF generado: {path_pdf}")

        # 4) Subir a Storage
        await m.answer("☁️ Subiendo a la nube...")
        nombre_pdf = f"{_slug(folio)}_cdmx_{int(time.time())}.pdf"
        url_pdf = await asyncio.to_thread(_upload_pdf, path_pdf, nombre_pdf)
        log.info(f"PDF subido: {url_pdf}")

        # 5) Enviar PDF al chat (fallback a texto)
        caption = (
            f"✅ <b>Registro generado exitosamente</b>\n\n"
            f"📋 Folio: <code>{folio}</code>\n"
            f"🚗 Vehículo: {datos.get('marca','')} {datos.get('linea','')} ({datos.get('anio','')})\n"
            f"👤 Solicitante: {datos.get('nombre','')}\n\n"
            f"🔗 URL: {url_pdf}"
        )
        
        try:
            await m.answer("📤 Enviando tu permiso...")
            with open(path_pdf, "rb") as f:
                await m.answer_document(f, caption=caption)
            log.info(f"sendDocument OK chat:{m.chat.id} folio:{folio}")
        except Exception as e_doc:
            log.warning(f"sendDocument falló: {e_doc}. Enviando como texto.")
            await m.answer(caption)

        # 6) Guardar registro
        await m.answer("💾 Guardando en base de datos...")
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
        log.info(f"Registro guardado en BD: {folio}")

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

        await m.answer("🎉 <b>¡Permiso generado exitosamente!</b>\n\nSi necesitas otro permiso, puedes mandar /permiso nuevamente.")
        log.info(f"Proceso completado exitosamente para chat:{m.chat.id} folio:{folio}")

    except Exception as e:
        log.exception(f"Error generando permiso para chat:{m.chat.id}")
        await m.answer(f"❌ Error generando el permiso: {str(e)}\n\nIntenta nuevamente con /permiso")

    finally:
        # Siempre limpiar el estado y lock
        await state.finish()
        lock_release(m.chat.id)

# Fallback para mensajes no reconocidos
@dp.message_handler(content_types=types.ContentTypes.ANY)
async def fallback(m: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await m.answer("❌ Por favor responde con texto válido, o usa /cancel para abortar.")
    else:
        await m.answer("👋 ¡Hola! No entendí tu mensaje.\n\nUsa /permiso para iniciar un registro o /start para ver la ayuda.")

# Keep-alive para Render
async def keep_alive():
    if not BASE_URL:
        return
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BASE_URL}/", timeout=10):
                    pass
        except Exception as e:
            log.warning(f"keep_alive: {e}")
        await asyncio.sleep(600)

# ---------- FASTAPI ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Iniciando webhook…")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            await bot.set_webhook(
                f"{BASE_URL}/webhook",
                drop_pending_updates=True,
                allowed_updates=["message"]
            )
            info = await bot.get_webhook_info()
            log.info(f"Webhook OK: {info.url} | pending={info.pending_update_count}")
        else:
            log.warning("BASE_URL no configurada; sin webhook.")
        asyncio.create_task(keep_alive())
        asyncio.create_task(_sweeper())
    except Exception as e:
        log.warning(f"No se pudo setear webhook: {e}")
    yield
    try:
        await bot.delete_webhook()
    except Exception:
        pass

app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.get("/")
async def health():
    try:
        info = await bot.get_webhook_info()
        return {"ok": True, "webhook": info.url, "pending": info.pending_update_count}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/debug")
async def debug():
    try:
        me = await bot.get_me()
        info = await bot.get_webhook_info()
        return {
            "bot": {"id": me.id, "username": me.username},
            "webhook": info.url,
            "pending": info.pending_update_count,
            "active_locks": len(ACTIVE),
            "active_states": list(ACTIVE.keys())
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": True, "note": "bad_json"}

    Bot.set_current(bot)
    Dispatcher.set_current(dp)

    # Log mínimo
    try:
        msg = data.get("message") or data.get("edited_message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        frm = (msg.get("from") or {}).get("id")
        txt = msg.get("text", "")[:50]  # Truncar para logs
        log.info(f"POST /webhook <- chat:{chat_id} from:{frm} text:{txt}")
    except Exception:
        pass

    async def _proc():
        try:
            update = types.Update(**data)
            await dp.process_update(update)
        except Exception as e:
            log.exception(f"process_update error: {e}")

    asyncio.create_task(_proc())
    return {"ok": True}
