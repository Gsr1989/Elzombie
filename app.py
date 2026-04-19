from datetime import datetime, timedelta
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.client.session.aiohttp import AiohttpSession
from contextlib import asynccontextmanager, suppress
import asyncio
import aiohttp
import qrcode

# ------------ CONFIG ------------
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL     = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR   = "documentos"
PLANTILLA_PDF   = "cdmxdigital2025ppp.pdf"
PLANTILLA_BUENO = "elbueno.pdf"

PRECIO_PERMISO = 374
DIAS_PERMISO   = 30

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT (timeout 180s) ------------
_bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=180))
bot     = Bot(token=BOT_TOKEN, session=_bot_session)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ------------ TIMERS ------------
timers_activos       = {}
user_folios          = {}
pending_comprobantes = {}

# ------------ FOLIO 122 ------------
FOLIO_PREFIJO      = "122"
folio_counter      = {"siguiente": 1}   # candidato actual; sube +1 en cada búsqueda
MAX_INTENTOS_FOLIO = 100_000

# ── Supabase síncrono ────────────────────────────────────────────────────────

def _sb_folio_existe(folio: str) -> bool:
    """Devuelve True si el folio ya existe en folios_registrados."""
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except Exception as e:
        print(f"[ERROR] Verificando folio {folio}: {e}")
        return False          # ante la duda asumimos libre para no bloquear


def _sb_obtener_siguiente_folio() -> str:
    """
    Busca el siguiente folio libre partiendo de folio_counter['siguiente'].
    Sube de uno en uno (+1) hasta encontrar un hueco vacío en Supabase.
    Al asignar deja el contador en candidato+1 para la próxima llamada.

    Ejemplo:
        BD tiene: 1221, 1222, 1223
        contador arranca en 1224 → libre → asigna 1224, deja contador en 1225

        Si BD tiene: 1221, 1222, 1223, 1224
        prueba 1224 → ocupado
        prueba 1225 → libre → asigna 1225, deja contador en 1226
    """
    candidato = folio_counter["siguiente"]

    for _ in range(MAX_INTENTOS_FOLIO):
        folio = f"{FOLIO_PREFIJO}{candidato}"
        if not _sb_folio_existe(folio):
            folio_counter["siguiente"] = candidato + 1
            print(f"[FOLIO] Asignado: {folio}  (siguiente candidato: {folio_counter['siguiente']})")
            return folio
        print(f"[FOLIO] {folio} ocupado → probando {FOLIO_PREFIJO}{candidato + 1}")
        candidato += 1

    raise Exception("No se pudo generar folio único — límite alcanzado")


def _sb_inicializar_folio():
    """
    Al arrancar lee TODOS los folios del prefijo 122 en Supabase,
    calcula el máximo consecutivo ya usado y arranca desde max+1.
    Así nunca pisamos un folio existente aunque el servidor se reinicie.
    """
    try:
        r = (
            supabase
            .table("folios_registrados")
            .select("folio")
            .like("folio", f"{FOLIO_PREFIJO}%")
            .execute()
        )
        if r.data:
            consecutivos = []
            for row in r.data:
                f = row.get("folio", "")
                if isinstance(f, str) and f.startswith(FOLIO_PREFIJO):
                    sufijo = f[len(FOLIO_PREFIJO):]
                    if sufijo.isdigit():
                        consecutivos.append(int(sufijo))

            if consecutivos:
                maximo = max(consecutivos)
                folio_counter["siguiente"] = maximo + 1
                print(f"[INFO] Folio inicializado — máximo encontrado: {FOLIO_PREFIJO}{maximo} "
                      f"→ siguiente candidato: {folio_counter['siguiente']}")
                return

        folio_counter["siguiente"] = 1
        print("[INFO] Sin folios 122 previos — empezando desde 1221")
    except Exception as e:
        print(f"[ERROR] inicializar_folio: {e}")
        folio_counter["siguiente"] = 1

# ── Wrappers async ────────────────────────────────────────────────────────────

async def obtener_siguiente_folio() -> str:
    return await asyncio.to_thread(_sb_obtener_siguiente_folio)

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ------------ TIMERS ------------
async def eliminar_folio_automatico(folio: str):
    try:
        uid = timers_activos.get(folio, {}).get("user_id")
        await asyncio.to_thread(
            lambda: supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        )
        await asyncio.to_thread(
            lambda: supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        )
        if uid:
            await bot.send_message(uid,
                f"TIEMPO AGOTADO - CDMX\n\n"
                f"Folio {folio} eliminado por no pagar en 36h.\n\n"
                f"Use /chuleta para generar otro.")
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"[ERROR] eliminar_folio {folio}: {e}")


