from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import os
import fitz  # PyMuPDF

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "cdmxdigital2025ppp.pdf"
PLANTILLA_BUENO = "elbueno.pdf"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ FOLIO ------------
folio_counter = {"count": 1}
def nuevo_folio() -> str:
    folio = f"234{folio_counter['count']}"
    folio_counter["count"] += 2
    return folio

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# ------------ PDF ------------
def generar_pdf_principal(datos: dict) -> str:
    doc = fitz.open(PLANTILLA_PDF)
    page = doc[0]

    page.insert_text((87, 130), datos["folio"], fontsize=14, color=(1, 0, 0))         # FOLIO
    page.insert_text((130, 145), datos["fecha"], fontsize=12, color=(0, 0, 0))        # FECHA
    page.insert_text((87, 290), datos["marca"], fontsize=11, color=(0, 0, 0))         # MARCA
    page.insert_text((375, 290), datos["serie"], fontsize=11, color=(0, 0, 0))        # SERIE
    page.insert_text((87, 307), datos["linea"], fontsize=11, color=(0, 0, 0))         # LINEA
    page.insert_text((375, 307), datos["motor"], fontsize=11, color=(0, 0, 0))        # MOTOR
    page.insert_text((87, 323), datos["anio"], fontsize=11, color=(0, 0, 0))          # AÑO
    page.insert_text((375, 323), datos["vigencia"], fontsize=11, color=(0, 0, 0))     # VIGENCIA
    page.insert_text((375, 340), datos["nombre"], fontsize=11, color=(0, 0, 0))       # NOMBRE

    filename = f"{OUTPUT_DIR}/{datos['folio']}_principal.pdf"
    doc.save(filename)
    return filename

def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    page.insert_text((135.02, 193.88), serie, fontsize=6)
    page.insert_text((190, 324), fecha.strftime("%d/%m/%Y"), fontsize=6)
    filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf"
    doc.save(filename)
    return filename

# ------------ HANDLERS CON DIÁLOGOS CHINGONES ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🔥 ¡Órale! Aquí está el Sistema Digital de Permisos CDMX.\n"
        "Somos eficientes, directos y no andamos con mamadas.\n\n"
        "Usa /permiso para tramitar tu documento. Punto."
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer(
        "🚗 Vamos a trabajar en serio.\n"
        "Escribe la MARCA del vehículo y que sea claro:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA: {marca} - Registrado.\n\n"
        "Ahora la LÍNEA del vehículo. Sin rollos:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA: {linea} - Anotado.\n\n"
        "El AÑO del vehículo (números, no letras):"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ Ahí no, jefe. El año debe ser de 4 dígitos.\n"
            "Ejemplo: 2020, 2015, etc. Inténtelo de nuevo:"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ AÑO: {anio} - Confirmado.\n\n"
        "NÚMERO DE SERIE del vehículo:"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer(
            "⚠️ Ese número de serie está muy corto.\n"
            "Revise bien y escriba el número completo:"
        )
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ SERIE: {serie} - En el sistema.\n\n"
        "NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR: {motor} - Capturado.\n\n"
        "Por último, el NOMBRE COMPLETO del solicitante:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["folio"] = nuevo_folio()

    # -------- FECHAS FORMATOS --------
    hoy = datetime.now()
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    fecha_ven = hoy + timedelta(days=30)
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
    # ---------------------------------

    await message.answer(
        f"🔄 PROCESANDO PERMISO...\n"
        f"Folio: {datos['folio']}\n"
        f"Titular: {nombre}\n\n"
        "El sistema está trabajando. Espere..."
    )

    try:
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])

        await message.answer_document(
            FSInputFile(p1),
            caption=f"📋 PERMISO PRINCIPAL\nFolio: {datos['folio']}\n⚡ Sistema CDMX Digital"
        )
        await message.answer_document(
            FSInputFile(p2),
            caption=f"🏆 DOCUMENTO VERIFICADO\nSerie: {datos['serie']}\n✅ Validación oficial"
        )

        # Guardar en base de datos
        supabase.table("folios_registrados").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "nombre": datos["nombre"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": "cdmx",
        }).execute()

        await message.answer(
            f"🎯 MISIÓN CUMPLIDA\n\n"
            f"Permiso generado con folio {datos['folio']}\n"
            f"Vigencia: 30 días\n"
            f"Estado: ACTIVO\n\n"
            "Sus documentos están listos. El sistema no falla.\n"
            "Para otro trámite, use /permiso nuevamente."
        )
        
    except Exception as e:
        await message.answer(
            f"💥 ERROR EN EL SISTEMA\n\n"
            f"Algo se jodió: {str(e)}\n\n"
            "Intente nuevamente con /permiso\n"
            "Si persiste, contacte al administrador."
        )
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    respuestas_random = [
        "🤖 No entiendo esa orden, soldado. Use /permiso para tramitar.",
        "⚡ Sistema no reconoce esa instrucción. /permiso es lo que necesita.",
        "🎯 Directo al grano: /permiso para iniciar su trámite.",
        "🔥 Aquí no hay tiempo que perder. /permiso y listo.",
    ]
    import random
    await message.answer(random.choice(respuestas_random))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
        _keep_task = asyncio.create_task(keep_alive())
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}
