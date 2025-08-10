# app.py
# -------------------------------------------------------------
# Bot de Telegram (Aiogram v2) + FastAPI con webhook (Render)
#
# Rutas:
#   GET  /            -> healthcheck (200 OK)
#   GET  /info        -> flags de config
#   GET  /webhook     -> ping manual (200 OK)
#   POST /webhook     -> endpoint real que Telegram llama
# Comandos del bot:
#   /start   -> ayuda
#   /permiso -> inicia formulario de permiso
#   /cancel  -> cancela el flujo actual
# -------------------------------------------------------------

import os
import re
import logging
import tempfile
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager

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
BASE_URL = os.getenv("BASE_URL", "")  # p.ej. https://elzombie.onrender.com
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")

# ---------- BOT & DISPATCHER ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

# MUY IMPORTANTE: fija las instancias en el contexto (evita el error de Aiogram)
Bot.set_current(bot)
Dispatcher.set_current(dp)

# ---------- FSM (formulario de permiso) ----------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    solicitante = State()

# ---------- Helpers de validación ----------
def normaliza_anio(txt: str) -> int:
    m = re.fullmatch(r"\d{4}", txt.strip())
    if not m:
        raise ValueError("año")
    year = int(txt)
    if not (1900 <= year <= 2100):
        raise ValueError("año")
    return year

def _make_pdf(datos: dict) -> str:
    """
    Genera un PDF simple con ReportLab y regresa la ruta del archivo.
    datos = {marca, linea, anio, serie, motor, solicitante, folio}
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
    y -= 0.45 * inch

    c.setFont("Helvetica", 12)
    lineas = [
        f"Folio: {datos['folio']}",
        f"Marca: {datos['marca']}",
        f"Línea: {datos['linea']}",
        f"Año: {datos['anio']}",
        f"Serie (VIN): {datos['serie']}",
        f"Motor: {datos['motor']}",
        f"Solicitante: {datos['solicitante']}",
        f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    for t in lineas:
        c.drawString(1 * inch, y, t)
        y -= 0.3 * inch

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
        "• /permiso – genera PDF con datos del vehículo\n"
        "• /cancel – cancela el proceso actual"
    )

@dp.message_handler(Command("cancel"), state="*")
async def cmd_cancel(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("🛑 Proceso cancelado. Usa /permiso para empezar de nuevo.")

@dp.message_handler(Command("permiso"))
async def permiso_init(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("🚗 Empecemos.\n\n¿Marca?")
    await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca, content_types=types.ContentTypes.TEXT)
async def permiso_marca(m: types.Message, state: FSMContext):
    await state.update_data(marca=m.text.strip())
    await m.answer("¿Línea (modelo)?")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea, content_types=types.ContentTypes.TEXT)
async def permiso_linea(m: types.Message, state: FSMContext):
    await state.update_data(linea=m.text.strip())
    await m.answer("¿Año? (formato: 4 dígitos, ej. 2019)")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio, content_types=types.ContentTypes.TEXT)
async def permiso_anio(m: types.Message, state: FSMContext):
    try:
        anio = normaliza_anio(m.text)
    except ValueError:
        await m.answer("❌ Año inválido. Escribe 4 dígitos (ej. 2019).")
        return
    await state.update_data(anio=anio)
    await m.answer("¿Serie (VIN)?")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie, content_types=types.ContentTypes.TEXT)
async def permiso_serie(m: types.Message, state: FSMContext):
    await state.update_data(serie=m.text.strip())
    await m.answer("¿Motor?")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor, content_types=types.ContentTypes.TEXT)
async def permiso_motor(m: types.Message, state: FSMContext):
    await state.update_data(motor=m.text.strip())
    await m.answer("Nombre del solicitante:")
    await PermisoForm.solicitante.set()

@dp.message_handler(state=PermisoForm.solicitante, content_types=types.ContentTypes.TEXT)
async def permiso_solicitante(m: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["solicitante"] = m.text.strip()
    datos["folio"] = f"P-{m.from_user.id}-{int(datetime.now().timestamp())}"

    path = _make_pdf(datos)
    caption = (
        "✅ PDF generado\n"
        f"Folio: {datos['folio']}\n"
        f"{datos['marca']} {datos['linea']} {datos['anio']}\n"
        f"Serie: {datos['serie']} | Motor: {datos['motor']}\n"
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

# Fallback para cualquier texto fuera del flujo
@dp.message_handler()
async def fallback(msg: types.Message, state: FSMContext):
    await msg.answer("No entendí. Usa /permiso para generar el PDF o /start para ver ayuda.")

# ---------- LIFESPAN (configurar webhook al arrancar) ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando bot...")
    try:
        Bot.set_current(bot)
        Dispatcher.set_current(dp)
        if BASE_URL:
            webhook_url = f"{BASE_URL}/webhook"
            await bot.set_webhook(webhook_url)
            logger.info(f"✅ Webhook configurado: {webhook_url}")
        else:
            logger.warning("⚠️ BASE_URL no configurada. Sin webhook.")
        yield
    finally:
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
app = FastAPI(title="Bot Permisos (vehículos)", lifespan=lifespan)

@app.get("/")
async def root():
    """
    Healthcheck para Render. Mantiene la instancia viva (200 OK).
    """
    return {"ok": True, "service": "permiso-bot", "webhook": f"{BASE_URL}/webhook" if BASE_URL else None}

@app.get("/info")
async def info():
    """
    Flags de configuración rápidos.
    """
    return {"bot_token_configured": bool(BOT_TOKEN), "base_url_configured": bool(BASE_URL)}

@app.get("/webhook")
async def webhook_get():
    """
    Ping manual al webhook (GET). Telegram usa POST.
    """
    return {"ok": True, "detail": "Webhook GET listo (Telegram usa POST)."}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Endpoint real que Telegram llama con cada update.
    SIEMPRE devolvemos 200 para evitar reintentos.
    """
    try:
        data = await request.json()
        logger.info(f"UPDATE ENTRANTE: {data}")
    except Exception as e:
        logger.exception("No se pudo leer JSON del request")
        return {"ok": True, "note": "bad_json"}

    # Asegura instancias en el contexto antes de procesar
    Bot.set_current(bot)
    Dispatcher.set_current(dp)

    try:
        update = types.Update(**data)  # pydantic v1 (aiogram v2)
    except Exception as e:
        logger.exception(f"No pude parsear Update: {e}")
        return {"ok": True, "note": "parse_failed"}

    try:
        await dp.process_update(update)
    except Exception as e:
        logger.exception(f"Error en handlers: {e}")
        return {"ok": True, "note": "handler_failed"}

    return {"ok": True}# ===== MENÚ START =====
