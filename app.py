from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Command
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio, os, time, fitz

--- CONFIG ---

BOT_TOKEN = "7558047306:AAGza9rjVbRz2MwUN9c1a5LJ3mvM99qfHFM" SUPABASE_URL = "https://xsagwqepoljfsogusubw.supabase.co" SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws" BASE_URL = os.getenv("BASE_URL", "").rstrip("/") OUTPUT_DIR = "documentos" PLANTILLA_PDF = "cdmxdigital2025ppp.pdf" PLANTILLA_BUENO = "elbueno.pdf"

os.makedirs(OUTPUT_DIR, exist_ok=True)

--- SUPABASE ---

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

--- BOT ---

bot = Bot(token=BOT_TOKEN) storage = MemoryStorage() dp = Dispatcher(bot, storage=storage)

--- FOLIO ---

folio_counter = {"count": 1}

def nuevo_folio(): folio = f"01{folio_counter['count']}" folio_counter['count'] += 1 return folio

--- FSM STATES ---

class PermisoForm(StatesGroup): marca = State() linea = State() anio = State() serie = State() motor = State() nombre = State()

--- PDF GENERADORES ---

def generar_pdf_principal(datos): doc = fitz.open(PLANTILLA_PDF) page = doc[0] page.insert_text((100, 100), f"FOLIO: {datos['folio']}", fontsize=12) page.insert_text((100, 120), f"MARCA: {datos['marca']}", fontsize=12) page.insert_text((100, 140), f"LÍNEA: {datos['linea']}", fontsize=12) page.insert_text((100, 160), f"AÑO: {datos['anio']}", fontsize=12) page.insert_text((100, 180), f"SERIE: {datos['serie']}", fontsize=12) page.insert_text((100, 200), f"MOTOR: {datos['motor']}", fontsize=12) page.insert_text((100, 220), f"NOMBRE: {datos['nombre']}", fontsize=12) filename = f"{OUTPUT_DIR}/{datos['folio']}_principal.pdf" doc.save(filename) return filename

def generar_pdf_bueno(serie, fecha, folio): doc = fitz.open(PLANTILLA_BUENO) page = doc[0] page.insert_text((135.02, 193.88), serie, fontsize=6) page.insert_text((190, 324), fecha.strftime('%d/%m/%Y'), fontsize=6) filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf" doc.save(filename) return filename

--- HANDLERS ---

@dp.message_handler(Command("start"), state="*") async def start_cmd(m: types.Message, state: FSMContext): await state.finish() await m.answer("\U0001F44B Bienvenido. Usa /permiso para iniciar")

@dp.message_handler(Command("permiso"), state="*") async def permiso_cmd(m: types.Message): await m.answer("Marca del vehículo:") await PermisoForm.marca.set()

@dp.message_handler(state=PermisoForm.marca) async def get_marca(m: types.Message, state: FSMContext): await state.update_data(marca=m.text.strip()) await m.answer("Línea:") await PermisoForm.linea.set()

@dp.message_handler(state=PermisoForm.linea) async def get_linea(m: types.Message, state: FSMContext): await state.update_data(linea=m.text.strip()) await m.answer("Año:") await PermisoForm.anio.set()

@dp.message_handler(state=PermisoForm.anio) async def get_anio(m: types.Message, state: FSMContext): await state.update_data(anio=m.text.strip()) await m.answer("Serie:") await PermisoForm.serie.set()

@dp.message_handler(state=PermisoForm.serie) async def get_serie(m: types.Message, state: FSMContext): await state.update_data(serie=m.text.strip()) await m.answer("Motor:") await PermisoForm.motor.set()

@dp.message_handler(state=PermisoForm.motor) async def get_motor(m: types.Message, state: FSMContext): await state.update_data(motor=m.text.strip()) await m.answer("Nombre del solicitante:") await PermisoForm.nombre.set()

@dp.message_handler(state=PermisoForm.nombre) async def get_nombre(m: types.Message, state: FSMContext): datos = await state.get_data() datos["nombre"] = m.text.strip() datos["folio"] = nuevo_folio() try: # GENERAR PDFS path1 = generar_pdf_principal(datos) path2 = generar_pdf_bueno(datos['serie'], datetime.now(), datos['folio'])

# MANDAR AL USUARIO
    await m.answer_document(open(path1, "rb"), caption=f"\U0001F4C4 Principal - Folio: {datos['folio']}")
    await m.answer_document(open(path2, "rb"), caption=f"\u2705 EL BUENO - Serie: {datos['serie']}")

    # GUARDAR EN SUPABASE
    fecha_exp = datetime.now().date()
    fecha_ven = fecha_exp + timedelta(days=30)
    supabase.table("folios_registrados").insert({
        "folio": datos['folio'],
        "marca": datos['marca'],
        "linea": datos['linea'],
        "anio": datos['anio'],
        "numero_serie": datos['serie'],
        "numero_motor": datos['motor'],
        "nombre": datos['nombre'],
        "fecha_expedicion": fecha_exp.isoformat(),
        "fecha_vencimiento": fecha_ven.isoformat(),
        "entidad": "cdmx"
    }).execute()

    await m.answer("\u2705 Permiso guardado y registrado correctamente.")
except Exception as e:
    await m.answer(f"\u274C Error al generar: {e}")
await state.finish()

@dp.message_handler() async def fallback(m: types.Message): await m.answer("Usa /permiso para iniciar.")

--- FASTAPI ---

_keep_task = None

async def keep_alive(): while True: await asyncio.sleep(600)

@asynccontextmanager async def lifespan(app: FastAPI): global _keep_task await bot.delete_webhook(drop_pending_updates=True) if BASE_URL: await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"]) _keep_task = asyncio.create_task(keep_alive()) yield if _keep_task: _keep_task.cancel() with suppress(asyncio.CancelledError): await _keep_task await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook") async def telegram_webhook(request: Request): data = await request.json() update = types.Update(**data) Bot.set_current(bot) Dispatcher.set_current(dp) asyncio.create_task(dp.process_update(update)) return {"ok": True}

