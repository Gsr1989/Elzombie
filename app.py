from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
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

# ------------ FOLIO PERSISTENTE (DESDE 88102) ------------
def obtener_ultimo_folio():
    """Obtiene el último folio de la base de datos para continuar secuencia"""
    try:
        # NO usar "id" porque no existe esa columna, usar "created_at" o el nombre real
        result = supabase.table("folios_registrados")\
            .select("folio")\
            .eq("entidad", "cdmx")\
            .order("folio", desc=True)\
            .limit(1)\
            .execute()
        
        if result.data:
            ultimo_folio = result.data[0]["folio"]
            print(f"Último folio encontrado: {ultimo_folio}")
            
            # Si empieza con "881", extraer el número después
            if ultimo_folio.startswith("881"):
                numero = int(ultimo_folio[3:])  # Quitar "881" 
                siguiente = numero + 1
                print(f"Número extraído: {numero}, siguiente será: {siguiente}")
                return siguiente
            
            # Si no empieza con 881, empezar desde 200 (para evitar conflictos)
            print("Folio no empieza con 881, empezando desde 200")
            return 200
        
        # Si no hay folios, empezar desde 200
        print("No hay folios, empezando desde 200")
        return 200
    except Exception as e:
        print(f"Error obteniendo último folio: {e}")
        return 200

def generar_folio_secuencial():
    """Genera folio 881 + secuencial infinito desde 200"""
    siguiente_numero = obtener_ultimo_folio()
    nuevo_folio = f"881{siguiente_numero}"
    print(f"Generando nuevo folio: {nuevo_folio}")
    return nuevo_folio

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    aceptacion = State()
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

    # TODOS LOS DATOS EN MAYÚSCULAS EN EL PDF
    page.insert_text((87, 130), datos["folio"], fontsize=14, color=(1, 0, 0))
    page.insert_text((130, 145), datos["fecha"], fontsize=12, color=(0, 0, 0))
    page.insert_text((87, 290), datos["marca"].upper(), fontsize=11, color=(0, 0, 0))         # MAYÚS
    page.insert_text((375, 290), datos["serie"].upper(), fontsize=11, color=(0, 0, 0))        # MAYÚS
    page.insert_text((87, 307), datos["linea"].upper(), fontsize=11, color=(0, 0, 0))         # MAYÚS
    page.insert_text((375, 307), datos["motor"].upper(), fontsize=11, color=(0, 0, 0))        # MAYÚS
    page.insert_text((87, 323), datos["anio"], fontsize=11, color=(0, 0, 0))
    page.insert_text((375, 323), datos["vigencia"], fontsize=11, color=(0, 0, 0))
    page.insert_text((375, 340), datos["nombre"].upper(), fontsize=10, color=(0, 0, 0))       # MAYÚS

    filename = f"{OUTPUT_DIR}/{datos['folio']}_principal.pdf"
    doc.save(filename)
    return filename

