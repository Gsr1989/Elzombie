import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Update

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL  = os.environ.get("BASE_URL", "")  # la pones tras el primer deploy

bot = Bot(BOT_TOKEN)
dp  = Dispatcher()
app = FastAPI()

@dp.message(Command("start"))
async def start(msg):
    await msg.answer("Hola, jefe. Ya estoy en la nube (Render) 🤖")

@dp.message()
async def echo(msg):
    await msg.answer(f"Me dijiste: {msg.text}")

@app.on_event("startup")
async def setup_webhook():
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
def health():
    return {"ok": True}
