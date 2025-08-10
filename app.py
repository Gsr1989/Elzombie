# app.py
# -------------------------------------------------------------
# Bot de Telegram (Aiogram v2) + FastAPI con webhook (Render)
#
# Rutas:
#   GET  /           -> healthcheck (estado y URL del webhook)
#   GET  /info       -> flags de configuración de envs
#   GET  /webhook    -> ping manual (prueba 200 OK desde navegador)
#   POST /webhook    -> endpoint que Telegram llama con updates
#
# Flujo /permiso (formulario):
#   1) Marca
#   2) Línea
#   3) Año (AAAA)
#   4) Serie
#   5) Motor
#   6) Nombre del solicitante
#   -> Genera PDF y lo envía
# -------------------------------------------------------------

import os
import re
import logging
import tempfile
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import Update

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("permiso-bot")

# ---------- ENV VARS ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BASE_URL  = os.getenv("BASE_URL", "")  # ej: https://tu-app.onrender.com
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")

# ---------- BOT (Aiogram v2) ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(bot, storage=storage)

# ---------- FSM: Formulario de Permiso ----------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    solicitante = State()

# ---------- Helpers ----------
def _validar_anio(texto: str) -> str:
    """Devuelve AAAA si es válido (1900-2099) o lanza ValueError."""
    t = texto.strip()
    if re.fullmatch(r"(19|20)\d{2}", t):
        return t
    raise ValueError("año inválido")

def _make_pdf(datos: dict) -> str:
    """
    Genera un PDF simple con ReportLab y regresa la ruta del archivo.
    datos = {marca, linea, anio, serie, motor, solicitante, folio}
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch

    tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    path = tmp.name
    tmp.close()

    c = canvas.Canvas(path, pagesize=LETTER)
    w, h = LETTER

    y = h - 1.25 * inch
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1 * inch, y, "Solicitud de Permiso")
    y -= 0.4 * inch

    c.setFont("Helvetica", 12)
    lineas = [
        f"Folio:          {datos['folio']}",
        f"Marca:          {datos['marca']}",
        f"Línea:          {datos['linea']}",
        f"Año:            {datos['anio']}",
        f"Serie:          {datos['serie']}",
        f"Motor:          {datos['motor']}",
        f"Solicitante:    {datos['solicitante']}",
        f"Generado:       {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    for t in lineas:
        c.drawString(1 * inch, y, t)
        y -= 0.3 * inch

    # Área de firma
    y -= 0.2 * inch
    c.line(1 * inch, y - 0.4 * inch, 3.8 * inch, y - 0.4 * inch)
    c.drawString(1 * inch, y - 0.6 * inch, "Firma del responsable")

    c.showPage()
    c.save()
    return path

# ---------- HANDLERS ----------
@dp.message_handler(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer(
        "👋 ¡Listo! Bot arriba.\n\n"
        "Comandos:\n"
        "• /permiso – capturar formulario y generar PDF\n"
        "• /cancel – cancela el proceso actual"
    )

@dp.message_handler(Command("permiso"))
async def permiso_init(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("🧾 Vamos a generar tu permiso.\n\n1/6) ¿Cuál es la *Marca*?")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca, content_types=types.ContentTypes.TEXT)
async def permiso_marca(m: types.Message, state: FSMContext):
    await state.update_data(marca=m.text.strip())
    await m.answer("2/6) *Línea*:")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea, content_types=types.ContentTypes.TEXT)
async def permiso_linea(m: types.Message, state: FSMContext):
    await state.update_data(linea=m.text.strip())
    await m.answer("3/6) *Año* (formato AAAA, ej. 2023):")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio, content_types=types.ContentTypes.TEXT)
async def permiso_anio(m: types.Message, state: FSMContext):
    try:
        anio = _validar_anio(m.text)
    except ValueError:
        await m.answer("❌ Año inválido. Usa *AAAA* entre 1900 y 2099 (ej. 2022). Intenta de nuevo:")
        return
    await state.update_data(anio=anio)
    await m.answer("4/6) *Serie*:")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie, content_types=types.ContentTypes.TEXT)
async def permiso_serie(m: types.Message, state: FSMContext):
    await state.update_data(serie=m.text.strip())
    await m.answer("5/6) *Motor*:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor, content_types=types.ContentTypes.TEXT)
async def permiso_motor(m: types.Message, state: FSMContext):
    await state.update_data(motor=m.text.strip())
    await m.answer("6/6) *Nombre del solicitante* (nombre completo):")
    await PermisoForm.solicitante.set()

@dp.message_handler(state=PermisoForm.solicitante, content_types=types.ContentTypes.TEXT)
async def permiso_solicitante(m: types.Message, state: FSMContext):
    await state.update_data(solicitante=m.text.strip())

    datos = await state.get_data()
    datos["folio"] = f"P-{m.from_user.id}-{int(datetime.now().timestamp())}"

    # Generar PDF
    path = _make_pdf(datos)
    caption = (
        "✅ Permiso generado\n"
        f"Folio: {datos['folio']}\n"
        f"Marca/Línea: {datos['marca']} {datos['linea']}\n"
        f"Año: {datos['anio']}  |  Serie: {datos['serie']}  |  Motor: {datos['motor']}\n"
        f"Solicitante: {datos['solicitante']}"
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

# Fallback: cualquier texto fuera del flujo
@dp.message_handler()
async def fallback(msg: types.Message, state: FSMContext):
    await msg.answer("No entendí. Usa /permiso para capturar el formulario o /start para ver ayuda.")

# ---------- LIFESPAN: set/unset webhook al iniciar/parar ----------
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
        session = await bot.get_session()  # forma nueva para cerrar sesión
        await session.close()
    except Exception:
        pass

# ---------- FASTAPI (app + rutas) ----------
app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.get("/")
def health():
    """GET / -> Healthcheck."""
    return {
        "ok": True,
        "service": "Bot Permisos Digitales",
        "status": "funcionando",
        "webhook_url": f"{BASE_URL}/webhook" if BASE_URL else "no configurado",
    }

@app.get("/info")
def info():
    """GET /info -> Info rápida de configuración desde ENV."""
    return {
        "bot_token_configured": bool(BOT_TOKEN),
        "base_url_configured": bool(BASE_URL),
    }

@app.get("/webhook")
def webhook_ping():
    """GET /webhook -> Ping manual (desde navegador). Telegram usa POST."""
    return {"ok": True, "detail": "Webhook GET listo (Telegram usará POST)."}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    POST /webhook -> Telegram envía aquí cada Update.
    Importante: fijar el contexto de bot/dispatcher antes de process_update.
    """
    data = await request.json()
    logger.info(f"UPDATE ENTRANTE: {data}")

    try:
        update = Update(**data)  # pydantic v1
    except Exception as e:
        logger.exception(f"❌ No pude parsear Update: {e}")
        return {"ok": True, "note": "parse_failed"}

    try:
        # FIX: evita "No se puede obtener la instancia del bot del contexto"
        Bot.set_current(bot)
        Dispatcher.set_current(dp)
        await dp.process_update(update)
    except Exception as e:
        logger.exception(f"❌ Error procesando update: {e}")
        return {"ok": True, "note": "handler_failed"}

    return {"ok": True}
