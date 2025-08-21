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

# CÓDIGO SECRETO DEL PATRÓN
CODIGO_PATRON = "GSR89ROJAS"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ CONTROL DE TEMPORIZADORES ------------
timers_activos = {}  # {user_id: {"folio": "xxx", "task": task_object}}

# ------------ FOLIO PERSISTENTE (DESDE 891) ------------
def obtener_ultimo_folio():
    """Obtiene el último folio de la base de datos para continuar secuencia"""
    try:
        result = supabase.table("folios_registrados")\
            .select("folio")\
            .eq("entidad", "cdmx")\
            .order("id", desc=True)\
            .limit(1)\
            .execute()
        
        if result.data:
            ultimo_folio = result.data[0]["folio"]
            if ultimo_folio.startswith("891"):
                numero = int(ultimo_folio[3:])
                return numero + 1
        return 1
    except:
        return 1

def generar_folio_secuencial():
    """Genera folio 891 + secuencial infinito"""
    siguiente_numero = obtener_ultimo_folio()
    return f"891{siguiente_numero}"

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    aceptacion = State()
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# ------------ FUNCIONES DE TEMPORIZADOR ------------
async def enviar_recordatorio(user_id: int, folio: str, minutos_restantes: int):
    """Envía recordatorio de tiempo restante"""
    try:
        await bot.send_message(
            user_id,
            f"⏰ **RECORDATORIO AUTOMÁTICO**\n\n"
            f"🚨 **Te quedan {minutos_restantes} minutos para realizar el pago del folio {folio}**\n\n"
            f"💰 **OPCIONES DE PAGO:**\n\n"
            f"🏦 **TRANSFERENCIA BANCARIA**\n"
            f"📍 BANCO AZTECA\n"
            f"👤 LIZBETH LAZCANO MOSCO\n"
            f"🔢 127180013037579543\n\n"
            f"⚠️ **SOLO CAJA OXXO** ⚠️ 👇\n"
            f"🔢 2242 1701 8038 5581\n"
            f"💳 DEPOSITO EN CAJA OXXO TARJETA SPIN\n"
            f"👤 LIZBETH LAZCANO MOSCO\n\n"
            f"📸 **Envía tu comprobante después del pago**",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error enviando recordatorio: {e}")

async def eliminar_folio_vencido(user_id: int, folio: str):
    """Elimina folio de la base de datos al vencerse el tiempo"""
    try:
        # Eliminar de Supabase
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        
        # Enviar mensaje de eliminación
        await bot.send_message(
            user_id,
            f"🚫 **TIEMPO AGOTADO - FOLIO ELIMINADO** 🚫\n\n"
            f"❌ **El folio {folio} ha sido dado de baja automáticamente**\n\n"
            f"🔴 **Tu número de serie ha sido vetado del sistema**\n\n"
            f"⛔ **No podrás realizar más trámites con este vehículo**\n\n"
            f"📍 **El folio ya no aparecerá en consultas oficiales**\n\n"
            f"🔄 **Para generar un nuevo permiso usa /permiso**",
            parse_mode="Markdown"
        )
        
        # Limpiar timer del diccionario
        if user_id in timers_activos:
            del timers_activos[user_id]
            
    except Exception as e:
        print(f"Error eliminando folio vencido: {e}")

async def iniciar_temporizador(user_id: int, folio: str):
    """Inicia el temporizador de 2 horas con recordatorios cada 30 min"""
    
    async def temporizador():
        try:
            # Recordatorio a los 30 minutos (quedan 90)
            await asyncio.sleep(30 * 60)
            if user_id in timers_activos:
                await enviar_recordatorio(user_id, folio, 90)
            
            # Recordatorio a los 60 minutos (quedan 60)
            await asyncio.sleep(30 * 60)
            if user_id in timers_activos:
                await enviar_recordatorio(user_id, folio, 60)
            
            # Recordatorio a los 90 minutos (quedan 30)
            await asyncio.sleep(30 * 60)
            if user_id in timers_activos:
                await enviar_recordatorio(user_id, folio, 30)
            
            # Último recordatorio a los 110 minutos (quedan 10)
            await asyncio.sleep(20 * 60)
            if user_id in timers_activos:
                await enviar_recordatorio(user_id, folio, 10)
            
            # Eliminación final a las 2 horas
            await asyncio.sleep(10 * 60)
            if user_id in timers_activos:
                await eliminar_folio_vencido(user_id, folio)
                
        except asyncio.CancelledError:
            print(f"Timer cancelado para user {user_id}, folio {folio}")
        except Exception as e:
            print(f"Error en temporizador: {e}")
    
    # Crear y guardar la tarea
    task = asyncio.create_task(temporizador())
    timers_activos[user_id] = {"folio": folio, "task": task}

def cancelar_temporizador(user_id: int):
    """Cancela el temporizador activo para un usuario"""
    if user_id in timers_activos:
        timers_activos[user_id]["task"].cancel()
        del timers_activos[user_id]

# ------------ PDF ------------
def generar_pdf_principal(datos: dict) -> str:
    doc = fitz.open(PLANTILLA_PDF)
    page = doc[0]

    page.insert_text((87, 130), datos["folio"], fontsize=14, color=(1, 0, 0))
    page.insert_text((130, 145), datos["fecha"], fontsize=12, color=(0, 0, 0))
    page.insert_text((87, 290), datos["marca"].upper(), fontsize=11, color=(0, 0, 0))
    page.insert_text((375, 290), datos["serie"].upper(), fontsize=11, color=(0, 0, 0))
    page.insert_text((87, 307), datos["linea"].upper(), fontsize=11, color=(0, 0, 0))
    page.insert_text((375, 307), datos["motor"].upper(), fontsize=11, color=(0, 0, 0))
    page.insert_text((87, 323), datos["anio"], fontsize=11, color=(0, 0, 0))
    page.insert_text((375, 323), datos["vigencia"], fontsize=11, color=(0, 0, 0))
    page.insert_text((375, 340), datos["nombre"].upper(), fontsize=10, color=(0, 0, 0))

    filename = f"{OUTPUT_DIR}/{datos['folio']}_principal.pdf"
    doc.save(filename)
    return filename

def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    page.insert_text((135.02, 193.88), serie.upper(), fontsize=6)
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
    datos["folio"] = generar_folio_secuencial()
    datos["user_id"] = message.from_user.id

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
        await message.answer("🔧 **PASO 1: Generando PDF principal...**")
        
        # Generar PDFs
        p1 = generar_pdf_principal(datos)
        await message.answer("🔧 **PASO 2: Generando PDF comprobante...**")
        
        p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])
        await message.answer("🔧 **PASO 3: Guardando en base de datos...**")

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
            "PENDIENTE_PAGO": "PENDIENTE_PAGO"
            # Quité "user_id" porque no existe esa columna
        }).execute()
        
        await message.answer("🔧 **PASO 4: Verificando archivos...**")
        
        # Verificar que los archivos existen
        if not os.path.exists(p1):
            await message.answer(f"❌ Error: No se generó {p1}")
            return
        if not os.path.exists(p2):
            await message.answer(f"❌ Error: No se generó {p2}")
            return
            
        await message.answer("🔧 **PASO 5: Enviando documentos...**")

        # Enviar documentos
        try:
            await message.answer_document(
                FSInputFile(p1),
                caption=f"📄 PERMISO PRINCIPAL OFICIAL\n🆔 Folio: {datos['folio']}\n🏛️ CDMX Digital"
            )
        except Exception as e:
            await message.answer(f"❌ Error enviando PDF principal: {e}")
            
        try:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"📋 COMPROBANTE DE TRÁMITE\n🔢 Serie: {datos['serie']}\n✅ EL BUENO"
            )
        except Exception as e:
            await message.answer(f"❌ Error enviando PDF comprobante: {e}")

        # Mensaje de pago con info bancaria
        await message.answer(
            f"🎉 **DOCUMENTOS GENERADOS EXITOSAMENTE** 🎉\n\n"
            f"📋 **RESUMEN DEL TRÁMITE:**\n"
            f"🆔 **Folio:** `{datos['folio']}`\n"
            f"🚗 **Vehículo:** {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"🔢 **Serie:** {datos['serie']}\n"
            f"👤 **Solicitante:** {datos['nombre']}\n\n"
            f"💰 **INFORMACIÓN DE PAGO:**\n\n"
            f"🏦 **TRANSFERENCIA BANCARIA**\n"
            f"📍 BANCO AZTECA\n"
            f"👤 LIZBETH LAZCANO MOSCO\n"
            f"🔢 127180013037579543\n\n"
            f"⚠️ **SOLO CAJA OXXO** ⚠️ 👇\n"
            f"🔢 2242 1701 8038 5581\n"
            f"💳 DEPOSITO EN CAJA OXXO TARJETA SPIN\n"
            f"👤 LIZBETH LAZCANO MOSCO\n\n"
            f"⏰ **TIENES 2 HORAS PARA PAGAR**\n\n"
            f"📸 **Envía tu comprobante de pago para validación**\n\n"
            f"🚨 **RECORDATORIO:**\n"
            f"🔴 **Sin pago en 2 horas = Folio eliminado automáticamente**\n"
            f"🔴 **Serie vetada permanentemente del sistema**",
            parse_mode="Markdown"
        )

        # INICIAR TEMPORIZADOR DE 2 HORAS (DESACTIVADO TEMPORALMENTE)
        # await iniciar_temporizador(message.from_user.id, datos["folio"])
        print(f"Timer iniciado para user {message.from_user.id}, folio {datos['folio']}")
        
    except Exception as e:
        await message.answer(
            f"🚫 **ERROR EN EL SISTEMA**\n\n"
            f"❌ **Fallo al procesar:** {str(e)}\n\n"
            f"🔄 **Intenta nuevamente con /permiso**",
            parse_mode="Markdown"
        )
        print(f"Error completo: {e}")
    finally:
        await state.clear()

