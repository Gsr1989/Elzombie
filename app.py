# app.py
# -------------------------------------------------------------
# Bot de Telegram (Aiogram v2) + FastAPI con webhook en Render
# Rutas:
#   GET  /          -> healthcheck (muestra estado y URL del webhook)
#   GET  /info      -> flags de configuración de envs
#   POST /webhook   -> endpoint que Telegram llama con updates
#   GET  /webhook   -> ping manual (útil para probar 200 OK)
# -------------------------------------------------------------

import os
import re
import logging
import tempfile
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager

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
BASE_URL = os.getenv("BASE_URL", "")  # ej: https://elzombie.onrender.com

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")

# ---------- BOT ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

# ---------- Helpers: parse de fecha/hora tolerante ----------
def parse_fecha(texto: str) -> str:
    """
    Acepta:
      - YYYY-MM-DD (recomendado)
      - DD/MM/YYYY
      - DD-MM-YYYY
      - DD.MM.YYYY
      - 'hoy' | 'mañana' (sin acento tb)
    Devuelve string normalizado YYYY-MM-DD o lanza ValueError.
    """
    t = texto.strip().lower()

    # Palabras
    if t in ("hoy",):
        return datetime.now().strftime("%Y-%m-%d")
    if t in ("manana", "mañana"):
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # YYYY-MM-DD
    try:
        dt = datetime.strptime(t, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    # DD/MM/YYYY
    for fmt in ("%d/%Y/%m",):  # (evitar confusión) no aplicar
        pass  # placeholder para claridad

    # DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(t, fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            continue

    raise ValueError("fecha")

def parse_hora(texto: str) -> str:
    """
    Acepta:
      - HH:MM (24h) -> 00..23 : 00..59
      - H:MM am/pm  o HH:MMam / HH:MM pm
      - H.MM  (reemplaza punto por dos puntos)
      - 'ahora' -> hora actual redondeada a minuto
    Devuelve HH:MM (24h) o lanza ValueError.
    """
    t = texto.strip().lower().replace(" ", "")
    if t == "ahora":
        return datetime.now().strftime("%H:%M")

    # Permitir H.MM
    if re.fullmatch(r"\d{1,2}\.\d{2}", t):
        t = t.replace(".", ":")

    # 24h
    if re.fullmatch(r"\d{1,2}:\d{2}", t):
        try:
            dt = datetime.strptime(t, "%H:%M")
            return dt.strftime("%H:%M")
        except Exception:
            pass

    # 12h con am/pm (ej: 2:30pm, 12:05 am)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(am|pm)", t)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2))
        ap = m.group(3)
        if not (1 <= h <= 12 and 0 <= mm <= 59):
            raise ValueError("hora")
        if ap == "am":
            h24 = 0 if h == 12 else h
        else:  # pm
            h24 = 12 if h == 12 else h + 12
        return f"{h24:02d}:{mm:02d}"

    raise ValueError("hora")

# ---------- FSM: Flujo de Permiso ----------
class PermisoForm(StatesGroup):
    nombre = State()
    motivo = State()
    destino = State()
    fecha = State()
    hora = State()

def _make_pdf(datos: dict) -> str:
    """
    Genera un PDF simple con ReportLab y regresa la ruta del archivo.
    datos = {nombre, motivo, destino, fecha, hora, folio}
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
    c.drawString(1 * inch, y, "Permiso de Salida")
    y -= 0.4 * inch

    c.setFont("Helvetica", 12)
    lineas = [
        f"Folio: {datos['folio']}",
        f"Nombre: {datos['nombre']}",
        f"Motivo: {datos['motivo']}",
        f"Destino: {datos['destino']}",
        f"Fecha: {datos['fecha']}",
        f"Hora: {datos['hora']}",
        f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    for t in lineas:
        c.drawString(1 * inch, y, t)
        y -= 0.3 * inch

    c.line(1 * inch, y - 0.4 * inch, 3.5 * inch, y - 0.4 * inch)
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
        "• /permiso – genera un permiso en PDF\n"
        "• /cancel – cancela el proceso actual"
    )

@dp.message_handler(Command("permiso"))
async def permiso_init(m: types.Message, state: FSMContext):
    await state.finish()
    await m.answer("🧾 Vamos a generar tu permiso.\n\n¿Cuál es tu *nombre completo*?")
    await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre, content_types=types.ContentTypes.TEXT)
async def permiso_nombre(m: types.Message, state: FSMContext):
    await state.update_data(nombre=m.text.strip())
    await m.answer("Motivo del permiso (p. ej. *Trámite*, *Médico*, *Personal*):")
    await PermisoForm.motivo.set()

@dp.message_handler(state=PermisoForm.motivo, content_types=types.ContentTypes.TEXT)
async def permiso_motivo(m: types.Message, state: FSMContext):
    await state.update_data(motivo=m.text.strip())
    await m.answer("Destino (p. ej. *CDMX*, *Oficina central*, *Hospital X*):")
    await PermisoForm.destino.set()

@dp.message_handler(state=PermisoForm.destino, content_types=types.ContentTypes.TEXT)
async def permiso_destino(m: types.Message, state: FSMContext):
    await state.update_data(destino=m.text.strip())
    await m.answer("Fecha (acepto `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `hoy`, `mañana`):")
    await PermisoForm.fecha.set()

@dp.message_handler(state=PermisoForm.fecha, content_types=types.ContentTypes.TEXT)
async def permiso_fecha(m: types.Message, state: FSMContext):
    raw = m.text.strip()
    try:
        fecha_norm = parse_fecha(raw)
    except ValueError:
        await m.answer("❌ Formato inválido.\nEjemplos válidos: `2025-08-10`, `10/08/2025`, `hoy`, `mañana`.\nIntenta de nuevo:")
        return
    await state.update_data(fecha=fecha_norm)
    await m.answer("Hora (acepto `14:30`, `2:30pm`, `2:30 pm`, `14.30`, `ahora`):")
    await PermisoForm.hora.set()

@dp.message_handler(state=PermisoForm.hora, content_types=types.ContentTypes.TEXT)
async def permiso_hora(m: types.Message, state: FSMContext):
    raw = m.text.strip()
    try:
        hora_norm = parse_hora(raw)
    except ValueError:
        await m.answer("❌ Formato inválido. Ejemplos: `14:30`, `2:30pm`, `09:05`, `ahora`. Intenta de nuevo:")
        return

    datos = await state.get_data()
    datos["hora"] = hora_norm
    datos["folio"] = f"P-{m.from_user.id}-{int(datetime.now().timestamp())}"

    # Generar PDF
    path = _make_pdf(datos)
    caption = (
        "✅ Permiso generado\n"
        f"Folio: {datos['folio']}\n"
        f"Nombre: {datos['nombre']}\n"
        f"Motivo: {datos['motivo']}\n"
        f"Destino: {datos['destino']}\n"
        f"Fecha/Hora: {datos['fecha']}  {datos['hora']}"
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
    await msg.answer("No entendí. Usa /permiso para generar un PDF o /start para ver ayuda.")

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
    # aviso: get_session() es la forma nueva (evita DeprecationWarning)
    try:
        session = await bot.get_session()
        await session.close()
    except Exception:
        pass

# ---------- FASTAPI ----------
app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.get("/")
def health():
    """
    Healthcheck. Útil para ver si la app corre y qué webhook quedó.
    """
    return {
        "ok": True,
        "service": "Bot Permisos Digitales",
        "status": "funcionando",
        "webhook_url": f"{BASE_URL}/webhook" if BASE_URL else "no configurado",
    }

@app.get("/info")
def info():
    """
    Info rápida de configuración.
    """
    return {
        "bot_token_configured": bool(BOT_TOKEN),
        "base_url_configured": bool(BASE_URL),
    }

@app.get("/webhook")
def webhook_ping():
    """
    GET /webhook: solo para probar desde el navegador (Telegram usa POST).
    """
    return {"ok": True, "detail": "Webhook GET listo (Telegram usará POST)."}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    POST /webhook: Telegram enviará aquí los updates.
    """
    try:
        data = await request.json()
        logger.info(f"UPDATE ENTRANTE: {data}")   # visible en logs de Render
        update = Update(**data)
        await dp.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.exception("Error en webhook")
        raise HTTPException(status_code=400, detail=str(e))
