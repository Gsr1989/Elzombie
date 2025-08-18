# IMPORTAR_MODULOS.EXE INICIANDO...
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

# ===== CONFIGURACION_SECRETA.DAT =====
TOKEN_ROBOT = "7558047306:AAGza9rjVbRz2MwUN9c1a5LJ3mvM99qfHFM"
DB_URL_SUPER = "https://xsagwqepoljfsogusubw.supabase.co"
DB_KEY_ULTRA = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws"
URL_BASE_MAESTRA = os.getenv("BASE_URL", "").rstrip("/")
CARPETA_DOCS = "documentos"
TEMPLATE_PDF_PRINCIPAL = "cdmxdigital2025ppp.pdf"
TEMPLATE_PDF_BUENO = "elbueno.pdf"

# CREAR_DIRECTORIO.exe
os.makedirs(CARPETA_DOCS, exist_ok=True)

# ===== CONEXION_DB_ULTRA.init =====
cliente_supabase: Client = create_client(DB_URL_SUPER, DB_KEY_ULTRA)

# ===== ROBOT_TELEGRAM.boot =====
robot_bot = Bot(token=TOKEN_ROBOT)
memoria_storage = MemoryStorage()
procesador_dp = Dispatcher(robot_bot, storage=memoria_storage)

# ===== CONTADOR_FOLIOS.sys =====
folio_memoria = {"valor_actual": 1}

def GENERAR_FOLIO_NUEVO():
    """FUNCION_FOLIO_INCREMENTO.exe"""
    folio_nuevo = f"01{folio_memoria['valor_actual']}"
    folio_memoria['valor_actual'] += 1
    return folio_nuevo

# ===== ESTADOS_MAQUINA.cfg =====
class FormularioPermiso(StatesGroup):
    marca_vehiculo = State()
    linea_vehiculo = State() 
    anio_fabricacion = State()
    numero_serie = State()
    numero_motor = State()
    nombre_solicitante = State()

# ===== GENERADORES_PDF.dll =====

def CREAR_PDF_PRINCIPAL(datos_entrada):
    """GENERADOR_PDF_PRINCIPAL.exe EJECUTANDO..."""
    documento = fitz.open(TEMPLATE_PDF_PRINCIPAL)
    pagina_uno = documento[0]
    
    # INSERTAR_DATOS.process
    pagina_uno.insert_text((100, 100), f"FOLIO: {datos_entrada['folio']}", fontsize=12)
    pagina_uno.insert_text((100, 120), f"MARCA: {datos_entrada['marca']}", fontsize=12)
    pagina_uno.insert_text((100, 140), f"LÍNEA: {datos_entrada['linea']}", fontsize=12)
    pagina_uno.insert_text((100, 160), f"AÑO: {datos_entrada['anio']}", fontsize=12)
    pagina_uno.insert_text((100, 180), f"SERIE: {datos_entrada['serie']}", fontsize=12)
    pagina_uno.insert_text((100, 200), f"MOTOR: {datos_entrada['motor']}", fontsize=12)
    pagina_uno.insert_text((100, 220), f"NOMBRE: {datos_entrada['nombre']}", fontsize=12)
    
    # GUARDAR_ARCHIVO.save
    archivo_salida = f"{CARPETA_DOCS}/{datos_entrada['folio']}_principal.pdf"
    documento.save(archivo_salida)
    return archivo_salida

def CREAR_PDF_BUENO(serie_num, fecha_actual, folio_num):
    """GENERADOR_PDF_BUENO.exe EJECUTANDO..."""
    documento = fitz.open(TEMPLATE_PDF_BUENO)
    pagina_uno = documento[0]
    
    # INSERTAR_DATOS_BUENO.process
    pagina_uno.insert_text((135.02, 193.88), serie_num, fontsize=6)
    pagina_uno.insert_text((190, 324), fecha_actual.strftime('%d/%m/%Y'), fontsize=6)
    
    # GUARDAR_ARCHIVO_BUENO.save
    archivo_bueno = f"{CARPETA_DOCS}/{folio_num}_bueno.pdf"
    documento.save(archivo_bueno)
    return archivo_bueno

# ===== MANEJADORES_COMANDOS.handlers =====

@procesador_dp.message_handler(Command("start"), state="*")
async def COMANDO_START(mensaje: types.Message, estado: FSMContext):
    """INICIALIZADOR_SISTEMA.run"""
    await estado.finish()
    await mensaje.answer("🤖 SISTEMA_ROBOT ACTIVADO. EJECUTAR /permiso PARA_INICIAR_PROCESO")

@procesador_dp.message_handler(Command("permiso"), state="*") 
async def COMANDO_PERMISO(mensaje: types.Message):
    """PROCESO_PERMISO.init"""
    await mensaje.answer("🔧 INTRODUCIR_MARCA_VEHICULO:")
    await FormularioPermiso.marca_vehiculo.set()

@procesador_dp.message_handler(state=FormularioPermiso.marca_vehiculo)
async def CAPTURAR_MARCA(mensaje: types.Message, estado: FSMContext):
    """CAPTURA_MARCA.process"""
    await estado.update_data(marca=mensaje.text.strip())
    await mensaje.answer("⚙️ INTRODUCIR_LINEA_VEHICULO:")
    await FormularioPermiso.linea_vehiculo.set()

@procesador_dp.message_handler(state=FormularioPermiso.linea_vehiculo)
async def CAPTURAR_LINEA(mensaje: types.Message, estado: FSMContext):
    """CAPTURA_LINEA.process"""
    await estado.update_data(linea=mensaje.text.strip())
    await mensaje.answer("📅 INTRODUCIR_AÑO_FABRICACION:")
    await FormularioPermiso.anio_fabricacion.set()

