import os
import logging
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Update
from contextlib import asynccontextmanager

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables de entorno
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")

# Inicializar bot y dispatcher
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# Manejador del comando /start
@dp.message(Command("start"))
async def start(msg):
    await msg.answer("🚀 ¡Hola! Soy tu bot de permisos digitales.\n\n"
                    "Estoy funcionando en la nube con Render.\n"
                    "¡Listo para procesar tus permisos!")

# Manejador de todos los otros mensajes
@dp.message()
async def echo(msg):
    await msg.answer(f"📝 Me dijiste: {msg.text}\n\n"
                    "Próximamente tendré menús chingones para generar permisos. 😎")

# Context manager para startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("🚀 Iniciando bot...")
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url)
        logger.info(f"✅ Webhook configurado: {webhook_url}")
    else:
        logger.warning("⚠️ BASE_URL no configurada, webhook no establecido")
    
    yield
    
    # Shutdown
    logger.info("🛑 Cerrando bot...")
    await bot.delete_webhook()
    await bot.session.close()

# Crear app FastAPI
app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=400, detail="Error procesando update")

@app.get("/")
def health():
    return {
        "ok": True,
        "service": "Bot Permisos Digitales",
        "status": "funcionando",
        "webhook_url": f"{BASE_URL}/webhook" if BASE_URL else "no configurado"
    }

@app.get("/info")
def info():
    return {
        "bot_token_configured": bool(BOT_TOKEN),
        "base_url_configured": bool(BASE_URL),
        "webhook_url": f"{BASE_URL}/webhook" if BASE_URL else None
    }
