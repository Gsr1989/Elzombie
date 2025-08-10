import os
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
    await m.answer("Fecha (formato YYYY-MM-DD):")
    await PermisoForm.fecha.set()

@dp.message_handler(state=PermisoForm.fecha, content_types=types.ContentTypes.TEXT)
async def permiso_fecha(m: types.Message, state: FSMContext):
    fecha = m.text.strip()
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except Exception:
        await m.answer("❌ Formato inválido. Usa YYYY-MM-DD. Intenta de nuevo:")
        return
    await state.update_data(fecha=fecha)
    await m.answer("Hora (formato HH:MM 24h):")
    await PermisoForm.hora.set()

@dp.message_handler(state=PermisoForm.hora, content_types=types.ContentTypes.TEXT)
async def permiso_hora(m: types.Message, state: FSMContext):
    hora = m.text.strip()
    try:
        datetime.strptime(hora, "%H:%M")
    except Exception:
        await m.answer("❌ Formato inválido. Usa HH:MM (24h). Intenta de nuevo:")
        return

    datos = await state.get_data()
    datos["hora"] = hora
    datos["folio"] = f"P-{m.from_user.id}-{int(datetime.now().timestamp())}"

    # Generar PDF
    path = _make_pdf(datos)
    caption = (
        "✅ Permiso generado\n"
        f"Folio: {datos['folio']}\n"
        f"Nombre: {datos['nombre']}\n"
        f"Fecha: {datos['fecha']}  {datos['hora']}"
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
    await m.answer("🛑 Proceso cancelado.")

# ---------- LIFESPAN (webhook) ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando bot...")
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url)
        logger.info(f"✅ Webhook: {webhook_url}")
    else:
        logger.warning("⚠️ BASE_URL no configurada. Sin webhook.")
    yield
    logger.info("🛑 Cerrando bot...")
    await bot.delete_webhook()
    await bot.session.close()

# ---------- FASTAPI ----------
app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update(**data)
        await dp.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.exception("Error en webhook")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/")
def health():
    return {
        "ok": True,
        "service": "Bot Permisos Digitales",
        "status": "funcionando",
        "webhook_url": f"{BASE_URL}/webhook" if BASE_URL else "no configurado",
    }

@app.get("/info")
def info():
    return {
        "bot_token_configured": bool(BOT_TOKEN),
        "base_url_configured": bool(BASE_URL),
}
