from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
import asyncio
import random
from PIL import Image
import qrcode
import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "cdmxdigital2025ppp.pdf"
PLANTILLA_BUENO = "elbueno.pdf"

PRECIO_BASE = 374  # 30 días = 374, 60 = 748, 90 = 1122

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
# Session con timeout de 120s para evitar timeout en send_document
_bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=120))
bot = Bot(token=BOT_TOKEN, session=_bot_session)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ DATOS PENDIENTES (esperando selección de días) ------------
datos_pendientes: dict = {}

# ------------ TIMER MANAGEMENT - 36 HORAS ------------
timers_activos = {}
user_folios = {}
pending_comprobantes = {}

TOTAL_MINUTOS_TIMER = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]

        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()

        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO - CDMX\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )

        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return

        user_id = timers_activos[folio]["user_id"]

        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - CDMX\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} (36 horas)")

        await asyncio.sleep(34.5 * 3600)

        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)

        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }

    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)

    print(f"[SISTEMA] Timer 36h iniciado para folio {folio}, total timers: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]

        del timers_activos[folio]

        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

        print(f"[SISTEMA] Timer cancelado para folio {folio}")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]

        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ------------ FOLIO CDMX CON PREFIJO 122 ------------
FOLIO_PREFIJO = "122"
folio_counter = {"siguiente": 1}
MAX_INTENTOS_FOLIO = 100000

def folio_existe_en_supabase(folio: str) -> bool:
    try:
        response = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(response.data) > 0
    except Exception as e:
        print(f"[ERROR] Verificando existencia de folio {folio}: {e}")
        return False

def obtener_siguiente_folio():
    intentos = 0
    while intentos < MAX_INTENTOS_FOLIO:
        folio_num = folio_counter["siguiente"]
        folio = f"{FOLIO_PREFIJO}{folio_num}"

        if not folio_existe_en_supabase(folio):
            folio_counter["siguiente"] += 4
            print(f"[FOLIO] Asignado: {folio}, siguiente será: {FOLIO_PREFIJO}{folio_counter['siguiente']}")
            return folio

        print(f"[FOLIO] {folio} ya existe, intentando con el siguiente...")
        folio_counter["siguiente"] += 1
        intentos += 1

    raise Exception(f"No se pudo generar un folio único después de {MAX_INTENTOS_FOLIO} intentos")

def inicializar_folio_desde_supabase():
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
                folio_counter["siguiente"] = numero + 4
                print(f"[INFO] Folio inicializado desde Supabase: {ultimo_folio}, siguiente: {folio_counter['siguiente']}")
                return

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
                folio_counter["siguiente"] = numero + 4
                print(f"[INFO] Folio inicializado desde cualquier 122: {ultimo_folio}, siguiente: {folio_counter['siguiente']}")
                return

        folio_counter["siguiente"] = 1
        print(f"[INFO] No se encontraron folios 122, empezando desde: {folio_counter['siguiente']}")

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

URL_CONSULTA_BASE = "https://semovidigitalgob.onrender.com"

def generar_qr_dinamico_cdmx(folio):
    try:
        url_directa = f"{URL_CONSULTA_BASE}/consulta/{folio}"

        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1
        )
        qr.add_data(url_directa)
        qr.make(fit=True)

        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR CDMX] Generado para folio {folio} -> {url_directa}")
        return img_qr, url_directa

    except Exception as e:
        print(f"[ERROR QR CDMX] {e}")
        return None, None

