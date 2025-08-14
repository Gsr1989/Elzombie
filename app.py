# app.py — Bot de Telegram con FastAPI y Supabase (aiogram v3)

import os, re, time, asyncio, unicodedata, qrcode, logging, aiohttp, fitz
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from contextlib import asynccontextmanager, suppress
from supabase import create_client

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

# Inicialización
os.makedirs(OUTPUT_DIR, exist_ok=True)
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
logging.basicConfig(level=logging.INFO)

# Candados anti-spam
ACTIVE = {}
def _now(): return time.time()
def lock_acquire(cid): return not ACTIVE.get(cid, 0) > _now() and not ACTIVE.update({cid: _now()+FLOW_TTL})
def lock_bump(cid): ACTIVE[cid] = _now() + FLOW_TTL
def lock_release(cid): ACTIVE.pop(cid, None)

async def _sweeper():
    while True:
        await asyncio.sleep(30)
        now = _now()
        [ACTIVE.pop(cid) for cid, t in list(ACTIVE.items()) if t <= now]

# Utilidades
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

# Coordenadas de inserción en PDF
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

# FSM
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# Telegram handlers (aiogram v3)
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    lock_release(message.chat.id)
    await message.answer("👋 Bot listo. Usa /permiso para iniciar.")

@dp.message(Command("cancel", "stop"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    lock_release(message.chat.id)
    await message.answer("❎ Flujo cancelado.")

@dp.message(Command("permiso"))
async def permiso_init(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        return await message.answer("⚠️ Ya tienes uno activo. Manda /cancel.")
    if not lock_acquire(message.chat.id):
        return await message.answer("⏳ Espera unos minutos.")
    await message.answer("🚗 Marca del vehículo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def step_marca(message: types.Message, state: FSMContext):
    lock_bump(message.chat.id)
    await state.update_data(marca=message.text.strip())
    await message.answer("📱 Línea:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def step_linea(message: types.Message, state: FSMContext):
    lock_bump(message.chat.id)
    await state.update_data(linea=message.text.strip())
    await message.answer("📅 Año (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def step_anio(message: types.Message, state: FSMContext):
    txt = message.text.strip()
    if not txt.isdigit() or len(txt) != 4:
        return await message.answer("❌ Año inválido. Intenta de nuevo:")
    lock_bump(message.chat.id)
    await state.update_data(anio=txt)
    await message.answer("🔢 Serie:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def step_serie(message: types.Message, state: FSMContext):
    lock_bump(message.chat.id)
    await state.update_data(serie=message.text.strip())
    await message.answer("🔧 Motor:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def step_motor(message: types.Message, state: FSMContext):
    lock_bump(message.chat.id)
    await state.update_data(motor=message.text.strip())
    await message.answer("👤 Nombre del contribuyente:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def step_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = message.text.strip()
    try:
        folio = await asyncio.to_thread(nuevo_folio)
        datos["folio"] = folio
        await message.answer("📄 Generando PDF...")
        path = await asyncio.to_thread(_make_pdf, datos)
        nombre_pdf = f"{folio}_{int(time.time())}.pdf"
        url_pdf = await asyncio.to_thread(_upload_pdf, path, nombre_pdf)
        
        with open(path, "rb") as pdf_file:
            await message.answer_document(
                types.BufferedInputFile(pdf_file.read(), filename=f"{folio}.pdf"),
                caption=f"✅ PDF listo\nFolio: {folio}\n🔗 {url_pdf}"
            )
        
        await supabase_insert_retry(TABLE_REGISTROS, {
            "folio": folio, "marca": datos["marca"], "linea": datos["linea"], "anio": datos["anio"],
            "numero_serie": datos["serie"], "numero_motor": datos["motor"], "nombre": datos["nombre"],
            "entidad": "CDMX", "url_pdf": url_pdf,
            "fecha_expedicion": datetime.now().date().isoformat(),
            "fecha_vencimiento": (datetime.now().date() + timedelta(days=30)).isoformat(),
        })
        await message.answer("🎉 ¡Listo! Usa /permiso para otro.")
    except Exception as e:
        logging.exception("❌ Error generando permiso")
        await message.answer(f"❌ Error: {e}")
    finally:
        await state.clear()
        lock_release(message.chat.id)

@dp.message()
async def fallback(message: types.Message):
    await message.answer("👋 Usa /permiso para iniciar")

# FASTAPI + webhook
_keep_task = _sweeper_task = None
_keep_session: aiohttp.ClientSession = None

async def keep_alive():
    global _keep_session
    if not BASE_URL: return
    _keep_session = aiohttp.ClientSession()
    try:
        while True:
            try: 
                await _keep_session.get(f"{BASE_URL}/", timeout=10)
            except: 
                pass
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
        if t:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
    if _keep_session and not _keep_session.closed:
        await _keep_session.close()
    with suppress(Exception): 
        await bot.delete_webhook()
    with suppress(Exception): 
        await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    try: 
        info = await bot.get_webhook_info()
        return {"ok": True, "webhook": info.url}
    except Exception as e: 
        return {"ok": False, "error": str(e)}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return {"ok": True}