async def enviar_recordatorio(folio: str, minutos: int):
    try:
        uid = timers_activos.get(folio, {}).get("user_id")
        if not uid:
            return
        await bot.send_message(uid,
            f"RECORDATORIO - CDMX\n\n"
            f"Folio: {folio}\nTiempo restante: {minutos} min\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"Envie comprobante de pago.\n\n"
            f"Use /chuleta para generar otro.")
    except Exception as e:
        print(f"[ERROR] recordatorio {folio}: {e}")


async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def _run():
        print(f"[TIMER] 36h iniciado - folio {folio}")
        await asyncio.sleep(34.5 * 3600)
        for mins, sleep_seg in [(90, 1800), (60, 1800), (30, 1200), (10, 600)]:
            if folio not in timers_activos:
                return
            await enviar_recordatorio(folio, mins)
            await asyncio.sleep(sleep_seg)
        if folio in timers_activos:
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(_run())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer iniciado {folio}, total activos: {len(timers_activos)}")


def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        uid = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if uid in user_folios:
            user_folios[uid] = [f for f in user_folios[uid] if f != folio]
            if not user_folios[uid]:
                del user_folios[uid]
        print(f"[SISTEMA] Timer cancelado: {folio}")


def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if uid in user_folios:
            user_folios[uid] = [f for f in user_folios[uid] if f != folio]
            if not user_folios[uid]:
                del user_folios[uid]

# ------------ QR ------------
URL_CONSULTA_BASE = "https://semovidigitalgob.onrender.com"

def _generar_qr_cdmx(folio: str):
    """Síncrono – llamar con asyncio.to_thread."""
    try:
        url = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                             box_size=4, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR] {folio} -> {url}")
        return img
    except Exception as e:
        print(f"[ERROR QR] {e}")
        return None

# ------------ GENERACION PDF (síncrono, llamar con to_thread) ----------------