# ------------ HANDLERS DE PAGO ------------
@dp.message(lambda message: message.text and message.text.strip() == CODIGO_PATRON)
async def codigo_patron_recibido(message: types.Message):
    """Maneja el código secreto del patrón"""
    user_id = message.from_user.id
    
    if user_id in timers_activos:
        folio = timers_activos[user_id]["folio"]
        cancelar_temporizador(user_id)
        
        # Actualizar estado en base de datos
        supabase.table("folios_registrados")\
            .update({"PENDIENTE_PAGO": "PAGADO_PATRON"})\
            .eq("folio", folio)\
            .execute()
        
        await message.answer(
            f"👑 **CÓDIGO DE PATRÓN CONFIRMADO** 👑\n\n"
            f"✅ **Folio {folio} marcado como PAGADO**\n\n"
            f"⏹️ **Temporizador detenido**\n\n"
            f"🔒 **Permiso asegurado permanentemente**",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "👑 **CÓDIGO DE PATRÓN VÁLIDO**\n\n"
            "❌ **No hay temporizador activo**\n\n"
            "📝 **Genera un permiso primero con /permiso**",
            parse_mode="Markdown"
        )

@dp.message(lambda message: message.photo or message.document)
async def comprobante_recibido(message: types.Message):
    """Maneja las imágenes/comprobantes de pago"""
    user_id = message.from_user.id
    
    if user_id in timers_activos:
        folio = timers_activos[user_id]["folio"]
        cancelar_temporizador(user_id)
        
        # Actualizar estado en base de datos
        supabase.table("folios_registrados")\
            .update({"PENDIENTE_PAGO": "COMPROBANTE_RECIBIDO"})\
            .eq("folio", folio)\
            .execute()
        
        await message.answer(
            f"📸 **COMPROBANTE RECIBIDO** 📸\n\n"
            f"✅ **Folio:** {folio}\n\n"
            f"⏳ **El pago será validado por el sistema**\n\n"
            f"🔒 **Tu permiso está asegurado**\n\n"
            f"✅ **Excelente día**",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "📸 **Imagen recibida**\n\n"
            "❌ **No hay trámite activo**\n\n"
            "📝 **Genera un permiso primero con /permiso**",
            parse_mode="Markdown"
        )

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

app = FastAPI(lifespan=lifespan, title="Sistema CDMX con Pagos", version="3.0.0")

@app.get("/")
async def health():
    return {
        "status": "SISTEMA OPERATIVO",
        "entidad": "CDMX Digital", 
        "version": "3.0.0",
        "folios": "891+ secuencial infinito",
        "temporizador": "2 horas con auto-eliminación"
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

@app.get("/timers")
async def timers_status():
    """Endpoint para ver timers activos (debug)"""
    return {
        "timers_activos": len(timers_activos),
        "folios_en_tiempo": [info["folio"] for info in timers_activos.values()]
                                          }
