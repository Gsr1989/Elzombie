# app.py
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from supabase import create_client
from datetime import datetime, timedelta
from contextlib import asynccontextmanager, suppress
import asyncio, aiohttp, os, qrcode, fitz, unicodedata, re, time, logging

# Variables de entorno
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
FOLIO_PREFIX = os.getenv("FOLIO_PREFIX", "05")
BUCKET = os.getenv("BUCKET", "pdfs")
FLOW_TTL = int(os.getenv("FLOW_TTL", "300"))
OUTPUT_DIR = "/tmp/pdfs"
PLANTILLA_PDF = os.path.join(os.path.dirname(__file__), "cdmxdigital2025ppp.pdf")
TABLE_FOLIOS = "folios_unicos"
TABLE_REGISTROS = "borradores_registros"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Inicialización
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
ACTIVE = {}

# Funciones utilitarias
def _now(): return time.time()
def lock_acquire(cid): return not ACTIVE.get(cid, 0) > _now() and not ACTIVE.update({cid: _now()+FLOW_TTL})
def lock_bump(cid): ACTIVE[cid] = _now() + FLOW_TTL
def lock_release(cid): ACTIVE.pop(cid, None)
async def _sweeper():
    while True:
        await asyncio.sleep(30)
        now = _now()
        [ACTIVE.pop(cid) for cid, t in list(ACTIVE.items()) if t <= now]

def _slug(s):
    nfkd = unicodedata.normalize("NFKD", s or "")
    s2 = "".join(c for c in nfkd if not unicodedata.combining(c)).replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s2)

async def supabase_insert_retry(table, row, attempts=4, delay=0.6):
    for i in range(attempts):
        try:
            return await asyncio.to_thread(lambda: supabase.table(table).insert(row).execute().data)
        except Exception as e:
            if i == attempts - 1: raise e
            await asyncio.sleep(delay * (i + 1))

def nuevo_folio(prefix=FOLIO_PREFIX):
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).match({"id": nid}).execute()
    except: pass
    return folio

coords = {
    "folio": (87, 130, 14, (1, 0, 0)), "fecha": (130, 145, 12, (0, 0, 0)),
    "marca": (87, 290, 11, (0, 0, 0)), "serie": (375, 290, 11, (0, 0, 0)),
    "linea": (87, 307, 11, (0, 0, 0)), "motor": (375, 307, 11, (0, 0, 0)),
    "anio": (87, 323, 11, (0, 0, 0)), "vigencia": (375, 323, 11, (0, 0, 0)),
    "nombre": (375, 340, 11, (0, 0, 0)),
}

def _make_pdf(datos):
    out_path = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_cdmx.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)
    fecha_visual = fecha_exp.strftime("%d DE %B DE %Y").upper()
    vigencia_visual = fecha_ven.strftime("%d/%m/%Y")

    pg.insert_text(coords["folio"][:2], datos["folio"], fontsize=coords["folio"][2], color=coords["folio"][3])
    pg.insert_text(coords["fecha"][:2], fecha_visual, fontsize=coords["fecha"][2], color=coords["fecha"][3])
    for k in ["marca", "serie", "linea", "motor", "anio"]:
        pg.insert_text(coords[k][:2], str(datos.get(k, "")), fontsize=coords[k][2], color=coords[k][3])
    pg.insert_text(coords["vigencia"][:2], vigencia_visual, fontsize=coords["vigencia"][2], color=coords["vigencia"][3])
    pg.insert_text(coords["nombre"][:2], datos.get("nombre", ""), fontsize=coords["nombre"][2], color=coords["nombre"][3])

    qr_text = (
        f"Folio: {datos['folio']}\nMarca: {datos.get('marca','')}\nLínea: {datos.get('linea','')}\n"
        f"Año: {datos.get('anio','')}\nSerie: {datos.get('serie','')}\nMotor: {datos.get('motor','')}\n"
        f"Nombre: {datos.get('nombre','')}\nSEMOVICDMX DIGITAL"
    )
    qr = qrcode.make(qr_text)
    qr_path = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_qr.png")
    qr.save(qr_path)

    tam = 1.6 * 28.35
    x0, y0 = (pg.rect.width / 2 - tam / 2) - 19, 680.17
    pg.insert_image(fitz.Rect(x0, y0, x0 + tam, y0 + tam), filename=qr_path)

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