# ------------ GENERACIÓN PDF UNIFICADO (2 PÁGINAS EN 1 ARCHIVO) ------------
def generar_pdf_unificado(datos: dict, dias: int) -> str:
    """Genera UN SOLO PDF con ambas plantillas (2 páginas).
    dias: 30, 60 o 90 — afecta vigencia en página 1 y precio/título en página 2.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{OUTPUT_DIR}/{datos['folio']}_completo.pdf"

    hoy = datos["fecha_obj"]
    fecha_ven = hoy + timedelta(days=dias)
    vigencia_str = fecha_ven.strftime("%d/%m/%Y")
    precio = PRECIO_BASE * (dias // 30)
    anio_actual = str(hoy.year)

    try:
        # ===== PÁGINA 1: PLANTILLA PRINCIPAL =====
        doc_principal = fitz.open(PLANTILLA_PDF)
        page_principal = doc_principal[0]

        page_principal.insert_text((50, 130), "FOLIO: ", fontsize=12, color=(0, 0, 0))
        page_principal.insert_text((100, 130), datos["folio"], fontsize=12, color=(1, 0, 0))
        page_principal.insert_text((130, 145), datos["fecha"], fontsize=12, color=(0, 0, 0))
        page_principal.insert_text((87, 290), datos["marca"], fontsize=11, color=(0, 0, 0))
        page_principal.insert_text((375, 290), datos["serie"], fontsize=11, color=(0, 0, 0))
        page_principal.insert_text((87, 307), datos["linea"], fontsize=11, color=(0, 0, 0))
        page_principal.insert_text((375, 307), datos["motor"], fontsize=11, color=(0, 0, 0))
        page_principal.insert_text((87, 323), datos["anio"], fontsize=11, color=(0, 0, 0))
        # Vigencia dinámica según días elegidos
        page_principal.insert_text((375, 323), vigencia_str, fontsize=11, color=(0, 0, 0))
        page_principal.insert_text((375, 340), datos["nombre"], fontsize=11, color=(0, 0, 0))

        # QR dinámico
        img_qr, url_qr = generar_qr_dinamico_cdmx(datos["folio"])

        if img_qr:
            from io import BytesIO
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())

            x_qr = 49
            y_qr = 653
            ancho_qr = 96
            alto_qr = 96

            page_principal.insert_image(
                fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
                pixmap=qr_pix,
                overlay=True
            )
            print(f"[QR CDMX] Insertado en página 1")

        # ===== PÁGINA 2: PLANTILLA BUENO =====
        doc_bueno = fitz.open(PLANTILLA_BUENO)
        page_bueno = doc_bueno[0]

        # --- Coordenadas base ---
        Y_SERIE = 193.88
        X_SERIE = 135.02

        # 1) TÍTULO — 10 puntos ARRIBA de serie — negritas
        titulo = f"IMPUESTO POR DERECHO DE AUTOMOVIL Y MOTOCICLETAS (PERMISO PARA CIRCULAR {dias} DIAS)"
        page_bueno.insert_text(
            (X_SERIE, Y_SERIE - 10),
            titulo,
            fontsize=6,
            fontname="hebo",
            color=(0, 0, 0)
        )

        # 2) SERIE — posición original
        page_bueno.insert_text(
            (X_SERIE, Y_SERIE),
            datos["serie"],
            fontsize=6,
            fontname="hebo",
            color=(0, 0, 0)
        )

        # 3) AÑO ACTUAL — 5 puntos abajo de serie — negritas
        page_bueno.insert_text(
            (X_SERIE, Y_SERIE + 5 + 6),   # +6 por el tamaño de fuente de serie
            anio_actual,
            fontsize=6,
            fontname="hebo",
            color=(0, 0, 0)
        )

        # 4) PRECIO — abajo del año — negritas
        page_bueno.insert_text(
            (X_SERIE, Y_SERIE + 5 + 6 + 5 + 6),  # año + margen + tamaño fuente
            f"${precio}",
            fontsize=6,
            fontname="hebo",
            color=(0, 0, 0)
        )

        # Fecha en la posición original que ya existía
        page_bueno.insert_text(
            (190, 324),
            hoy.strftime("%d/%m/%Y"),
            fontsize=6,
            fontname="hebo",
            color=(0, 0, 0)
        )

        # ===== UNIR AMBAS PÁGINAS =====
        doc_principal.insert_pdf(doc_bueno)
        doc_bueno.close()

        doc_principal.save(filename)
        doc_principal.close()

        print(f"[PDF UNIFICADO CDMX] ✅ Generado: {filename} ({dias} días, vigencia {vigencia_str}, precio ${precio})")

    except Exception as e:
        print(f"[ERROR] Generando PDF unificado CDMX: {e}")
        doc_fallback = fitz.open()
        page = doc_fallback.new_page()
        page.insert_text((50, 50), f"ERROR - Folio: {datos['folio']}", fontsize=12)
        doc_fallback.save(filename)
        doc_fallback.close()

    return filename

# ------------ FUNCIÓN PARA ENVIAR PDF Y REGISTRAR EN SUPABASE ------------
async def procesar_y_enviar_pdf(message_or_callback, datos: dict, dias: int):
    """Genera PDF, lo envía y registra en Supabase."""
    user_id = datos["user_id"]

    # Calcular fechas según días elegidos
    hoy = datos["fecha_obj"]
    fecha_ven = hoy + timedelta(days=dias)
    precio = PRECIO_BASE * (dias // 30)

    try:
        # Generar PDF con los días elegidos
        pdf_unificado = generar_pdf_unificado(datos, dias)

        # Botones de admin
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔑 Validar Admin", callback_data=f"validar_{datos['folio']}"),
                InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{datos['folio']}")
            ]
        ])

        # Enviar documento
        if hasattr(message_or_callback, 'message'):
            # Es un CallbackQuery
            chat_id = message_or_callback.message.chat.id
            await bot.send_document(
                chat_id,
                FSInputFile(pdf_unificado),
                caption=(
                    f"📋 PERMISO DE CIRCULACIÓN - CDMX (COMPLETO)\n"
                    f"Folio: {datos['folio']}\n"
                    f"Vigencia: {dias} días ({fecha_ven.strftime('%d/%m/%Y')})\n"
                    f"Monto: ${precio}\n\n"
                    f"✅ Documento con 2 páginas unificadas\n\n"
                    f"⏰ TIMER ACTIVO (36 horas)"
                ),
                reply_markup=keyboard
            )
        else:
            await message_or_callback.answer_document(
                FSInputFile(pdf_unificado),
                caption=(
                    f"📋 PERMISO DE CIRCULACIÓN - CDMX (COMPLETO)\n"
                    f"Folio: {datos['folio']}\n"
                    f"Vigencia: {dias} días ({fecha_ven.strftime('%d/%m/%Y')})\n"
                    f"Monto: ${precio}\n\n"
                    f"✅ Documento con 2 páginas unificadas\n\n"
                    f"⏰ TIMER ACTIVO (36 horas)"
                ),
                reply_markup=keyboard
            )

        # Guardar en Supabase
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
            "user_id": user_id,
            "username": datos.get("username", "Sin username"),
            "dias_permiso": dias,
            "precio": precio
        }).execute()

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
            "user_id": user_id,
            "dias_permiso": dias,
            "precio": precio
        }).execute()

        # Iniciar timer
        await iniciar_timer_eliminacion(user_id, datos["folio"])

        # Instrucciones de pago
        await bot.send_message(
            user_id,
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"📅 Vigencia: {dias} días\n"
            f"💵 Monto: ${precio}\n"
            f"⏰ Tiempo límite: 36 horas\n\n"
            f"🏦 TRANSFERENCIA:\n"
            f"• Banco: AZTECA\n"
            f"• Titular: LIZBETH LAZCANO MOSCO\n"
            f"• Cuenta: 127180013037579543\n"
            f"• Concepto: Permiso {datos['folio']}\n\n"
            f"🏪 OXXO:\n"
            f"• Referencia: 2242170180385581\n"
            f"• Titular: LIZBETH LAZCANO MOSCO\n"
            f"• Monto: ${precio}\n\n"
            f"📸 Envía la foto del comprobante para validar.\n"
            f"⚠️ Si no pagas en 36 horas, el folio se elimina automáticamente.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        await bot.send_message(user_id, f"❌ Error generando documentación: {str(e)}\n\n📋 Para generar otro permiso use /chuleta")
        print(f"[ERROR] procesar_y_enviar_pdf: {e}")

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DE LA CIUDAD DE MÉXICO\n\n"
        "⏰ Tiempo límite: 36 horas\n\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer de 36 horas)"

    await message.answer(
        f"🚗 NUEVO PERMISO - CDMX\n\n"
        f"⏰ Plazo de pago: 36 horas"
        f"{mensaje_folios}\n\n"
        f"Primer paso: MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("AÑO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Formato inválido. Use 4 dígitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["user_id"] = message.from_user.id
    datos["username"] = message.from_user.username or "Sin username"

    # Generar folio
    try:
        datos["folio"] = obtener_siguiente_folio()
    except Exception as e:
        await message.answer(f"❌ ERROR generando folio: {str(e)}\n\n📋 Para generar otro permiso use /chuleta")
        await state.clear()
        return

    # Fecha actual
    hoy = datetime.now()
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    datos["fecha_obj"] = hoy

    # Guardar datos pendientes esperando selección de días
    datos_pendientes[message.from_user.id] = datos

    # Mostrar selector de días (predeterminado visual: 30)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 30 días - $374", callback_data=f"dias_30_{message.from_user.id}"),
            InlineKeyboardButton(text="60 días - $748",   callback_data=f"dias_60_{message.from_user.id}"),
            InlineKeyboardButton(text="90 días - $1,122", callback_data=f"dias_90_{message.from_user.id}"),
        ]
    ])

    await message.answer(
        f"📋 Folio: <b>{datos['folio']}</b>\n"
        f"👤 Titular: <b>{nombre}</b>\n\n"
        f"Selecciona la vigencia del permiso:\n"
        f"(Predeterminado: 30 días)",
        parse_mode="HTML",
        reply_markup=keyboard
    )

    await state.clear()

# ------------ CALLBACK: SELECCIÓN DE DÍAS ------------
@dp.callback_query(lambda c: c.data and c.data.startswith("dias_"))
async def callback_seleccion_dias(callback: CallbackQuery):
    partes = callback.data.split("_")
    # formato: dias_30_USERID  o  dias_60_USERID  o  dias_90_USERID
    if len(partes) != 3:
        await callback.answer("❌ Datos inválidos", show_alert=True)
        return

    dias = int(partes[1])
    user_id = int(partes[2])

    # Verificar que el que presiona sea el dueño del trámite
    if callback.from_user.id != user_id:
        await callback.answer("❌ Este botón no es para ti.", show_alert=True)
        return

    if user_id not in datos_pendientes:
        await callback.answer("❌ Los datos expiraron. Genera un nuevo permiso con /chuleta", show_alert=True)
        return

    datos = datos_pendientes.pop(user_id)

    # ✅ RESPONDER CALLBACK INMEDIATAMENTE (Telegram exige respuesta en <5s)
    await callback.answer(f"✅ {dias} días seleccionados", show_alert=False)

    # Actualizar mensaje con estado de generación
    precio = PRECIO_BASE * (dias // 30)
    try:
        await callback.message.edit_text(
            f"📋 Folio: <b>{datos['folio']}</b>\n"
            f"👤 Titular: <b>{datos['nombre']}</b>\n"
            f"📅 Vigencia: <b>{dias} días</b>\n"
            f"💵 Monto: <b>${precio}</b>\n\n"
            f"🔄 Generando documentación...",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"[WARN] No se pudo editar mensaje: {e}")

    # 🔥 Lanzar generación y envío como tarea en background
    # Así el webhook regresa de inmediato y no hay timeout
    chat_id = callback.message.chat.id
    asyncio.create_task(generar_y_enviar_background(chat_id, datos, dias))


async def generar_y_enviar_background(chat_id: int, datos: dict, dias: int):
    """Corre en background: genera PDF y lo manda. Sin bloquear el webhook."""
    user_id = datos["user_id"]
    precio = PRECIO_BASE * (dias // 30)
    hoy = datos["fecha_obj"]
    fecha_ven = hoy + timedelta(days=dias)

    try:
        pdf_path = generar_pdf_unificado(datos, dias)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔑 Validar Admin", callback_data=f"validar_{datos['folio']}"),
                InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{datos['folio']}")
            ]
        ])

        await bot.send_document(
            chat_id,
            FSInputFile(pdf_path),
            caption=(
                f"📋 PERMISO DE CIRCULACIÓN - CDMX (COMPLETO)\n"
                f"Folio: {datos['folio']}\n"
                f"Vigencia: {dias} días ({fecha_ven.strftime('%d/%m/%Y')})\n"
                f"Monto: ${precio}\n\n"
                f"✅ Documento con 2 páginas unificadas\n\n"
                f"⏰ TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        # Guardar en Supabase
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
            "user_id": user_id,
            "username": datos.get("username", "Sin username"),
            "dias_permiso": dias,
            "precio": precio
        }).execute()

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
            "user_id": user_id,
            "dias_permiso": dias,
            "precio": precio
        }).execute()

        await iniciar_timer_eliminacion(user_id, datos["folio"])

        await bot.send_message(
            user_id,
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"📅 Vigencia: {dias} días\n"
            f"💵 Monto: ${precio}\n"
            f"⏰ Tiempo límite: 36 horas\n\n"
            f"🏦 TRANSFERENCIA:\n"
            f"• Banco: AZTECA\n"
            f"• Titular: LIZBETH LAZCANO MOSCO\n"
            f"• Cuenta: 127180013037579543\n"
            f"• Concepto: Permiso {datos['folio']}\n\n"
            f"🏪 OXXO:\n"
            f"• Referencia: 2242170180385581\n"
            f"• Titular: LIZBETH LAZCANO MOSCO\n"
            f"• Monto: ${precio}\n\n"
            f"📸 Envía la foto del comprobante para validar.\n"
            f"⚠️ Si no pagas en 36 horas, el folio se elimina automáticamente.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        print(f"[ERROR] generar_y_enviar_background folio {datos.get('folio', '?')}: {e}")
        try:
            await bot.send_message(
                user_id,
                f"❌ Error generando el documento. Intenta de nuevo con /chuleta\n\nDetalle: {str(e)}"
            )
        except Exception:
            pass

# ------------ CALLBACK HANDLERS (BOTONES ADMIN) ------------
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")

    if not folio.startswith("122"):
        await callback.answer("❌ Folio inválido", show_alert=True)
        return

    if folio in timers_activos:
        user_con_folio = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)

        try:
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            supabase.table("borradores_registros").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")

        await callback.answer("✅ Folio validado por administración", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)

        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - CDMX\n"
                f"Folio: {folio}\n"
                f"Tu permiso está activo para circular.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando al usuario {user_con_folio}: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")

    if folio in timers_activos:
        cancelar_timer_folio(folio)

        try:
            supabase.table("folios_registrados").update({
                "estado": "TIMER_DETENIDO",
                "fecha_detencion": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")

        await callback.answer("⏹️ Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\n\n"
            f"Folio: {folio}\n"
            f"El timer de eliminación automática ha sido detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) > 4:
        folio_admin = texto[4:]

        if not folio_admin.startswith("122"):
            await message.answer(
                f"❌ FOLIO INVÁLIDO\n"
                f"El folio {folio_admin} no es CDMX.\n"
                f"Debe comenzar con 122\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            return

        if folio_admin in timers_activos:
            user_con_folio = timers_activos[folio_admin]["user_id"]
            cancelar_timer_folio(folio_admin)

            try:
                supabase.table("folios_registrados").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_comprobante": datetime.now().isoformat()
                }).eq("folio", folio_admin).execute()
                supabase.table("borradores_registros").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_comprobante": datetime.now().isoformat()
                }).eq("folio", folio_admin).execute()
            except Exception as e:
                print(f"Error actualizando BD para folio {folio_admin}: {e}")

            await message.answer(
                f"✅ VALIDACIÓN ADMINISTRATIVA OK\n"
                f"Folio: {folio_admin}\n"
                f"Timer cancelado y estado actualizado.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )

            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - CDMX\n"
                    f"Folio: {folio_admin}\n"
                    f"Tu permiso está activo para circular.\n\n"
                    f"📋 Para generar otro permiso use /chuleta"
                )
            except Exception as e:
                print(f"Error notificando al usuario {user_con_folio}: {e}")
        else:
            await message.answer(
                f"❌ FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\n"
                f"Folio consultado: {folio_admin}\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
    else:
        await message.answer(
            "⚠️ Formato: SERO[número_de_folio]\n"
            "Ejemplo: SERO1225\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )

@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)

        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay trámites pendientes de pago.\n\n"
                "📋 Para generar otro permiso use /chuleta"
            )
            return

        if len(folios_usuario) > 1:
            lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 Tienes varios folios activos:\n\n{lista_folios}\n\n"
                f"Responde con el NÚMERO DE FOLIO al que corresponde este comprobante.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            return

        folio = folios_usuario[0]
        cancelar_timer_folio(folio)

        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )

    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(f"❌ Error procesando el comprobante. Intenta enviar la foto nuevamente.\n\n📋 Para generar otro permiso use /chuleta")

@dp.message(lambda message: message.from_user.id in pending_comprobantes and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folio_especificado = message.text.strip().upper()
        folios_usuario = obtener_folios_usuario(user_id)

        if folio_especificado not in folios_usuario:
            await message.answer(
                "❌ Ese folio no está entre tus expedientes activos.\n"
                "Responde con uno de tu lista actual.\n\n"
                "📋 Para generar otro permiso use /chuleta"
            )
            return

        cancelar_timer_folio(folio_especificado)
        del pending_comprobantes[user_id]

        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            await message.answer(
                f"✅ Comprobante asociado.\n"
                f"📄 Folio: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"✅ Folio confirmado: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer(f"❌ Error procesando el folio especificado. Intenta de nuevo.\n\n📋 Para generar otro permiso use /chuleta")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)

        if not folios_usuario:
            await message.answer(
                "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
                "No tienes folios pendientes de pago.\n\n"
                "📋 Para generar otro permiso use /chuleta"
            )
            return

        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                tiempo_restante = 2160 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, tiempo_restante)
                horas = tiempo_restante // 60
                minutos = tiempo_restante % 60
                lista_folios.append(f"• {folio} ({horas}h {minutos}min restantes)")
            else:
                lista_folios.append(f"• {folio} (sin timer)")

        await message.answer(
            f"📋 FOLIOS CDMX ACTIVOS ({len(folios_usuario)})\n\n"
            + '\n'.join(lista_folios) +
            f"\n\n⏰ Cada folio tiene timer de 36 horas.\n"
            f"📸 Para enviar comprobante, use imagen.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer(f"❌ Error consultando expedientes activos.\n\n📋 Para generar otro permiso use /chuleta")

@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        "💰 INFORMACIÓN DE COSTOS\n\n"
        "• 30 días → $374\n"
        "• 60 días → $748\n"
        "• 90 días → $1,122\n\n"
        "📋 Para generar otro permiso use /chuleta"
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital CDMX.")

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema CDMX activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    try:
        inicializar_folio_desde_supabase()

        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            webhook_url = f"{BASE_URL}/webhook"
            await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
            print(f"[WEBHOOK] Configurado: {webhook_url}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Modo sin webhook")
        print("[SISTEMA] ¡Sistema Digital CDMX iniciado correctamente!")
        yield
    except Exception as e:
        print(f"[ERROR CRÍTICO] Iniciando sistema: {e}")
        yield
    finally:
        print("[CIERRE] Cerrando sistema...")
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema CDMX Digital", version="6.0")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def health():
    return {
        "ok": True,
        "bot": "CDMX Permisos Sistema",
        "status": "running",
        "version": "6.0 - Selector de días 30/60/90",
        "entidad": "CDMX",
        "vigencias": "30 / 60 / 90 días",
        "precios": {"30": "$374", "60": "$748", "90": "$1,122"},
        "timer_eliminacion": "36 horas",
        "active_timers": len(timers_activos),
        "prefijo_folio": "122",
        "siguiente_folio": f"122{folio_counter['siguiente']}"
    }

@app.get("/status")
async def status_detail():
    return {
        "sistema": "CDMX Digital v6.0 - Selector días inline",
        "entidad": "CDMX",
        "vigencias_disponibles": [30, 60, 90],
        "precio_base": PRECIO_BASE,
        "tiempo_eliminacion": "36 horas con avisos 90/60/30/10",
        "total_timers_activos": len(timers_activos),
        "folios_con_timer": list(timers_activos.keys()),
        "datos_pendientes_seleccion": list(datos_pendientes.keys()),
        "prefijo_folio": "122",
        "siguiente_folio": f"122{folio_counter['siguiente']}",
        "timestamp": datetime.now().isoformat(),
        "status": "Operacional"
    }

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        print(f"[SISTEMA] CDMX v6.0 - Selector días 30/60/90")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