def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    page.insert_text((135.02, 193.88), serie.upper(), fontsize=6)  # SERIE EN MAYÚS
    page.insert_text((190, 324), fecha.strftime("%d/%m/%Y"), fontsize=6)
    filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf"
    doc.save(filename)
    return filename

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🚨 **SISTEMA AUTOMATIZADO CDMX** 🚨\n\n"
        "⚡ **GENERACIÓN DE PERMISOS OFICIALES**\n\n"
        "🔹 Utiliza /permiso para iniciar proceso\n"
        "🔹 **ADVERTENCIA:** Una vez iniciado el proceso, debes completarlo\n"
        "🔹 **NO interrumpas** el procedimiento\n\n"
        "📍 **Estado:** SISTEMA OPERATIVO\n"
        "📍 **Entidad:** Ciudad de México Digital",
        parse_mode="Markdown"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Crear botones de aceptación
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ ACEPTO Y CONTINÚO", callback_data="acepto_condiciones"),
            InlineKeyboardButton(text="❌ NO ACEPTO", callback_data="rechazo_condiciones")
        ]
    ])
    
    await message.answer(
        "🚨 **SISTEMA AUTOMATIZADO PARA LA GENERACIÓN DE PERMISOS** 🚨\n\n"
        "⚠️ **ALERTA IMPORTANTE - LEE CUIDADOSAMENTE**\n\n"
        "📋 **CONDICIONES DEL SISTEMA:**\n\n"
        "🔴 **EL SISTEMA TE ENVIARÁ EL PERMISO YA REGISTRADO EN BASE DE DATOS OFICIAL**\n\n"
        "⏰ **TIENES EXACTAMENTE 2 HORAS PARA GENERAR EL PAGO**\n\n"
        "🚫 **DE LO CONTRARIO EL SISTEMA LO DARÁ DE BAJA AUTOMÁTICAMENTE DE LA BASE DE DATOS DE CDMX**\n\n"
        "⛔ **EL NÚMERO DE NIV/SERIE QUEDARÁ VETADO PERMANENTEMENTE**\n\n"
        "🔒 **NO SE PODRÁN REALIZAR MÁS TRÁMITES NI FOLIOS CON ESE VEHÍCULO**\n\n"
        "💰 **UNA VEZ REALIZADO EL PAGO, ENVÍA LA CAPTURA PARA VALIDACIÓN**\n\n"
        "⚡ **¿ESTÁS DE ACUERDO Y DESEAS CONTINUAR?**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.callback_query(lambda c: c.data == "acepto_condiciones")
async def acepto_condiciones(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "✅ **CONDICIONES ACEPTADAS**\n\n"
        "🔄 **INICIANDO PROCESO AUTOMATIZADO...**\n\n"
        "📝 **PROPORCIONA LA INFORMACIÓN SOLICITADA:**",
        parse_mode="Markdown"
    )
    await callback.message.answer(
        "🚗 **PASO 1/6:** Ingresa la **MARCA** del vehículo:\n\n"
        "⚠️ **IMPORTANTE:** Proporciona información exacta",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.marca)

@dp.callback_query(lambda c: c.data == "rechazo_condiciones")
async def rechazo_condiciones(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "❌ **PROCESO CANCELADO**\n\n"
        "🚫 **NO SE ACEPTARON LAS CONDICIONES**\n\n"
        "📍 **El sistema se ha desconectado**\n\n"
        "🔄 **Puedes intentar nuevamente con /permiso cuando estés listo**",
        parse_mode="Markdown"
    )

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ **MARCA REGISTRADA:** {marca}\n\n"
        "📱 **PASO 2/6:** Ingresa la **LÍNEA/MODELO** del vehículo:",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ **LÍNEA REGISTRADA:** {linea}\n\n"
        "📅 **PASO 3/6:** Ingresa el **AÑO** del vehículo (4 dígitos):",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "🚫 **ERROR EN FORMATO**\n\n"
            "❌ **Año inválido detectado**\n\n"
            "📋 **Proporciona un año válido (4 dígitos)**\n"
            "📝 **Ejemplo:** 2020, 2018, 2023",
            parse_mode="Markdown"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ **AÑO REGISTRADO:** {anio}\n\n"
        "🔢 **PASO 4/6:** Ingresa el **NÚMERO DE SERIE (NIV)**:",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ **SERIE REGISTRADA:** {serie}\n\n"
        "🔧 **PASO 5/6:** Ingresa el **NÚMERO DE MOTOR**:",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ **MOTOR REGISTRADO:** {motor}\n\n"
        "👤 **PASO 6/6:** Ingresa el **NOMBRE COMPLETO** del solicitante:",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = message.text.strip().upper()
    
    # Generar folio secuencial desde 8811
    datos["folio"] = generar_folio_secuencial()

    # Fechas
    hoy = datetime.now()
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    fecha_ven = hoy + timedelta(days=30)
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")

    await message.answer(
        "⚡ **PROCESANDO INFORMACIÓN...**\n\n"
        "🔄 **CONECTANDO CON BASE DE DATOS CDMX...**\n\n"
        "📋 **GENERANDO DOCUMENTOS OFICIALES...**",
        parse_mode="Markdown"
    )

    try:
        # Generar PDFs
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])

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

        # Enviar documentos
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📄 **PERMISO PRINCIPAL OFICIAL**\n🆔 **Folio:** {datos['folio']}\n🏛️ **CDMX Digital**"
        )
        
        await message.answer_document(
            FSInputFile(p2),
            caption=f"📋 **COMPROBANTE DE TRÁMITE**\n🔢 **Serie:** {datos['serie']}\n✅ **EL BUENO**"
        )

        # Mensaje final amenazante pero "profesional"
        tiempo_limite = (hoy + timedelta(hours=2)).strftime("%H:%M hrs")
        
        await message.answer(
            f"🎉 **DOCUMENTOS GENERADOS EXITOSAMENTE** 🎉\n\n"
            f"📋 **RESUMEN DEL TRÁMITE:**\n"
            f"🆔 **Folio:** `{datos['folio']}`\n"
            f"🚗 **Vehículo:** {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"🔢 **Serie:** {datos['serie']}\n"
            f"👤 **Solicitante:** {datos['nombre']}\n\n"
            f"⏰ **TIEMPO LÍMITE DE PAGO:** {tiempo_limite}\n\n"
            f"🚨 **INSTRUCCIONES IMPORTANTES:**\n\n"
            f"💰 **1.** Realiza el pago correspondiente\n"
            f"📸 **2.** Envía la captura del comprobante de pago\n"
            f"✅ **3.** Espera la validación del sistema\n\n"
            f"⚠️ **RECORDATORIO:**\n"
            f"🔴 **Tienes 2 horas para completar el pago**\n"
            f"🔴 **De lo contrario el folio {datos['folio']} será dado de baja**\n"
            f"🔴 **La serie {datos['serie']} quedará vetada permanentemente**\n\n"
            f"📞 **El sistema está monitoreando tu trámite**",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await message.answer(
            f"🚫 **ERROR EN EL SISTEMA**\n\n"
            f"❌ **Fallo al procesar:** {str(e)}\n\n"
            f"🔄 **Intenta nuevamente con /permiso**",
            parse_mode="Markdown"
        )
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    await message.answer(
        "🚨 **SISTEMA AUTOMATIZADO CDMX** 🚨\n\n"
        "❌ **COMANDO NO RECONOCIDO**\n\n"
        "📋 **Comandos disponibles:**\n"
        "🔹 /permiso - Generar permiso oficial\n"
        "🔹 /start - Información del sistema\n\n"
        "⚡ **Para iniciar el proceso utiliza:** /permiso",
        parse_mode="Markdown"
    )

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
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
        print(f"🚀 Sistema iniciado - Webhook: {webhook_url}")
        _keep_task = asyncio.create_task(keep_alive())
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema CDMX Automatizado", version="2.0.0")

@app.get("/")
async def health():
    return {
        "status": "SISTEMA OPERATIVO",
        "entidad": "CDMX Digital", 
        "version": "2.0.0",
        "folios": "8811+ secuencial infinito"
    }

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/status")
async def bot_status():
    try:
        bot_info = await bot.get_me()
        return {
            "sistema_activo": True,
            "bot_username": bot_info.username,
            "entidad": "CDMX",
            "folios_desde": "8811"
        }
    except Exception as e:
        return {"sistema_activo": False, "error": str(e)}