# FSM States
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# Telegram Handlers
@dp.message_handler(Command("start"), state="*")
async def cmd_start(m, state): await state.finish(); lock_release(m.chat.id); await m.answer("👋 Bot listo. Usa /permiso")

@dp.message_handler(commands=["cancel", "stop"], state="*")
async def cmd_cancel(m, state): await state.finish(); lock_release(m.chat.id); await m.answer("❎ Cancelado")

@dp.message_handler(Command("permiso"), state="*")
async def permiso_init(m, state):
    if await state.get_state(): return await m.answer("⚠️ Ya tienes uno activo")
    if not lock_acquire(m.chat.id): return await m.answer("⏳ Espera unos minutos")
    await m.answer("🚗 Marca del vehículo:"); await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca)
async def step_marca(m, state): lock_bump(m.chat.id); await state.update_data(marca=m.text.strip()); await m.answer("📱 Línea:"); await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea)
async def step_linea(m, state): lock_bump(m.chat.id); await state.update_data(linea=m.text.strip()); await m.answer("📅 Año (4 dígitos):"); await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio)
async def step_anio(m, state):
    if not m.text.strip().isdigit() or len(m.text.strip()) != 4: return await m.answer("❌ Año válido de 4 dígitos:")
    lock_bump(m.chat.id); await state.update_data(anio=m.text.strip()); await m.answer("🔢 Serie:"); await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie)
async def step_serie(m, state): lock_bump(m.chat.id); await state.update_data(serie=m.text.strip()); await m.answer("🔧 Motor:"); await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor)
async def step_motor(m, state): lock_bump(m.chat.id); await state.update_data(motor=m.text.strip()); await m.answer("👤 Nombre:"); await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre)
async def step_nombre(m, state):
    datos = await state.get_data(); datos["nombre"] = m.text.strip()
    try:
        folio = await asyncio.to_thread(nuevo_folio)
        datos["folio"] = folio
        await m.answer("📄 Generando PDF...")
        path = await asyncio.to_thread(_make_pdf, datos)
        nombre_pdf = f"{folio}_{int(time.time())}.pdf"
        url_pdf = await asyncio.to_thread(_upload_pdf, path, nombre_pdf)
        await m.answer_document(open(path, "rb"), caption=f"✅ PDF listo\nFolio: {folio}\n🔗 {url_pdf}")
        await supabase_insert_retry(TABLE_REGISTROS, {
            "folio": folio, "marca": datos["marca"], "linea": datos["linea"], "anio": datos["anio"],
            "numero_serie": datos["serie"], "numero_motor": datos["motor"], "nombre": datos["nombre"],
            "entidad": "CDMX", "url_pdf": url_pdf,
            "fecha_expedicion": datetime.now().date().isoformat(),
            "fecha_vencimiento": (datetime.now().date() + timedelta(days=30)).isoformat(),
        })
        await m.answer("🎉 ¡Listo!")
    except Exception as e:
        await m.answer(f"❌ Error: {e}")
    finally:
        await state.finish(); lock_release(m.chat.id)

@dp.message_handler()
async def fallback(m, state): await m.answer("👋 Usa /permiso para iniciar")

# FASTAPI
_keep_task = _sweeper_task = None
_keep_session: aiohttp.ClientSession = None

async def keep_alive():
    global _keep_session
    if not BASE_URL: return
    _keep_session = aiohttp.ClientSession()
    try:
        while True:
            try: await _keep_session.get(f"{BASE_URL}/", timeout=10)
            except: pass
            await asyncio.sleep(600)
    finally:
        if _keep_session and not _keep_session.closed:
            await _keep_session.close()

