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
import string
from PIL import Image

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "cdmxdigital2025ppp.pdf"
PLANTILLA_BUENO = "elbueno.pdf"
# Plantillas Morelos añadidas
PLANTILLA_MORELOS_PDF = "morelos_hoja1_imagen.pdf"
PLANTILLA_MORELOS_BUENO = "morelosvergas1.pdf"

# Precio del permiso
PRECIO_PERMISO = 200 

# Coordenadas Morelos añadidas
coords_morelos = {
    "folio": (665,282,18,(1,0,0)),
    "placa": (200,200,60,(0,0,0)),
    "fecha": (200,340,14,(0,0,0)),
    "vigencia": (600,340,14,(0,0,0)),
    "marca": (110,425,14,(0,0,0)),
    "serie": (460,420,14,(0,0,0)),
    "linea": (110,455,14,(0,0,0)),
    "motor": (460,445,14,(0,0,0)),
    "anio": (110,485,14,(0,0,0)),
    "color": (460,395,14,(0,0,0)),
    "tipo": (510,470,14,(0,0,0)),
    "nombre": (150,370,14,(0,0,0)),
    "fecha_hoja2": (126,310,15,(0,0,0)),
}

# Meses en español para Morelos añadido
meses_es = {
    "January": "ENERO", "February": "FEBRERO", "March": "MARZO",
    "April": "ABRIL", "May": "MAYO", "June": "JUNIO",
    "July": "JULIO", "August": "AGOSTO", "September": "SEPTIEMBRE",
    "October": "OCTUBRE", "November": "NOVIEMBRE", "December": "DICIEMBRE"
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT MEJORADO CON FUNCIONES DE MORELOS ------------
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [lista_de_folios_activos]}