@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("Permiso"))
    await message.answer("Bienvenido, ¿qué deseas hacer?", reply_markup=markup)

# ===== COMANDO /permiso O BOTÓN =====
@dp.message_handler(lambda m: m.text.lower() in ["permiso", "/permiso"])
async def permiso_cmd(message: types.Message):
    await message.answer("Ingresa la marca:")
    await PermisoForm.marca.set()

# ===== FLUJO DEL FORMULARIO =====
@dp.message_handler(state=PermisoForm.marca)
async def set_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text)
    await message.answer("Ingresa la línea:")
    await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea)
async def set_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text)
    await message.answer("Ingresa el año:")
    await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio)
async def set_anio(message: types.Message, state: FSMContext):
    await state.update_data(anio=message.text)
    await message.answer("Ingresa el número de serie:")
    await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie)
async def set_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text)
    await message.answer("Ingresa el número de motor:")
    await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor)
async def set_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text)
    await message.answer("Ingresa el nombre del solicitante:")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre)
async def set_nombre(message: types.Message, state: FSMContext):
    await state.update_data(nombre=message.text)
    data = await state.get_data()

    # Aquí podrías generar el PDF o guardar en DB
    resumen = (
        f"✅ Permiso capturado:\n"
        f"Marca: {data['marca']}\n"
        f"Línea: {data['linea']}\n"
        f"Año: {data['anio']}\n"
        f"Serie: {data['serie']}\n"
        f"Motor: {data['motor']}\n"
        f"Nombre: {data['nombre']}"
    )

    await message.answer(resumen)
    await state.finish()

# ===== WEBHOOK =====
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    logger.info(f"UPDATE ENTRANTE: {data}")

    try:
        update = types.Update(**data)
    except Exception as e:
        logger.exception(f"No pude parsear Update: {e}")
        return {"ok": True, "note": "parse_failed"}

    try:
        # Fix de contexto por cada request
        Bot.set_current(bot)
        Dispatcher.set_current(dp)

        await dp.process_update(update)
    except Exception as e:
        logger.exception(f"Error procesando update: {e}")
        return {"ok": True, "note": "handler_failed"}

    return {"ok": True}

# ===== ARRANQUE LOCAL =====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