@asynccontextmanager
async def lifespan(app):
    global _keep_task, _sweeper_task
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", drop_pending_updates=True, allowed_updates=["message"])
    _keep_task = asyncio.create_task(keep_alive())
    _sweeper_task = asyncio.create_task(_sweeper())
    yield
    for t in (_keep_task, _sweeper_task):
        if t: t.cancel(); with suppress(asyncio.CancelledError): await t
    if _keep_session and not _keep_session.closed:
        await _keep_session.close()
    with suppress(Exception): await bot.delete_webhook()
    with suppress(Exception): await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    try: return {"ok": True, "webhook": (await bot.get_webhook_info()).url}
    except Exception as e: return {"ok": False, "error": str(e)}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    Bot.set_current(bot); Dispatcher.set_current(dp)
    asyncio.create_task(dp.process_update(types.Update(**data)))
    return {"ok": True}def lock_bump(cid): ACTIVE[cid] = _now() + FLOW_TTL
def lock_release(cid): ACTIVE.pop(cid, None)
async def _sweeper():
    while True:
        await asyncio.sleep(30)
        now = _now()
        [ACTIVE.pop(cid) for cid, t in list(ACTIVE.items()) if t <= now]

# Slug
def _slug(s):
    nfkd = unicodedata.normalize("NFKD", s or "")
    s2 = "".join(c for c in nfkd if not unicodedata.combining(c)).replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s2)

# Supabase Insert
async def supabase_insert_retry(table, row, attempts=4, delay=0.6):
    for i in range(attempts):
        try:
            return await asyncio.to_thread(lambda: supabase.table(table).insert(row).execute().data)
        except Exception as e:
            if i == attempts - 1: raise e
            await asyncio.sleep(delay * (i + 1))

def nuevo_folio(prefix=FOLIO_PREFIX):
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).match({"id": nid}).execute()
    except: pass
    return folio

# Coords
coords = {
    "folio": (87, 130, 14, (1, 0, 0)), "fecha": (130, 145, 12, (0, 0, 0)),
    "marca": (87, 290, 11, (0, 0, 0)), "serie": (375, 290, 11, (0, 0, 0)),
    "linea": (87, 307, 11, (0, 0, 0)), "motor": (375, 307, 11, (0, 0, 0)),
    "anio": (87, 323, 11, (0, 0, 0)), "vigencia": (375, 323, 11, (0, 0, 0)),
    "nombre": (375, 340, 11, (0, 0, 0)),
}

def _make_pdf(datos):
    out_path = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_cdmx.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)
    fecha_visual = fecha_exp.strftime("%d DE %B DE %Y").upper()
    vigencia_visual = fecha_ven.strftime("%d/%m/%Y")

    pg.insert_text(coords["folio"][:2], datos["folio"], fontsize=coords["folio"][2], color=coords["folio"][3])
    pg.insert_text(coords["fecha"][:2], fecha_visual, fontsize=coords["fecha"][2], color=coords["fecha"][3])
    for k in ["marca", "serie", "linea", "motor", "anio"]:
        pg.insert_text(coords[k][:2], str(datos.get(k, "")), fontsize=coords[k][2], color=coords[k][3])
    pg.insert_text(coords["vigencia"][:2], vigencia_visual, fontsize=coords["vigencia"][2], color=coords["vigencia"][3])
    pg.insert_text(coords["nombre"][:2], datos.get("nombre", ""), fontsize=coords["nombre"][2], color=coords["nombre"][3])

    # QR
    qr_text = (
        f"Folio: {datos['folio']}\nMarca: {datos.get('marca','')}\nLínea: {datos.get('linea','')}\n"
        f"Año: {datos.get('anio','')}\nSerie: {datos.get('serie','')}\nMotor: {datos.get('motor','')}\n"
        f"Nombre: {datos.get('nombre','')}\nSEMOVICDMX DIGITAL"
    )
    qr = qrcode.make(qr_text)
    qr_path = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_qr.png")
    qr.save(qr_path)

    tam = 1.6 * 28.35
    x0, y0 = (pg.rect.width / 2 - tam / 2) - 19, 680.17
    pg.insert_image(fitz.Rect(x0, y0, x0 + tam, y0 + tam), filename=qr_path)

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

# FSM
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# HANDLERS
@dp.message_handler(Command("start"), state="*")
async def cmd_start(m, state):
    await state.finish(); lock_release(m.chat.id)
    await m.answer("👋 Bot listo. Usa /permiso para iniciar.")

@dp.message_handler(commands=["cancel", "stop"], state="*")
async def cmd_cancel(m, state):
    await state.finish(); lock_release(m.chat.id)
    await m.answer("❎ Flujo cancelado.")