def _generar_pdf_unificado(datos: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename  = f"{OUTPUT_DIR}/{datos['folio']}_completo.pdf"
    hoy       = datos["fecha_obj"]
    fecha_ven = hoy + timedelta(days=DIAS_PERMISO)
    anio_str  = str(hoy.year)

    try:
        # =====================================================================
        # PAGINA 1
        # =====================================================================
        doc1  = fitz.open(PLANTILLA_PDF)
        page1 = doc1[0]

        page1.insert_text((50,  130), "FOLIO: ",                     fontsize=12, color=(0,0,0))
        page1.insert_text((100, 130), datos["folio"],                 fontsize=12, color=(1,0,0))
        page1.insert_text((130, 145), datos["fecha"],                 fontsize=12, color=(0,0,0))
        page1.insert_text((87,  290), datos["marca"],                 fontsize=11, color=(0,0,0))
        page1.insert_text((375, 290), datos["serie"],                 fontsize=11, color=(0,0,0))
        page1.insert_text((87,  307), datos["linea"],                 fontsize=11, color=(0,0,0))
        page1.insert_text((375, 307), datos["motor"],                 fontsize=11, color=(0,0,0))
        page1.insert_text((87,  323), datos["anio"],                  fontsize=11, color=(0,0,0))
        page1.insert_text((375, 323), fecha_ven.strftime("%d/%m/%Y"), fontsize=11, color=(0,0,0))
        page1.insert_text((375, 340), datos["nombre"],                fontsize=11, color=(0,0,0))

        img_qr = _generar_qr_cdmx(datos["folio"])
        if img_qr:
            from io import BytesIO
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            page1.insert_image(fitz.Rect(49, 653, 145, 749), pixmap=qr_pix, overlay=True)
            print("[QR] Insertado en pagina 1")

        # =====================================================================
        # PAGINA 2 — coordenadas explícitas igual que página 1
        # =====================================================================
        doc2  = fitz.open(PLANTILLA_BUENO)
        page2 = doc2[0]

        titulo = (f"IMPUESTO POR DERECHO DE AUTOMOVIL Y MOTOCICLETAS "
                  f"(PERMISO PARA CIRCULAR {DIAS_PERMISO} DIAS)")
        page2.insert_text((135, 170), titulo,                          fontsize=6, fontname="hebo", color=(0,0,0))
        page2.insert_text((135, 194), datos["serie"],                  fontsize=6, fontname="hebo", color=(0,0,0))
        page2.insert_text((135, 202), anio_str,                        fontsize=6, fontname="hebo", color=(0,0,0))
        page2.insert_text((385, 416), f"${PRECIO_PERMISO}",            fontsize=16, fontname="hebo", color=(0,0,0))
        page2.insert_text((190, 324), hoy.strftime("%d/%m/%Y"),        fontsize=6, fontname="hebo", color=(0,0,0))
        
        # =====================================================================
        # UNIR PÁGINAS
        # =====================================================================
        doc1.insert_pdf(doc2)
        doc2.close()
        doc1.save(filename)
        doc1.close()

        print(f"[PDF] Generado: {filename}")

    except Exception as e:
        print(f"[ERROR PDF] {e}")
        fb = fitz.open()
        fb.new_page().insert_text((50, 50), f"ERROR - {datos['folio']}", fontsize=12)
        fb.save(filename)
        fb.close()

    return filename

# ------------ SEND CON RETRY ------------
async def send_document_con_retry(chat_id: int, path: str, caption: str,
                                   reply_markup, reintentos: int = 3) -> bool:
    for intento in range(1, reintentos + 1):
        try:
            await bot.send_document(
                chat_id,
                FSInputFile(path),
                caption=caption,
                reply_markup=reply_markup
            )
            print(f"[SEND] Documento enviado (intento {intento})")
            return True
        except Exception as e:
            print(f"[SEND] Intento {intento} fallido: {e}")
            if intento < reintentos:
                await asyncio.sleep(5)
    return False

# ------------ BACKGROUND: genera y manda PDF ---------------------------------
async def generar_y_enviar_background(chat_id: int, datos: dict):
    user_id   = datos["user_id"]
    hoy       = datos["fecha_obj"]
    fecha_ven = hoy + timedelta(days=DIAS_PERMISO)

    try:
        pdf_path = await asyncio.to_thread(_generar_pdf_unificado, datos)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Validar Admin",  callback_data=f"validar_{datos['folio']}"),
            InlineKeyboardButton(text="Detener Timer",  callback_data=f"detener_{datos['folio']}")
        ]])

        caption = (
            f"PERMISO DE CIRCULACION - CDMX\n"
            f"Folio: {datos['folio']}\n"
            f"Vigencia: {DIAS_PERMISO} dias ({fecha_ven.strftime('%d/%m/%Y')})\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"Documento con 2 paginas\n"
            f"TIMER ACTIVO (36 horas)"
        )

        ok = await send_document_con_retry(chat_id, pdf_path, caption, keyboard)

        if not ok:
            await bot.send_message(user_id,
                f"No se pudo enviar el documento (fallo de red).\n"
                f"Folio generado: {datos['folio']}\n"
                f"Use /chuleta para reintentar.")
            return

        def _insert_supabase():
            supabase.table("folios_registrados").insert({
                "folio":             datos["folio"],
                "marca":             datos["marca"],
                "linea":             datos["linea"],
                "anio":              datos["anio"],
                "numero_serie":      datos["serie"],
                "numero_motor":      datos["motor"],
                "nombre":            datos["nombre"],
                "fecha_expedicion":  hoy.date().isoformat(),
                "fecha_vencimiento": fecha_ven.date().isoformat(),
                "entidad":           "cdmx",
                "estado":            "PENDIENTE",
                "user_id":           user_id,
                "username":          datos.get("username", "Sin username"),
            }).execute()
            supabase.table("borradores_registros").insert({
                "folio":             datos["folio"],
                "entidad":           "CDMX",
                "numero_serie":      datos["serie"],
                "marca":             datos["marca"],
                "linea":             datos["linea"],
                "numero_motor":      datos["motor"],
                "anio":              datos["anio"],
                "fecha_expedicion":  hoy.isoformat(),
                "fecha_vencimiento": fecha_ven.isoformat(),
                "contribuyente":     datos["nombre"],
                "estado":            "PENDIENTE",
                "user_id":           user_id,
            }).execute()

        await asyncio.to_thread(_insert_supabase)
        await iniciar_timer_eliminacion(user_id, datos["folio"])

        await bot.send_message(user_id,
            f"INSTRUCCIONES DE PAGO\n\n"
            f"Folio: {datos['folio']}\n"
            f"Vigencia: {DIAS_PERMISO} dias\n"
            f"Monto: ${PRECIO_PERMISO}\n"
            f"Tiempo limite: 36 horas\n\n"
            f"TRANSFERENCIA:\n"
            f"Banco: AZTECA\n"
            f"Titular: LIZBETH LAZCANO MOSCO\n"
            f"Cuenta: 127180013037579543\n"
            f"Concepto: Permiso {datos['folio']}\n\n"
            f"OXXO:\n"
            f"Referencia: 2242170180385581\n"
            f"Titular: LIZBETH LAZCANO MOSCO\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"Envia la foto del comprobante para validar.\n"
            f"Sin pago en 36h el folio se elimina.\n\n"
            f"Para generar otro permiso use /chuleta")

    except Exception as e:
        print(f"[ERROR] generar_y_enviar_background folio {datos.get('folio','?')}: {e}")
        try:
            await bot.send_message(user_id,
                f"Error al generar el documento: {e}\n\nUse /chuleta para reintentar.")
        except Exception:
            pass