async def eliminar_folio_automatico(folio: str):
    """Elimina folio automáticamente después del tiempo límite - FUNCIÓN DE MORELOS AÑADIDA"""
    try:
        # Obtener user_id del folio
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Notificar al usuario si está disponible
        if user_id:
            entidad = "CDMX" if folio.startswith("822") else "MORELOS"
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO\n\n"
                f"El folio {folio} ({entidad}) ha sido eliminado del sistema por falta de pago.\n\n"
                f"Para tramitar un nuevo permiso utilize /permiso"
            )
        
        # Limpiar timers
        limpiar_timer_folio(folio)
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    """Envía recordatorios de pago - MEJORADO CON DETECCIÓN DE ENTIDAD"""
    try:
        if folio not in timers_activos:
            return  # Timer ya fue cancelado
            
        user_id = timers_activos[folio]["user_id"]
        entidad = "CDMX" if folio.startswith("822") else "MORELOS"
        
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO {entidad}\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: El costo es el mismo de siempre\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 2 horas con recordatorios para un folio específico - FUNCIÓN DE MORELOS AÑADIDA"""
    async def timer_task():
        start_time = datetime.now()
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id}")
        
        # Recordatorios cada 30 minutos
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)  # 30 minutos
            
            # Verificar si el timer sigue activo
            if folio not in timers_activos:
                print(f"[TIMER] Cancelado para folio {folio}")
                return  # Timer cancelado (usuario pagó)
                
            minutos_restantes = 120 - minutos
            await enviar_recordatorio(folio, minutos_restantes)
        
        # Último recordatorio a los 110 minutos (faltan 10)
        await asyncio.sleep(20 * 60)  # 20 minutos más
        if folio in timers_activos:
            await enviar_recordatorio(folio, 10)
        
        # Esperar 10 minutos finales
        await asyncio.sleep(10 * 60)
        
        # Si llegamos aquí, se acabó el tiempo
        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio}")
            await eliminar_folio_automatico(folio)
    
    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    # Agregar folio a la lista del usuario
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer iniciado para folio {folio}, total timers activos: {len(timers_activos)}")

def cancelar_timer(user_id: int):
    """Cancela el timer cuando el usuario paga - FUNCIÓN ORIGINAL CDMX MANTENIDA PARA COMPATIBILIDAD"""
    if user_id in timers_activos:
        timers_activos[user_id]["task"].cancel()
        del timers_activos[user_id]

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico cuando el usuario paga - FUNCIÓN DE MORELOS AÑADIDA"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        # Remover de estructuras de datos
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:  # Si no quedan folios, eliminar entrada
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}, timers restantes: {len(timers_activos)}")

def limpiar_timer_folio(folio: str):
    """Limpia todas las referencias de un folio tras expirar - FUNCIÓN DE MORELOS AÑADIDA"""
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    """Obtiene todos los folios activos de un usuario - FUNCIÓN DE MORELOS AÑADIDA"""
    return user_folios.get(user_id, [])

# ------------ FOLIO CDMX CON PREFIJO 822 PROGRESIVO (MANTENIDO) ------------
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

# ------------ FOLIO MORELOS CON PREFIJO 345 AÑADIDO ------------
folio_counter_morelos = {"count": 1}

def generar_folio_automatico_morelos() -> tuple:
    """
    Genera folio automático con prefijo 345 - FUNCIÓN DE MORELOS AÑADIDA
    Returns: (folio_generado: str, success: bool, error_msg: str)
    """
    max_intentos = 5
    
    for intento in range(max_intentos):
        folio = f"345{folio_counter_morelos['count']}"
        
        try:
            # Verificar si el folio ya existe en la BD
            response = supabase.table("folios_registrados") \
                .select("folio") \
                .eq("folio", folio) \
                .execute()
            
            if response.data:
                # Folio duplicado, incrementar contador y reintentar
                print(f"[WARNING] Folio {folio} duplicado, incrementando contador...")
                folio_counter_morelos["count"] += 1
                continue
            
            # Folio disponible
            folio_counter_morelos["count"] += 1
            print(f"[SUCCESS] Folio generado: {folio}")
            return folio, True, ""
            
        except Exception as e:
            print(f"[ERROR] Al verificar folio {folio}: {e}")
            folio_counter_morelos["count"] += 1
            continue
    
    # Si llegamos aquí, fallaron todos los intentos
    error_msg = f"Sistema sobrecargado, no se pudo generar folio único después de {max_intentos} intentos"
    print(f"[ERROR CRÍTICO] {error_msg}")
    return "", False, error_msg

def generar_placa_digital():
    """Genera placa digital para el vehículo - FUNCIÓN DE MORELOS AÑADIDA"""
    archivo = "placas_digitales.txt"
    abc = string.ascii_uppercase
    
    try:
        if not os.path.exists(archivo):
            with open(archivo, "w") as f:
                f.write("GSR1989\n")
                
        with open(archivo, "r") as f:
            ultimo = f.read().strip().split("\n")[-1]
            
        pref, num = ultimo[:3], int(ultimo[3:])
        
        if num < 9999:
            nuevo = f"{pref}{num+1:04d}"
        else:
            l1, l2, l3 = list(pref)
            i3 = abc.index(l3)
            if i3 < 25:
                l3 = abc[i3+1]
            else:
                i2 = abc.index(l2)
                if i2 < 25:
                    l2 = abc[i2+1]
                    l3 = "A"
                else:
                    l1 = abc[(abc.index(l1)+1)%26]
                    l2 = l3 = "A"
            nuevo = f"{l1}{l2}{l3}0000"
            
        with open(archivo, "a") as f:
            f.write(nuevo+"\n")
            
        return nuevo
        
    except Exception as e:
        print(f"[ERROR] Generando placa digital: {e}")
        # Fallback: generar placa aleatoria
        letras = ''.join(random.choices(abc, k=3))
        numeros = ''.join(random.choices('0123456789', k=4))
        return f"{letras}{numeros}"

def inicializar_folio_desde_supabase():
    """
    Busca el último folio de CDMX en Supabase y ajusta el contador. MANTENIDO ORIGINAL
    + Inicializa también contador de Morelos AÑADIDO
    """
    # CDMX (CÓDIGO ORIGINAL MANTENIDO)
    try:
        # Primero intentamos buscar por entidad 'cdmx'
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
                print(f"[INFO] Folio inicializado desde Supabase: {ultimo_folio}, siguiente: {folio_counter['siguiente']}")
                return
        
        # Si no hay folios de CDMX, buscar cualquier folio que empiece con 822
        response_general = supabase.table("folios_registrados") \
            .select("folio") \
            .like("folio", f"{FOLIO_PREFIJO}%") \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()
        
        if response_general.data:
            ultimo_folio = response_general.data[0]["folio"]
            if isinstance(ultimo_folio, str) and ultimo_folio.startswith(FOLIO_PREFIJO):
                numero = int(ultimo_folio[len(FOLIO_PREFIJO):])
                folio_counter["siguiente"] = numero + 2
                print(f"[INFO] Folio inicializado desde cualquier 822: {ultimo_folio}, siguiente: {folio_counter['siguiente']}")
                return
        
        # Si no hay ningún folio 822, empezar desde 1
        folio_counter["siguiente"] = 1
        print(f"[INFO] No se encontraron folios 822, empezando desde: {folio_counter['siguiente']}")
        
    except Exception as e:
        print(f"[ERROR] Al inicializar folio CDMX: {e}")
        folio_counter["siguiente"] = 1
    
    # MORELOS AÑADIDO
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", "morelos") \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            ultimo_folio = response.data[0]["folio"]
            # Extraer número del folio (eliminar prefijo "345")
            if ultimo_folio.startswith("345") and len(ultimo_folio) > 3:
                try:
                    numero = int(ultimo_folio[3:])  # Quitar "345" del inicio
                    folio_counter_morelos["count"] = numero + 1
                    print(f"[INFO] Folio Morelos inicializado desde Supabase: {ultimo_folio}, siguiente: 345{folio_counter_morelos['count']}")
                except ValueError:
                    print("[ERROR] Formato de folio inválido en BD, iniciando desde 3451")
                    folio_counter_morelos["count"] = 1
            else:
                print("[INFO] No hay folios con prefijo 345, iniciando desde 3451")
                folio_counter_morelos["count"] = 1
        else:
            print("[INFO] No se encontraron folios de Morelos, iniciando desde 3451")
            folio_counter_morelos["count"] = 1
            
        print(f"[SISTEMA] Próximo folio Morelos a generar: 345{folio_counter_morelos['count']}")
        
    except Exception as e:
        print(f"[ERROR CRÍTICO] Al inicializar folio Morelos: {e}")
        folio_counter_morelos["count"] = 1
        print("[FALLBACK] Iniciando contador Morelos desde 3451")

# ------------ FSM STATES EXPANDIDO CON ESTADOS DE MORELOS ------------
class PermisoForm(StatesGroup):
    entidad = State()  # NUEVO: Seleccionar entidad
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    # Estados adicionales para Morelos
    color = State()
    tipo = State()
    nombre = State()

# ------------ GENERACIÓN PDF CDMX (MANTENIDO ORIGINAL) ------------BLOQUE 2:# ------------ GENERACIÓN PDF CDMX (MANTENIDO ORIGINAL) ------------
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

# ------------ GENERACIÓN PDF MORELOS AÑADIDO ------------
def generar_pdf_principal_morelos(datos: dict) -> tuple:
    """
    Genera PDF principal MORELOS - FUNCIÓN AÑADIDA
    Returns: (filename: str, success: bool, error_msg: str)
    """
    try:
        doc = fitz.open(PLANTILLA_MORELOS_PDF)
        pg = doc[0]

        # Usar coordenadas de Morelos
        pg.insert_text(coords_morelos["folio"][:2], datos["folio"], fontsize=coords_morelos["folio"][2], color=coords_morelos["folio"][3])
        pg.insert_text(coords_morelos["placa"][:2], datos["placa"], fontsize=coords_morelos["placa"][2], color=coords_morelos["placa"][3])
        pg.insert_text(coords_morelos["fecha"][:2], datos["fecha"], fontsize=coords_morelos["fecha"][2], color=coords_morelos["fecha"][3])
        pg.insert_text(coords_morelos["vigencia"][:2], datos["vigencia"], fontsize=coords_morelos["vigencia"][2], color=coords_morelos["vigencia"][3])
        pg.insert_text(coords_morelos["marca"][:2], datos["marca"], fontsize=coords_morelos["marca"][2], color=coords_morelos["marca"][3])
        pg.insert_text(coords_morelos["serie"][:2], datos["serie"], fontsize=coords_morelos["serie"][2], color=coords_morelos["serie"][3])
        pg.insert_text(coords_morelos["linea"][:2], datos["linea"], fontsize=coords_morelos["linea"][2], color=coords_morelos["linea"][3])
        pg.insert_text(coords_morelos["motor"][:2], datos["motor"], fontsize=coords_morelos["motor"][2], color=coords_morelos["motor"][3])
        pg.insert_text(coords_morelos["anio"][:2], datos["anio"], fontsize=coords_morelos["anio"][2], color=coords_morelos["anio"][3])
        pg.insert_text(coords_morelos["color"][:2], datos["color"], fontsize=coords_morelos["color"][2], color=coords_morelos["color"][3])
        pg.insert_text(coords_morelos["tipo"][:2], datos["tipo"], fontsize=coords_morelos["tipo"][2], color=coords_morelos["tipo"][3])
        pg.insert_text(coords_morelos["nombre"][:2], datos["nombre"], fontsize=coords_morelos["nombre"][2], color=coords_morelos["nombre"][3])

        # Segunda página: texto + QR
        if len(doc) > 1:
            pg2 = doc[1]

            # Insertar vigencia en hoja 2
            pg2.insert_text(
                coords_morelos["fecha_hoja2"][:2],
                datos["vigencia"],
                fontsize=coords_morelos["fecha_hoja2"][2],
                color=coords_morelos["fecha_hoja2"][3]
            )

            # Generar QR
            texto_qr = (
                f"FOLIO: {datos['folio']}\n"
                f"NOMBRE: {datos['nombre']}\n"
                f"MARCA: {datos['marca']}\n"
                f"LINEA: {datos['linea']}\n"
                f"AÑO: {datos['anio']}\n"
                f"SERIE: {datos['serie']}\n"
                f"MOTOR: {datos['motor']}\n"
                f"PERMISO MORELOS DIGITAL"
            )

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=2,
            )
            qr.add_data(texto_qr)
            qr.make(fit=True)

            qr_img = qr.make_image(fill_color="black", back_color="white")
            buffer = BytesIO()
            qr_img.save(buffer, format="PNG")
            buffer.seek(0)

            rect_qr = fitz.Rect(665, 282, 665 + 70.87, 282 + 70.87)  # 2.5 cm x 2.5 cm
            pg2.insert_image(rect_qr, stream=buffer.read())

        filename = f"{OUTPUT_DIR}/{datos['folio']}_morelos.pdf"
        doc.save(filename)
        doc.close()
        return filename, True, ""
        
    except Exception as e:
        error_msg = f"Error generando PDF principal: {str(e)}"
        print(f"[ERROR PDF] {error_msg}")
        return "", False, error_msg

def generar_pdf_bueno_morelos(folio: str, numero_serie: str, nombre: str) -> tuple:
    """
    Genera PDF de comprobante MORELOS - FUNCIÓN AÑADIDA
    Returns: (filename: str, success: bool, error_msg: str)
    """
    try:
        doc = fitz.open(PLANTILLA_MORELOS_BUENO)
        page = doc[0]

        ahora = datetime.now()
        page.insert_text((155, 245), nombre.upper(), fontsize=18, fontname="helv")
        page.insert_text((1045, 205), folio, fontsize=20, fontname="helv")
        page.insert_text((1045, 275), ahora.strftime("%d/%m/%Y"), fontsize=20, fontname="helv")
        page.insert_text((1045, 348), ahora.strftime("%H:%M:%S"), fontsize=20, fontname="helv")

        filename = f"{OUTPUT_DIR}/{folio}.pdf"
        doc.save(filename)
        doc.close()
        return filename, True, ""
        
    except Exception as e:
        error_msg = f"Error generando PDF comprobante: {str(e)}"
        print(f"[ERROR PDF] {error_msg}")
        return "", False, error_msg

# ------------ HANDLERS CDMX CON DIÁLOGOS ELEGANTES (MODIFICADOS PARA SOPORTAR ENTIDADES) ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ Sistema Digital de Permisos - CDMX & MORELOS\n"
        "Servicio oficial automatizado para trámites vehiculares\n\n"
        "📍 Entidades disponibles:\n"
        "• CDMX (Folios 822xxx)\n"
        "• MORELOS (Folios 345xxx)\n\n"
        "💰 Costo del permiso: El costo es el mismo de siempre\n"
        "⏰ Tiempo límite para pago: 2 horas\n"
        "📸 Métodos de pago: Transferencia bancaria y OXXO\n\n"
        "📋 Use /permiso para iniciar su trámite\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Mantener compatibilidad con timers antiguos pero agregar nuevas funciones
    folios_activos = obtener_folios_usuario(message.from_user.id)
    
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer independiente)"
    
    await message.answer(
        f"🚗 SISTEMA INTEGRADO DE PERMISOS\n\n"
        f"📋 Costo: El costo es el mismo de siempre\n"
        f"⏰ Tiempo para pagar: 2 horas\n"
        f"📱 Concepto de pago: Su folio asignado\n\n"
        f"📍 Seleccione la entidad para tramitar su permiso:\n\n"
        f"1️⃣ Envíe: CDMX (Folios 822xxx)\n"
        f"2️⃣ Envíe: MORELOS (Folios 345xxx)\n\n"
        f"Al continuar acepta que su folio será eliminado si no paga en el tiempo establecido."
        + mensaje_folios
    )
    await state.set_state(PermisoForm.entidad)

@dp.message(PermisoForm.entidad)
async def get_entidad(message: types.Message, state: FSMContext):
    entidad = message.text.strip().upper()
    
    if entidad not in ["CDMX", "MORELOS"]:
        await message.answer(
            "⚠️ ENTIDAD NO VÁLIDA\n\n"
            "Por favor, seleccione una entidad válida:\n"
            "• Escriba: CDMX\n"
            "• Escriba: MORELOS\n\n"
            "Intente nuevamente:"
        )
        return
        
    await state.update_data(entidad=entidad)
    emoji = "🏙️" if entidad == "CDMX" else "🏔️"
    prefijo = "822xxx" if entidad == "CDMX" else "345xxx"
    
    await message
    await message.answer(
        f"{emoji} TRÁMITE DE PERMISO {entidad}\n\n"
        f"📄 Sistema de folios: {prefijo}\n"
        f"📋 Costo: El costo es el mismo de siempre\n"
        f"⏰ Tiempo para pagar: 2 horas\n\n"
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
    datos = await state.get_data()
    entidad = datos.get("entidad", "CDMX")
    
    await state.update_data(motor=motor)
    
    # Si es MORELOS, pedir color y tipo. Si es CDMX, ir directo a nombre
    if entidad == "MORELOS":
        await message.answer(
            f"✅ MOTOR: {motor}\n\n"
            "Especifique el COLOR del vehículo:"
        )
        await state.set_state(PermisoForm.color)
    else:  # CDMX
        await message.answer(
            f"✅ MOTOR: {motor}\n\n"
            "Finalmente, proporcione el NOMBRE COMPLETO del titular:"
        )
        await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ COLOR: {color}\n\n"
        "Indique el TIPO de vehículo (automóvil, camioneta, motocicleta, etc.):"
    )
    await state.set_state(PermisoForm.tipo)

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    tipo = message.text.strip().upper()
    await state.update_data(tipo=tipo)
    await message.answer(
        f"✅ TIPO: {tipo}\n\n"
        "Para finalizar, proporcione el NOMBRE COMPLETO del titular del vehículo:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    entidad = datos.get("entidad", "CDMX")
    
    datos["nombre"] = nombre
    
    # Generar folio según entidad
    if entidad == "MORELOS":
        folio, folio_success, folio_error = generar_folio_automatico_morelos()
        if not folio_success:
            await message.answer(
                f"❌ ERROR GENERANDO FOLIO MORELOS\n\n"
                f"{folio_error}\n\n"
                "🔄 Por favor, utilice /permiso nuevamente para que el sistema le asigne el siguiente folio disponible."
            )
            await state.clear()
            return
        datos["placa"] = generar_placa_digital()
    else:  # CDMX
        folio = obtener_siguiente_folio()
        
    datos["folio"] = folio

    # -------- FECHAS SEGÚN ENTIDAD --------
    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)
    
    if entidad == "MORELOS":
        datos["fecha"] = hoy.strftime(f"%d DE {meses_es[hoy.strftime('%B')]} DEL %Y").upper()
        datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
    else:  # CDMX
        meses = {
            1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
            5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
            9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
        }
        datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
        datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
    # -------------------------

    placa_info = f"🚗 Placa digital: {datos['placa']}\n" if entidad == "MORELOS" else ""
    
    await message.answer(
        f"🔄 PROCESANDO PERMISO {entidad}...\n\n"
        f"📄 Folio asignado: {datos['folio']}\n"
        f"{placa_info}"
        f"👤 Titular: {nombre}\n\n"
        "Generando documentos oficiales..."
    )

    try:
        # Generar PDFs según entidad
        if entidad == "MORELOS":
            p1, pdf1_success, pdf1_error = generar_pdf_principal_morelos(datos)
            if not pdf1_success:
                await message.answer(f"❌ ERROR GENERANDO DOCUMENTO PRINCIPAL MORELOS\n{pdf1_error}")
                await state.clear()
                return
                
            p2, pdf2_success, pdf2_error = generar_pdf_bueno_morelos(datos["folio"], datos["serie"], datos["nombre"])
            if not pdf2_success:
                await message.answer(f"❌ ERROR GENERANDO COMPROBANTE MORELOS\n{pdf2_error}")
                await state.clear()
                return
                
            caption1 = f"📋 PERMISO OFICIAL DE CIRCULACIÓN - MORELOS\nFolio: {datos['folio']}\nPlaca: {datos['placa']}\nVigencia: 30 días\n🏛️ Documento con validez oficial"
            caption2 = f"📋 COMPROBANTE DE VERIFICACIÓN\nSerie: {datos['serie']}\n🔍 Documento complementario de autenticidad"
        else:  # CDMX
            p1 = generar_pdf_principal(datos)
            p2 = generar_pdf_bueno(datos["serie"], hoy, datos["folio"])
            caption1 = f"📋 PERMISO PRINCIPAL CDMX\nFolio: {datos['folio']}\nVigencia: 30 días\n🏛️ Documento oficial con validez legal"
            caption2 = f"📋 DOCUMENTO DE VERIFICACIÓN\nSerie: {datos['serie']}\n🔍 Comprobante adicional de autenticidad"

        # Enviar documentos
        await message.answer_document(FSInputFile(p1), caption=caption1)
        await message.answer_document(FSInputFile(p2), caption=caption2)

        # Guardar en base de datos según entidad
        registro_principal = {
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "nombre": datos["nombre"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": entidad.lower(),
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }
        
        registro_borrador = {
            "folio": datos["folio"],
            "entidad": entidad.upper(),
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
        }
        
        # Solo agregar color para Morelos
        if entidad == "MORELOS":
            registro_principal["color"] = datos["color"]
            registro_borrador["color"] = datos["color"]

        supabase.table("folios_registrados").insert(registro_principal).execute()
        supabase.table("borradores_registros").insert(registro_borrador).execute()

        # INICIAR TIMER DE PAGO CON NUEVO SISTEMA
        await iniciar_timer_pago(message.from_user.id, datos['folio'])

        # Mensaje de instrucciones de pago
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

# ------------ CÓDIGO SECRETO ADMIN MEJORADO PARA AMBAS ENTIDADES ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    
    # Verificar formato: SERO + número de folio
    if len(texto) > 4:
        folio_admin = texto[4:]  # Quitar "SERO" del inicio
        
        # Validar prefijos válidos
        if not (folio_admin.startswith("822") or folio_admin.startswith("345")):
            await message.answer(
                f"⚠️ FOLIO INVÁLIDO\n\n"
                f"El folio {folio_admin} no tiene un prefijo válido.\n"
                f"Prefijos válidos:\n"
                f"• 822 para CDMX\n"
                f"• 345 para MORELOS\n\n"
                f"Ejemplo: SERO8225 o SERO3451234"
            )
            return
        
        # Buscar si hay un timer activo con ese folio (NUEVO SISTEMA)
        if folio_admin in timers_activos:
            user_con_folio = timers_activos[folio_admin]["user_id"]
            entidad = "CDMX" if folio_admin.startswith("822") else "MORELOS"
            
            # Cancelar timer específico
            cancelar_timer_folio(folio_admin)
            
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
                f"✅ TIMER DEL FOLIO {folio_admin} SE DETUVO CON ÉXITO\n\n"
                f"🔐 Código admin ejecutado correctamente\n"
                f"📍 Entidad: {entidad}\n"
                f"⏰ Timer cancelado exitosamente\n"
                f"📄 Estado actualizado a VALIDADO_ADMIN\n"
                f"👤 Usuario ID: {user_con_folio}\n\n"
                f"El usuario ha sido notificado automáticamente."
            )
            
            # Notificar al usuario que su permiso está validado
            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO POR ADMINISTRACIÓN\n\n"
                    f"📄 Folio: {folio_admin} ({entidad})\n"
                    f"Su permiso ha sido validado por administración.\n"
                    f"El documento está completamente activo para circular.\n\n"
                    f"Gracias por utilizar el Sistema Digital Integrado."
                )
            except Exception as e:
                print(f"Error notificando al usuario {user_con_folio}: {e}")
        else:
            # MANTENER COMPATIBILIDAD CON SISTEMA ANTERIOR PARA CDMX
            user_con_folio = None
            for user_id, timer_info in timers_activos.items():
                if isinstance(timer_info, dict) and timer_info.get("folio") == folio_admin:
                    user_con_folio = user_id
                    break
            
            if user_con_folio:
                # Cancelar timer del sistema anterior
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
                    f"✅ TIMER DEL FOLIO {folio_admin} SE DETUVO CON ÉXITO\n\n"
                    f"🔐 Código admin ejecutado correctamente (sistema anterior)\n"
                    f"⏰ Timer cancelado exitosamente\n"
                    f"📄 Estado actualizado a VALIDADO_ADMIN\n"
                    f"👤 Usuario ID: {user_con_folio}\n\n"
                    f"El usuario ha sido notificado automáticamente."
                )
                
                # Notificar al usuario
                try:
                    await bot.send_message(
                        user_con_folio,
                        f"✅ PAGO VALIDADO POR ADMINISTRACIÓN\n\n"
                        f"📄 Folio: {folio_admin}\n"
                        f"Su permiso ha sido validado por administración.\n"
                        f"El documento está completamente activo para circular.\n\n"
                        f"Gracias por utilizar el Sistema Digital Integrado."
                    )
                except Exception as e:
                    print(f"Error notificando al usuario {user_con_folio}: {e}")
            else:
                await message.answer(
                    f"❌ ERROR: TIMER NO ENCONTRADO\n\n"
                    f"📄 Folio: {folio_admin}\n"
                    f"⚠️ No se encontró ningún timer activo para este folio.\n\n"
                    f"Posibles causas:\n"
                    f"• El timer ya expiró automáticamente\n"
                    f"• El usuario ya envió comprobante\n"
                    f"• El folio no existe o es incorrecto\n"
                    f"• El folio ya fue validado anteriormente"
                )
    else:
        await message.answer(
            "⚠️ FORMATO INCORRECTO\n\n"
            "Use el formato: SERO[número de folio]\n"
            "Ejemplo: SERO8225 (CDMX) o SERO3451234 (MORELOS)"
        )

# Handler para recibir comprobantes de pago (MEJORADO PARA MÚLTIPLES FOLIOS)
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    # Verificar si tiene timer activo en sistema anterior (COMPATIBILIDAD)
    if user_id in timers_activos and isinstance(timers_activos[user_id], dict) and "folio" in timers_activos[user_id]:
        folio = timers_activos[user_id]["folio"]
        
        # Cancelar timer del sistema anterior
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
        
        entidad = "CDMX" if folio.startswith("822") else "MORELOS"
        
        await message.answer(
            f"✅ COMPROBANTE RECIBIDO CORRECTAMENTE\n\n"
            f"📄 Folio: {folio} ({entidad})\n"
            f"📸 Gracias por la imagen, este comprobante será revisado por un 2do filtro\n"
            f"⏰ Timer de pago detenido\n\n"
            f"🔍 Su comprobante está siendo verificado por nuestro equipo.\n"
            f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
            f"Gracias por utilizar el Sistema Digital Integrado."
        )
        return
    
    # Verificar nuevos folios del sistema mejorado
    if not folios_usuario:
        await message.answer(
            "ℹ️ No se encontró ningún permiso pendiente de pago.\n\n"
            "Si desea tramitar un nuevo permiso, use /permiso"
        )
        return
    
    # Si tiene varios folios, preguntar cuál
    if len(folios_usuario) > 1:
        lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
        await message.answer(
            f"📄 MÚLTIPLES FOLIOS ACTIVOS\n\n"
            f"Tienes {len(folios_usuario)} folios pendientes de pago:\n\n"
            f"{lista_folios}\n\n"
            f"Por favor, responda con el NÚMERO DE FOLIO al que corresponde este comprobante.\n"
            f"Ejemplo: {folios_usuario[0]}"
        )
        return
    
    # Solo un folio activo, procesar automáticamente
    folio = folios_usuario[0]
    entidad = "CDMX" if folio.startswith("822") else "MORELOS"
    
    # Cancelar timer específico del folio
    cancelar_timer_folio(folio)
    
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
        f"📄 Folio: {folio} ({entidad})\n"
        f"📸 Gracias por la imagen, este comprobante será revisado por un segundo filtro de verificación\n"
        f"⏰ Timer específico del folio detenido exitosamente\n\n"
        f"🔍 Su comprobante está siendo verificado por nuestro equipo especializado.\n"
        f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
        f"Agradecemos su confianza en el Sistema Digital Integrado."
    )

# Comando para ver folios activos AÑADIDO
@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
            "No tienes folios pendientes de pago en este momento.\n\n"
            "Para crear un nuevo permiso utilice /permiso"
        )
        return
    
    lista_folios = []
    for folio in folios_usuario:
        entidad = "CDMX" if folio.startswith("822") else "MORELOS"
        if folio in timers_activos:
            tiempo_restante = 120 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
            tiempo_restante = max(0, tiempo_restante)
            lista_folios.append(f"• {folio} ({entidad}) - {tiempo_restante} min restantes")
        else:
            lista_folios.append(f"• {folio} ({entidad}) - sin timer")
    
    await message.answer(
        f"📋 SUS FOLIOS ACTIVOS ({len(folios_usuario)})\n\n"
        + '\n'.join(lista_folios) +
        f"\n\n⏰ Cada folio tiene su propio timer independiente.\n"
        f"📸 Para enviar comprobante, use una imagen."
    )

# Handler para preguntas sobre costo/precio/depósito (MANTENIDO)
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
        "🏛️ Sistema Digital Integrado (CDMX/MORELOS). Para tramitar su permiso utilice /permiso",
        "📋 Servicio automatizado dual. Comando disponible: /permiso para iniciar trámite",
        "⚡ Sistema en línea integrado. Use /permiso para generar su documento oficial",
        "🚗 Plataforma de permisos integrada. Inicie su proceso con /permiso"
    ]
    await message.answer(random.choice(respuestas_elegantes))

# ------------ FASTAPI + LIFESPAN (MEJORADO) ------------BLOQUE 3:# ------------ FASTAPI + LIFESPAN (MEJORADO) ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    # Inicializar contador de folios desde Supabase (AMBOS SISTEMAS)
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
    return {
        "message": "Bot CDMX + MORELOS funcionando correctamente",
        "version": "3.0 - Sistema Integrado",
        "entidades": ["CDMX", "MORELOS"],
        "folios": {
            "cdmx": f"822{folio_counter['siguiente']}",
            "morelos": f"345{folio_counter_morelos['count']}"
        },
        "timers_activos": len(timers_activos),
        "sistema": "Timers independientes por folio"
    }

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
