# VERSIÓN DE EMERGENCIA CON POLLING PARA DEBUG
# Usar temporalmente para probar si el bot funciona
# uvicorn app_polling:app --host 0.0.0.0 --port $PORT

import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.filters import Command
from aiogram import types

# Setup básico
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no configurado")

# Bot con polling
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=storage)

# Estados simples para test
class TestForm(StatesGroup):
    waiting_name = State()

# Handlers de prueba
@dp.message_handler(Command("start"), state="*")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.finish()
    logger.info(f"START command from {message.from_user.id}")
    await message.answer("🎯 BOT FUNCIONANDO!\n\nComandos:\n/test - Prueba simple\n/flow - Prueba con estados")

@dp.message_handler(Command("test"), state="*")
async def cmd_test(message: types.Message, state: FSMContext):
    await state.finish()
    logger.info(f"TEST command from {message.from_user.id}")
    await message.answer("✅ ¡El bot responde correctamente!\n\nTelegram ✓\nPython ✓\nAiogram ✓")

@dp.message_handler(Command("flow"), state="*")
async def cmd_flow(message: types.Message, state: FSMContext):
    await state.finish()
    logger.info(f"FLOW command from {message.from_user.id}")
    await message.answer("📝 Dime tu nombre:")
    await TestForm.waiting_name.set()

@dp.message_handler(state=TestForm.waiting_name, content_types=types.ContentTypes.TEXT)
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    logger.info(f"Name received: {name} from {message.from_user.id}")
    await state.finish()
    await message.answer(f"👋 ¡Hola {name}! El flujo de estados funciona correctamente.")

@dp.message_handler()
async def echo(message: types.Message):
    logger.info(f"Echo: {message.text} from {message.from_user.id}")
    await message.answer(f"🔄 Recibido: {message.text}\n\nUsa /start para ver comandos")

# Función principal
async def on_startup(dp):
    logger.info("🚀 Bot iniciado con POLLING")
    me = await bot.get_me()
    logger.info(f"Bot info: @{me.username} (ID: {me.id})")

async def on_shutdown(dp):
    logger.info("🛑 Bot detenido")

if __name__ == '__main__':
    # Ejecutar con polling (no webhook)
    executor.start_polling(
        dp, 
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown
    )