# ------------ FSM ------------
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    nombre = State()

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "SISTEMA DIGITAL DE LA CIUDAD DE MEXICO\n\n"
        f"Costo: ${PRECIO_PERMISO}\n"
        "Tiempo limite: 36 horas\n\n"
        "Su folio se elimina si no paga en 36 horas.")

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    folios = obtener_folios_usuario(message.from_user.id)
    extra  = f"\n\nFOLIOS ACTIVOS: {', '.join(folios)}" if folios else ""
    await message.answer(
        f"NUEVO PERMISO - CDMX\n\n"
        f"Costo: ${PRECIO_PERMISO}\n"
        f"Plazo de pago: 36 horas{extra}\n\n"
        f"Primer paso: MARCA del vehiculo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LINEA/MODELO del vehiculo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("ANIO del vehiculo (4 digitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("Formato invalido. Use 4 digitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("NUMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("NUMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos             = await state.get_data()
    datos["nombre"]   = message.text.strip().upper()
    datos["user_id"]  = message.from_user.id
    datos["username"] = message.from_user.username or "Sin username"

    try:
        datos["folio"] = await obtener_siguiente_folio()
    except Exception as e:
        await message.answer(f"ERROR generando folio: {e}\n\nUse /chuleta")
        await state.clear()
        return

    hoy = datetime.now()
    meses = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
             7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"}
    datos["fecha"]     = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    datos["fecha_obj"] = hoy

    await state.clear()

    await message.answer(
        f"Folio: <b>{datos['folio']}</b>\n"
        f"Titular: <b>{datos['nombre']}</b>\n"
        f"Vigencia: <b>{DIAS_PERMISO} dias</b>\n"
        f"Monto: <b>${PRECIO_PERMISO}</b>\n\n"
        f"Generando documentacion...",
        parse_mode="HTML")

    asyncio.create_task(generar_y_enviar_background(message.chat.id, datos))

# ------------ CALLBACKS ADMIN ------------
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if not folio.startswith("122"):
        await callback.answer("Folio invalido", show_alert=True)
        return
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute()
            ))
        except Exception as e:
            print(f"[ERROR] BD validar {folio}: {e}")
        await callback.answer("Folio validado", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(uid,
                f"PAGO VALIDADO - CDMX\n"
                f"Folio: {folio}\nPermiso activo para circular.\n\n"
                f"Use /chuleta para generar otro.")
        except Exception as e:
            print(f"[ERROR] Notificar: {e}")
    else:
        await callback.answer("Folio no encontrado en timers", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        try:
            await asyncio.to_thread(lambda:
                supabase.table("folios_registrados").update(
                    {"estado": "TIMER_DETENIDO", "fecha_detencion": datetime.now().isoformat()}
                ).eq("folio", folio).execute()
            )
        except Exception as e:
            print(f"[ERROR] BD detener {folio}: {e}")
        await callback.answer("Timer detenido", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"TIMER DETENIDO\nFolio: {folio}\n\nUse /chuleta para generar otro.")
    else:
        await callback.answer("Timer ya no activo", show_alert=True)

# ------------ ADMIN POR TEXTO (SERO) ------------
@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) <= 4:
        await message.answer("Formato: SERO[folio]  Ejemplo: SERO1225")
        return
    folio = texto[4:]
    if not folio.startswith("122"):
        await message.answer(f"Folio {folio} no es CDMX (debe iniciar con 122)")
        return
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute()
            ))
        except Exception as e:
            print(f"[ERROR] BD SERO {folio}: {e}")
        await message.answer(f"VALIDACION OK\nFolio: {folio}\nTimer cancelado.")
        try:
            await bot.send_message(uid,
                f"PAGO VALIDADO - CDMX\n"
                f"Folio: {folio}\nPermiso activo.\n\n"
                f"Use /chuleta para generar otro.")
        except Exception as e:
            print(f"[ERROR] Notificar: {e}")
    else:
        await message.answer(f"Folio {folio} no encontrado en timers activos.")

# ------------ COMPROBANTE FOTO ------------
@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid    = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer("No hay tramites pendientes.\n\nUse /chuleta para generar uno.")
        return
    if len(folios) > 1:
        pending_comprobantes[uid] = "waiting_folio"
        lista = "\n".join(f"- {f}" for f in folios)
        await message.answer(f"Folios activos:\n{lista}\n\nResponde con el FOLIO de este comprobante.")
        return
    folio = folios[0]
    cancelar_timer_folio(folio)
    try:
        now = datetime.now().isoformat()
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio).execute(),
            supabase.table("borradores_registros").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio).execute()
        ))
    except Exception as e:
        print(f"[ERROR] comprobante {folio}: {e}")
    await message.answer(f"Comprobante recibido.\nFolio: {folio}\nTimer detenido.\n\nUse /chuleta para generar otro.")