@dp.message_handler(Command("permiso"), state="*")
async def permiso_init(m, state):
    if await state.get_state():
        return await m.answer("⚠️ Ya tienes un flujo. Manda /cancel.")
    if not lock_acquire(m.chat.id):
        return await m.answer("⚠️ Espera unos minutos.")
    await m.answer("🚗 Marca del vehículo:")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca)
async def step_marca(m, state):
    lock_bump(m.chat.id)
    await state.update_data(marca=m.text.strip())
    await m.answer("📱 Línea:")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea)
async def step_linea(m, state):
    lock_bump(m.chat.id)
    await state.update_data(linea=m.text.strip())
    await m.answer("📅 Año (4 dígitos):")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio)
async def step_anio(m, state):
    text = m.text.strip()
    if not text.isdigit() or len(text) != 4:
        return await m.answer("❌ Año válido de 4 dígitos:")
    lock_bump(m.chat.id)
    await state.update_data(anio=text)
    await m.answer("🔢 Serie:")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie)
async def step_serie(m, state):
    lock_bump(m.chat.id)
    await state.update_data(serie=m.text.strip())
    await m.answer("🔧 Motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor)
async def step_motor(m, state):
    lock_bump(m.chat.id)
    await state.update_data(motor=m.text.strip())
    await m.answer("👤 Nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre)
async def step_nombre(m, state):
    datos = await state.get_data()
    datos["nombre"] = m.text.strip()
    try:
        folio = await asyncio.to_thread(nuevo_folio)
        datos["folio"] = folio
        await m.answer("📄 Generando PDF...")
        path = await asyncio.to_thread(_make_pdf, datos)
        nombre_pdf = f"{folio}_{int(time.time())}.pdf"
        url_pdf = await asyncio.to_thread(_upload_pdf, path, nombre_pdf)
        await m.answer_document(open(path, "rb"), caption=f"✅ PDF listo\nFolio: {folio}\n🔗 {url_pdf}")
        await supabase_insert_retry(TABLE_REGISTROS, {
            "folio": folio, "marca": datos["marca"], "linea": datos["linea"],
            "anio": datos["anio"], "numero_serie": datos["serie"],
            "numero_motor": datos["motor"], "nombre": datos["nombre"],
            "entidad": "CDMX", "url_pdf": url_pdf,
            "fecha_expedicion": datetime.now().date().isoformat(),
            "fecha_vencimiento": (datetime.now().date() + timedelta(days=30)).isoformat(),
        })
        await m.answer("🎉 ¡Listo! Usa /permiso para otro.")
    except Exception as e:
        await m.answer(f"❌ Error: {e}")
    finally:
        await state.finish(); lock_release(m.chat.id)

@dp.message_handler()
async def fallback(m, state):
    await m.answer("👋 Usa /permiso para iniciar")

# -------- WEBHOOKS & FASTAPI --------
_keep_task = _sweeper_task = None
_keep_session: aiohttp.ClientSession = None

async def keep_alive():
    global _keep_session
    if not BASE_URL: return
    _keep_session = aiohttp.ClientSession()
    try:
        while True:
            try: await _keep_session.get(f"{BASE_URL}/", timeout=10)
            except: pass
            await asyncio.sleep(600)
    finally:
        if _keep_session and not _keep_session.closed:
            await _keep_session.close()

@asynccontextmanager
async def lifespan(app):
    global _keep_task, _sweeper_task
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", drop_pending_updates=True, allowed_updates=["message"])
    _keep_task = asyncio.create_task(keep_alive())
    _sweeper_task = asyncio.create_task(_sweeper())
    yield
    for t in (_keep_task, _sweeper_task):
        if t: t.cancel(); with suppress(asyncio.CancelledError): await t
    if _keep_session and not _keep_session.closed:
        await _keep_session.close()
    with suppress(Exception): await bot.delete_webhook()
    with suppress(Exception): await bot.session.close()

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
    data = await request.json()
    Bot.set_current(bot)
    Dispatcher.set_current(dp)
    asyncio.create_task(dp.process_update(types.Update(**data)))
    return {"ok": True}
