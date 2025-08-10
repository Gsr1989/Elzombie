# app.py
# -------------------------------------------------------------
# Bot de Telegram (Aiogram v2) + FastAPI con webhook en Render
# Rutas:
#   GET  /          -> healthcheck (estado y URL del webhook)
#   GET  /info      -> flags de configuración
#   GET  /webhook   -> ping manual (OK de prueba)
#   POST /webhook   -> endpoint que Telegram llama con updates
# -------------------------------------------------------------

import os
import re
import logging
import tempfile
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

# Aiogram v2
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("permiso-bot")

# ---------- ENV VARS ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "")  # ej: https://tuapp.onrender.com
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")

# ---------- BOT ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

# ---------- FSM: Formulario de Permiso ----------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()  # nombre del solicitante

# ---------- PDF ----------
def _make_pdf(datos: dict) -> str:
    """
    Crea un PDF con los datos y devuelve la ruta temporal del archivo.
    datos keys: marca, linea, anio, serie, motor, nombre, folio
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    path = tmp.name
    tmp.close()

    c = canvas.Canvas(path, pagesize=LETTER)
    w, h = LETTER

    y = h - 1.25 * inch
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1 * inch, y, "Permiso / Ficha del Vehículo")
    y -= 0.4 * inch

    c.setFont("Helvetica", 12)
    filas = [
        f"Folio: {datos['folio']}",
        f"Marca: {datos['marca']}",
        f"Línea: {datos['linea']}",
        f"Año: {datos['anio']}",
        f"Serie: {datos['serie']}",
        f"Motor: {datos['motor']}",
        f"Nombre del solicitante: {datos['nombre']}",
        f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    for t in filas:
        c.drawString(1 * inch, y, t)
        y -= 0.3 * inch

    c.line(1 * inch, y - 0.4 * inch, 3.5 * inch, y - 0.4 * inch)
    c.drawString(1 * inch, y - 0.6 * inch, "Firma del responsable")

    c.showPage()
    c.save()
    return path

# ---------- VALIDACIONES ----------
def _valida_anio(txt: str) -> str:
    t = txt.strip()
    if re.fullmatch(r"\d{4}", t) and 1900 <= int(t) <= 2100:
        return t
    raise ValueError("anio")

# ---------- HANDLERS ----------
@dp.message_handler(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer(
        "👋 Bot listo.\n\n"
        "Comandos:\n"
        "• /permiso – capturar datos y generar PDF\n"
        "• /cancel – cancelar el proceso actual"
    )

@dp.message_handler(Command("permiso"))
async def permiso_init(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("🧾 Vamos a capturar los datos.\n\nMarca del vehículo:")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca, content_types=types.ContentTypes.TEXT)
async def form_marca(m: types.Message, state: FSMContext):
    await state.update_data(marca=m.text.strip())
    await m.answer("Línea del vehículo (modelo/versión):")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea, content_types=types.ContentTypes.TEXT)
async def form_linea(m: types.Message, state: FSMContext):
    await state.update_data(linea=m.text.strip())
    await m.answer("Año (4 dígitos, ej. 2018):")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio, content_types=types.ContentTypes.TEXT)
async def form_anio(m: types.Message, state: FSMContext):
    try:
        anio = _valida_anio(m.text)
    except ValueError:
        await m.answer("❌ Año inválido. Escribe 4 dígitos (ej. 2018):")
        return
    await state.update_data(anio=anio)
    await m.answer("Serie (VIN):")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie, content_types=types.ContentTypes.TEXT)
async def form_serie(m: types.Message, state: FSMContext):
    await state.update_data(serie=m.text.strip())
    await m.answer("Motor (número/clave):")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor, content_types=types.ContentTypes.TEXT)
async def form_motor(m: types.Message, state: FSMContext):
    await state.update_data(motor=m.text.strip())
    await m.answer("Nombre del solicitante (nombre completo):")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre, content_types=types.ContentTypes.TEXT)
async def form_nombre(m: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = m.text.strip()
    datos["folio"] = f"P-{m.from_user.id}-{int(datetime.now().timestamp())}"

    # Generar PDF
    path = _make_pdf(datos)
    caption = (
        "✅ Datos capturados y PDF generado\n"
        f"Folio: {datos['folio']}\n"
        f"{datos['marca']} {datos['linea']} ({datos['anio']})\n"
        f"Serie: {datos['serie']}  Motor: {datos['motor']}\n"
        f"Solicitante: {datos['nombre']}"
    )
    try:
        with open(path, "rb") as f:
            await m.answer_document(f, caption=caption)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    await state.finish()

@dp.message_handler(Command("cancel"), state="*")
async def cmd_cancel(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("🛑 Proceso cancelado. Usa /permiso para empezar de nuevo.")

@dp.message_handler()
async def fallback(msg: types.Message, state: FSMContext):
    await msg.answer("No entendí. Usa /permiso para capturar el formulario o /start para ver ayuda.")

# ---------- LIFESPAN (set/unset webhook) ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando bot...")
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url)
        logger.info(f"✅ Webhook configurado: {webhook_url}")
    else:
        logger.warning("⚠️ BASE_URL no configurada. Sin webhook.")
    yield
    logger.info("🛑 Cerrando bot...")
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
    """Healthcheck: ver estado y URL del webhook."""
    return {
        "ok": True,
        "service": "Bot Permisos Digitales",
        "status": "funcionando",
        "webhook_url": f"{BASE_URL}/webhook" if BASE_URL else "no configurado",
    }

@app.get("/info")
def info():
    """Flags rápidos de configuración."""
    return {
        "bot_token_configured": bool(BOT_TOKEN),
        "base_url_configured": bool(BASE_URL),
    }

@app.get("/webhook")
def webhook_get():
    """Ping manual para probar desde navegador (Telegram usa POST)."""
    return {"ok": True, "detail": "Webhook GET listo (Telegram usará POST)."}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Telegram envía aquí los updates. Siempre devolvemos 200/JSON
    para que Telegram no reintente en loop.
    """
    try:
        data = await request.json()
        logger.info(f"UPDATE ENTRANTE: {data}")
    except Exception:
        logger.exception("❌ request.json() falló")
        return {"ok": True, "note": "bad_json"}

    # Parse del Update
    try:
        update = types.Update(**data)
    except Exception as e:
        logger.exception(f"❌ No pude parsear Update: {e}")
        return {"ok": True, "note": "parse_failed"}

    try:
        # 🔧 FIX: setear bot/dispatcher actuales en el contexto
        Bot.set_current(bot)
        Dispatcher.set_current(dp)

        await dp.process_update(update)
    except Exception as e:
        logger.exception(f"❌ Error procesando update: {e}")
        return {"ok": True, "note": "handler_failed"}

    return {"ok": True}