@dp.message(lambda m: m.from_user.id in pending_comprobantes
            and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    uid    = message.from_user.id
    folio  = message.text.strip().upper()
    folios = obtener_folios_usuario(uid)
    if folio not in folios:
        await message.answer("Folio no esta en tu lista. Escribe uno de tu lista.")
        return
    cancelar_timer_folio(folio)
    del pending_comprobantes[uid]
    try:
        now = datetime.now().isoformat()
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio).execute(),
            supabase.table("borradores_registros").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio).execute()
        ))
    except Exception as e:
        print(f"[ERROR] comprobante asociado {folio}: {e}")
    await message.answer(f"Comprobante asociado.\nFolio: {folio}\nTimer detenido.\n\nUse /chuleta para generar otro.")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    uid    = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer("No hay folios activos.\n\nUse /chuleta para generar uno.")
        return
    lineas = []
    for f in folios:
        if f in timers_activos:
            mins = max(0, 2160 - int((datetime.now() - timers_activos[f]["start_time"]).total_seconds() / 60))
            lineas.append(f"- {f} ({mins//60}h {mins%60}min restantes)")
        else:
            lineas.append(f"- {f} (sin timer)")
    await message.answer(
        f"FOLIOS CDMX ACTIVOS ({len(folios)})\n\n" + "\n".join(lineas) +
        "\n\nEnvia imagen para comprobante.\nUse /chuleta para generar otro.")

@dp.message(lambda m: m.text and any(p in m.text.lower() for p in
            ['costo','precio','cuanto','cuánto','deposito','depósito','pago','valor','monto']))
async def responder_costo(message: types.Message):
    await message.answer(f"Costo del permiso: ${PRECIO_PERMISO} (30 dias)\n\nUse /chuleta para generar uno.")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("Sistema Digital CDMX.")

# ------------ FASTAPI ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema CDMX activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    try:
        await asyncio.to_thread(_sb_inicializar_folio)
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            wh = f"{BASE_URL}/webhook"
            await bot.set_webhook(wh, allowed_updates=["message", "callback_query"])
            print(f"[WEBHOOK] {wh}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Sin webhook")
        print("[SISTEMA] CDMX v7.2 iniciado!")
        yield
    except Exception as e:
        print(f"[ERROR CRITICO] {e}")
        yield
    finally:
        print("[CIERRE] Cerrando...")
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema CDMX Digital", version="7.2")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data   = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def health():
    return {
        "ok":              True,
        "sistema":         "CDMX v7.2",
        "vigencia":        f"{DIAS_PERMISO} dias fijos",
        "precio":          f"${PRECIO_PERMISO}",
        "timer":           "36 horas",
        "active_timers":   len(timers_activos),
        "siguiente_folio": f"{FOLIO_PREFIJO}{folio_counter['siguiente']}",
        "fixes_v7.2": [
            "Folio: busqueda +1 puro hasta el infinito sin saltos raros",
            "Inicializacion: lee TODOS los folios 122x, toma el maximo real",
            "Si folio ocupado -> +1 -> +1 -> ... hasta encontrar libre",
            "asyncio.to_thread en PDF, QR y Supabase mantenido",
            "pagina2: coordenadas explicitas igual que pagina1",
        ]
    }

@app.get("/status")
async def status_detail():
    return {
        "sistema":         "CDMX v7.2",
        "timers_activos":  len(timers_activos),
        "folios_activos":  list(timers_activos.keys()),
        "siguiente_folio": f"{FOLIO_PREFIJO}{folio_counter['siguiente']}",
        "timestamp":       datetime.now().isoformat(),
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
