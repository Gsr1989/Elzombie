from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
import asyncio
import qrcode
from io import BytesIO
import random
from PIL import Image

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "cdmxdigital2025ppp.pdf"
PLANTILLA_BUENO = "elbueno.pdf"

# Precio del permiso
PRECIO_PERMISO = EL_MISMO_DE_SIEMPRE 

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT ------------
timers_activos = {}  # {user_id: {"task": task, "folio": folio, "start_time": datetime}}

async def eliminar_folio_automatico(user_id: int, folio: str):
    """Elimina folio automáticamente después del tiempo límite"""
    try:
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Notificar al usuario
        await bot.send_message(
            user_id,
            f"⏰ TIEMPO AGOTADO\n\n"
            f"El folio {folio} ha sido eliminado del sistema por falta de pago.\n\n"
            f"Para tramitar un nuevo permiso utilize /permiso"
        )
        
        # Limpiar timer
        if user_id in timers_activos:
            del timers_activos[user_id]
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, minutos_restantes: int):
    """Envía recordatorios de pago"""
    try:
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO CDMX\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: El costo es el mismo de siempre\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 2 horas con recordatorios"""
    async def timer_task():
        start_time = datetime.now()
        
        # Recordatorios cada 30 minutos
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)  # 30 minutos
            
            # Verificar si el timer sigue activo
            if user_id not in timers_activos:
                return  # Timer cancelado (usuario pagó)
                
            minutos_restantes = 120 - minutos
            await enviar_recordatorio(user_id, folio, minutos_restantes)
        
        # Último recordatorio a los 110 minutos (faltan 10)
        await asyncio.sleep(20 * 60)  # 20 minutos más
        if user_id in timers_activos:
            await enviar_recordatorio(user_id, folio, 10)
        
        # Esperar 10 minutos finales
        await asyncio.sleep(10 * 60)
        
        # Si llegamos aquí, se acabó el tiempo
        if user_id in timers_activos:
            await eliminar_folio_automatico(user_id, folio)
    
    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[user_id] = {
        "task": task,
        "folio": folio,
        "start_time": datetime.now()
    }

def cancelar_timer(user_id: int):
    """Cancela el timer cuando el usuario paga"""
    if user_id in timers_activos:
        timers_activos[user_id]["task"].cancel()
        del timers_activos[user_id]

# ------------ FOLIO CDMX CON PREFIJO 822 PROGRESIVO ------------
FOLIO_PREFIJO = "822"
folio_counter = {"siguiente": 1}

def obtener_siguiente_folio():
    """
    Retorna el folio como string con prefijo 822 y número progresivo.
    Ej: 8221, 8223, ..., 822100, etc.
    """
    folio_num = folio_counter["siguiente"]
    folio = f"{FOLIO_PREFIJO}{folio_num}"
    folio_counter["siguiente"] += 2
    return folio

def inicializar_folio_desde_supabase():
    """
    Busca el último folio de CDMX en Supabase y ajusta el contador.
    """
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", "cdmx") \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            ultimo_folio = response.data[0]["folio"]
            if isinstance(ultimo_folio, str) and ultimo_folio.startswith(FOLIO_PREFIJO):
                numero = int(ultimo_folio[len(FOLIO_PREFIJO):])
                folio_counter["siguiente"] = numero + 2
            else:
                folio_counter["siguiente"] = 1  # En caso de valores corruptos
        else:
            folio_counter["siguiente"] = 1  # No hay ningún folio registrado
    except Exception as e:
        print(f"[ERROR] Al inicializar folio CDMX: {e}")
        folio_counter["siguiente"] = 1

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    nombre = State()

# ------------ GENERACIÓN PDF CDMX ------------
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
    doc.close()
    return filename

def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    page.insert_text((135.02, 193.88), serie, fontsize=6)
    page.insert_text((190, 324), fecha.strftime("%d/%m/%Y"), fontsize=6)
    filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf"
    doc.save(filename)
    doc.close()
    return filename

# ------------ HANDLERS CDMX CON DIÁLOGOS ELEGANTES ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ Sistema Digital de Permisos CDMX\n"
        "Servicio oficial automatizado para trámites vehiculares\n\n"
        "💰 Costo del permiso: El costo es el mismo de siempre\n"
        "⏰ Tiempo límite para pago: 2 horas\n"
        "📸 Métodos de pago: Transferencia bancaria y OXXO\n\n"
        "📋 Use /permiso para iniciar su trámite\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Cancelar timer anterior si existe
    cancelar_timer(message.from_user.id)
    
    await message.answer(
        f"🚗 TRÁMITE DE PERMISO CDMX\n\n"
        f"📋 Costo: El costo es el mismo de siempre\n"
        f"⏰ Tiempo para pagar: 2 horas\n"
        f"📱 Concepto de pago: Su folio asignado\n\n"
        f"Al continuar acepta que su folio será eliminado si no paga en el tiempo establecido.\n\n"
        f"Comenzemos con la MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA: {marca}\n\n"
        "Ahora indique la LÍNEA del vehículo:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA: {linea}\n\n"
        "Proporcione el AÑO del vehículo (formato de 4 dígitos):"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ El año debe contener exactamente 4 dígitos.\n"
            "Ejemplo válido: 2020, 2015, 2023\n\n"
            "Por favor, ingrese nuevamente el año:"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ AÑO: {anio}\n\n"
        "Indique el NÚMERO DE SERIE del vehículo:"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer(
            "⚠️ El número de serie parece incompleto.\n"
            "Verifique que haya ingresado todos los caracteres.\n\n"
            "Intente nuevamente:"
        )
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ SERIE: {serie}\n\n"
        "Proporcione el NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR: {motor}\n\n"
        "Finalmente, proporcione el NOMBRE COMPLETO del titular:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["folio"] = obtener_siguiente_folio()

    # -------- FECHAS --------
    hoy = datetime.now()
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    fecha_ven = hoy + timedelta(days=30)
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
    # -------------------------

    await message.answer(
        f"🔄 PROCESANDO PERMISO CDMX...\n\n"
        f"📄 Folio asignado: {datos['folio']}\n"
        f"👤 Titular: {nombre}\n\n"
        "Generando documentos oficiales..."
    )

    try:
        # Generar PDFs
        pdf_principal = generar_pdf_principal(datos)
        pdf_bueno = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])

        # Enviar documentos
        await message.answer_document(
            FSInputFile(pdf_principal),
            caption=f"📋 PERMISO PRINCIPAL CDMX\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🏛️ Documento oficial con validez legal"
        )

        await message.answer_document(
            FSInputFile(pdf_bueno),
            caption=f"📋 DOCUMENTO DE VERIFICACIÓN\n"
                   f"Serie: {datos['serie']}\n"
                   f"🔍 Comprobante adicional de autenticidad"
        )

        # Guardar en base de datos con estado PENDIENTE (SIN CAMPO COLOR)
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
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

        # También en la tabla borradores (compatibilidad, SIN CAMPO COLOR)
        supabase.table("borradores_registros").insert({
            "folio": datos["folio"],
            "entidad": "CDMX",
            "numero_serie": datos["serie"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "numero_motor": datos["motor"],
            "anio": datos["anio"],
            "fecha_expedicion": hoy.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "contribuyente": datos["nombre"],
            "estado": "PENDIENTE",
            "user_id": message.from_user.id
        }).execute()

        # INICIAR TIMER DE PAGO
        await iniciar_timer_pago(message.from_user.id, datos['folio'])

        # Mensaje de instrucciones de pago con info bancaria real
        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"💵 Monto: El costo es el mismo de siempre\n"
            f"⏰ Tiempo límite: 2 horas\n\n"
            
            "🏦 TRANSFERENCIA BANCARIA:\n"
            "• Banco: AZTECA\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            "• Cuenta: 127180013037579543\n"
            "• Concepto: Permiso " + datos['folio'] + "\n\n"
            
            "🏪 PAGO EN OXXO:\n"
            "• Referencia: 2242170180385581\n"
            "• TARJETA SPIN\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            "• Cantidad exacta: El costo de siempre\n\n"
            
            f"📸 IMPORTANTE: Una vez realizado el pago, envíe la fotografía de su comprobante.\n\n"
            f"⚠️ ADVERTENCIA: Si no completa el pago en 2 horas, el folio {datos['folio']} será eliminado automáticamente del sistema."
        )
        
    except Exception as e:
        await message.answer(
            f"❌ ERROR EN EL SISTEMA\n\n"
            f"Se ha presentado un inconveniente técnico: {str(e)}\n\n"
            "Por favor, intente nuevamente con /permiso\n"
            "Si el problema persiste, contacte al soporte técnico."
        )
    finally:
        await state.clear()

# ------------ CÓDIGO SECRETO ADMIN ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    
    # Verificar formato: SERO + número de folio
    if len(texto) > 4:
        folio_admin = texto[4:]  # Quitar "SERO" del inicio
        
        # Buscar si hay un timer activo con ese folio
        user_con_folio = None
        for user_id, timer_info in timers_activos.items():
            if timer_info["folio"] == folio_admin:
                user_con_folio = user_id
                break
        
        if user_con_folio:
            # Cancelar timer
            cancelar_timer(user_con_folio)
            
            # Actualizar estado en base de datos
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
            
            supabase.table("borradores_registros").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
            
            await message.answer(
                f"🔐 CÓDIGO ADMIN EJECUTADO\n\n"
                f"📄 Folio: {folio_admin}\n"
                f"⏰ Timer cancelado exitosamente\n"
                f"✅ Estado actualizado a VALIDADO_ADMIN\n"
                f"👤 Usuario ID: {user_con_folio}"
            )
            
            # Notificar al usuario que su permiso está validado
            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO\n\n"
                    f"📄 Folio: {folio_admin}\n"
                    f"Su permiso ha sido validado por administración.\n"
                    f"El documento está completamente activo para circular.\n\n"
                    f"Gracias por utilizar el Sistema Digital CDMX."
                )
            except Exception as e:
                print(f"Error notificando al usuario {user_con_folio}: {e}")
        else:
            await message.answer(
                f"⚠️ CÓDIGO ADMIN\n\n"
                f"No se encontró ningún timer activo para el folio: {folio_admin}\n"
                f"Verifique el número de folio o que el timer no haya expirado ya."
            )
    else:
        await message.answer("⚠️ Formato incorrecto. Use: SERO[número de folio]")

# Handler para recibir comprobantes de pago (imágenes)
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    
    # Verificar si tiene timer activo
    if user_id not in timers_activos:
        await message.answer(
            "ℹ️ No se encontró ningún permiso pendiente de pago.\n\n"
            "Si desea tramitar un nuevo permiso, use /permiso"
        )
        return
    
    folio = timers_activos[user_id]["folio"]
    
    # Cancelar timer
    cancelar_timer(user_id)
    
    # Actualizar estado en base de datos
    supabase.table("folios_registrados").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    supabase.table("borradores_registros").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    await message.answer(
        f"✅ COMPROBANTE RECIBIDO CORRECTAMENTE\n\n"
        f"📄 Folio: {folio}\n"
        f"📸 Imagen procesada y almacenada\n"
        f"⏰ Timer de pago detenido\n\n"
        f"🔍 Su comprobante está siendo verificado por nuestro equipo.\n"
        f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
        f"Gracias por utilizar el Sistema Digital CDMX."
    )

# Handler para preguntas sobre costo/precio/depósito
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        "💰 INFORMACIÓN DE COSTO\n\n"
        "El costo es el mismo de siempre.\n\n"
        "Para iniciar su trámite use /permiso"
    )

@dp.message()
async def fallback(message: types.Message):
    respuestas_elegantes = [
        "🏛️ Sistema Digital CDMX. Para tramitar su permiso utilice /permiso",
        "📋 Servicio automatizado. Comando disponible: /permiso para iniciar trámite",
        "⚡ Sistema en línea. Use /permiso para generar su documento oficial",
        "🚗 Plataforma de permisos CDMX. Inicie su proceso con /permiso"
    ]
    await message.answer(random.choice(respuestas_elegantes))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    # Inicializar contador de folios desde Supabase
    inicializar_folio_desde_supabase()
    
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

@app.get("/")
async def root():
    return {"message": "Bot CDMX funcionando correctamente"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
