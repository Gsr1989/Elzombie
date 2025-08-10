import os
import logging
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.types import Update
from contextlib import asynccontextmanager

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Env vars
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado")

# Bot y dispatcher
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# /start
@dp.message_handler(Command("start"))
async def start(msg: types.Message):
    await msg.answer("🚀 ¡Hola! Bot arriba en Render.")

@dp.message_handler()
async def echo(msg: types.Message):
    await msg.answer(f"📝 Me dijiste: {msg.text}")

# Ciclo de vida
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando bot...")
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url)
        logger.info(f"✅ Webhook configurado: {webhook_url}")
    else:
        logger.warning("⚠️ BASE_URL no configurada, no se estableció webhook")
    yield
    logger.info("🛑 Cerrando bot...")
    await bot.delete_webhook()
    await bot.session.close()

# FastAPI
app = FastAPI(title="Bot Permisos Digitales", lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update(**data)
        await dp.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=400, detail="Error procesando update")

@app.get("/")
def health():
    return {"ok": True, "status": "funcionando"}# Context manager para startup/shutdown
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
