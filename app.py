import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from fastapi import FastAPI, Request
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# ===== CONFIGURACIÓN =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "TU_TOKEN_AQUI")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== INICIALIZAR BOT =====
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

# Fix de contexto
Bot.set_current(bot)
Dispatcher.set_current(dp)

# ===== DEFINICIÓN DE ESTADOS =====
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# ===== FASTAPI =====
app = FastAPI()

# ===== MENÚ START =====
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