@procesador_dp.message_handler(state=FormularioPermiso.anio_fabricacion)
async def CAPTURAR_ANIO(mensaje: types.Message, estado: FSMContext):
    """CAPTURA_AÑO.process"""
    await estado.update_data(anio=mensaje.text.strip())
    await mensaje.answer("🔢 INTRODUCIR_NUMERO_SERIE:")
    await FormularioPermiso.numero_serie.set()

@procesador_dp.message_handler(state=FormularioPermiso.numero_serie)
async def CAPTURAR_SERIE(mensaje: types.Message, estado: FSMContext):
    """CAPTURA_SERIE.process"""
    await estado.update_data(serie=mensaje.text.strip())
    await mensaje.answer("🚗 INTRODUCIR_NUMERO_MOTOR:")
    await FormularioPermiso.numero_motor.set()

@procesador_dp.message_handler(state=FormularioPermiso.numero_motor)
async def CAPTURAR_MOTOR(mensaje: types.Message, estado: FSMContext):
    """CAPTURA_MOTOR.process"""
    await estado.update_data(motor=mensaje.text.strip())
    await mensaje.answer("👤 INTRODUCIR_NOMBRE_SOLICITANTE:")
    await FormularioPermiso.nombre_solicitante.set()

@procesador_dp.message_handler(state=FormularioPermiso.nombre_solicitante)
async def CAPTURAR_NOMBRE_Y_PROCESAR(mensaje: types.Message, estado: FSMContext):
    """PROCESADOR_FINAL.exe EJECUTANDO..."""
    datos_completos = await estado.get_data()
    datos_completos["nombre"] = mensaje.text.strip()
    datos_completos["folio"] = GENERAR_FOLIO_NUEVO()

    try:
        # GENERAR_DOCUMENTOS.process
        ruta_pdf1 = CREAR_PDF_PRINCIPAL(datos_completos)
        ruta_pdf2 = CREAR_PDF_BUENO(datos_completos["serie"], datetime.now(), datos_completos["folio"])

        # ENVIAR_DOCUMENTOS.send
        await mensaje.answer_document(open(ruta_pdf1, "rb"), 
                                     caption=f"📄 DOCUMENTO_PRINCIPAL - FOLIO: {datos_completos['folio']}")
        await mensaje.answer_document(open(ruta_pdf2, "rb"), 
                                     caption=f"✅ DOCUMENTO_BUENO - SERIE: {datos_completos['serie']}")

        # CALCULAR_FECHAS.compute
        fecha_expedicion = datetime.now().date()
        fecha_vencimiento = fecha_expedicion + timedelta(days=30)

        # GUARDAR_EN_DB.insert
        cliente_supabase.table("folios_registrados").insert({
            "folio": datos_completos['folio'],
            "marca": datos_completos['marca'],
            "linea": datos_completos['linea'],
            "anio": datos_completos['anio'],
            "numero_serie": datos_completos['serie'],
            "numero_motor": datos_completos['motor'],
            "nombre": datos_completos['nombre'],
            "fecha_expedicion": fecha_expedicion.isoformat(),
            "fecha_vencimiento": fecha_vencimiento.isoformat(),
            "entidad": "cdmx"
        }).execute()

        await mensaje.answer("✅ PROCESO_COMPLETADO. PERMISO_GENERADO_Y_REGISTRADO")
    
    except Exception as error_sistema:
        await mensaje.answer(f"❌ ERROR_CRITICO_SISTEMA: {error_sistema}")

    await estado.finish()

@procesador_dp.message_handler()
async def MANEJADOR_DEFAULT(mensaje: types.Message):
    """RESPUESTA_GENERICA.default"""
    await mensaje.answer("🤖 COMANDO_NO_RECONOCIDO. EJECUTAR /permiso PARA_INICIAR")

# ===== SERVIDOR_FASTAPI.server =====

tarea_keepalive = None

async def MANTENER_VIVO():
    """PROCESO_KEEPALIVE.daemon"""
    while True:
        await asyncio.sleep(600)  # ESPERAR 10_MINUTOS

@asynccontextmanager
async def CICLO_VIDA_APP(aplicacion: FastAPI):
    """GESTOR_CICLO_VIDA.manager"""
    global tarea_keepalive
    
    # INICIALIZAR_WEBHOOK.init
    await robot_bot.delete_webhook(drop_pending_updates=True)
    if URL_BASE_MAESTRA:
        await robot_bot.set_webhook(f"{URL_BASE_MAESTRA}/webhook", 
                                   allowed_updates=["message"])
    
    # INICIAR_DAEMON.start
    tarea_keepalive = asyncio.create_task(MANTENER_VIVO())
    
    yield
    
    # TERMINAR_PROCESOS.cleanup
    if tarea_keepalive:
        tarea_keepalive.cancel()
        with suppress(asyncio.CancelledError):
            await tarea_keepalive
    await robot_bot.session.close()

# INICIALIZAR_SERVIDOR.boot
aplicacion_web = FastAPI(lifespan=CICLO_VIDA_APP)

@aplicacion_web.post("/webhook")
async def WEBHOOK_TELEGRAM(peticion: Request):
    """PROCESADOR_WEBHOOK.handler"""
    datos_json = await peticion.json()
    actualizacion = types.Update(**datos_json)
    
    # CONFIGURAR_CONTEXTO.set
    Bot.set_current(robot_bot)
    Dispatcher.set_current(procesador_dp)
    
    # PROCESAR_ASINCRONO.async
    asyncio.create_task(procesador_dp.process_update(actualizacion))
    return {"status": "OK_ROBOT_EJECUTADO"}

# ===== FIN_PROGRAMA.exit =====
