# app.py — Bot de Telegram NUCLEAR VERSION (que SÍ funciona en Render)

import os, re, time, asyncio, unicodedata, qrcode, logging, requests
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib import colors
from supabase import create_client
import json

# Variables de entorno
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
FOLIO_PREFIX = os.getenv("FOLIO_PREFIX", "05")
BUCKET = os.getenv("BUCKET", "pdfs")
FLOW_TTL = int(os.getenv("FLOW_TTL", "300"))
OUTPUT_DIR = "/tmp/pdfs"
TABLE_FOLIOS = "folios_unicos"
TABLE_REGISTROS = "borradores_registros"

# Inicialización
os.makedirs(OUTPUT_DIR, exist_ok=True)
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
logging.basicConfig(level=logging.INFO)

# Estados del bot
MARCA, LINEA, ANIO, SERIE, MOTOR, NOMBRE = range(6)

# Candados anti-spam
ACTIVE = {}
def _now(): return time.time()
def lock_acquire(cid): return not ACTIVE.get(cid, 0) > _now() and not ACTIVE.update({cid: _now()+FLOW_TTL})
def lock_bump(cid): ACTIVE[cid] = _now() + FLOW_TTL
def lock_release(cid): ACTIVE.pop(cid, None)

# Utilidades
def _slug(s):
    nfkd = unicodedata.normalize("NFKD", s or "")
    s2 = "".join(c for c in nfkd if not unicodedata.combining(c)).replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s2)

def nuevo_folio(prefix=FOLIO_PREFIX):
    ins = supabase.table(TABLE_FOLIOS).insert({"prefijo": prefix, "entidad": "CDMX"}).execute()
    nid = int(ins.data[0]["id"])
    folio = f"{prefix}{nid:06d}"
    try:
        supabase.table(TABLE_FOLIOS).update({"fol": folio}).match({"id": nid}).execute()
    except: 
        pass
    return folio

def _make_pdf(datos):
    """Crear PDF usando reportlab (más estable que PyMuPDF)"""
    out_path = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_cdmx.pdf")
    
    # Crear PDF
    c = canvas.Canvas(out_path, pagesize=letter)
    width, height = letter
    
    # Título
    c.setFont("Helvetica-Bold", 20)
    c.setFillColor(colors.red)
    c.drawString(100, height - 100, f"PERMISO DE CIRCULACIÓN - FOLIO: {datos['folio']}")
    
    # Fecha
    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)
    fecha_visual = fecha_exp.strftime("%d DE %B DE %Y").upper()
    vigencia_visual = fecha_ven.strftime("%d/%m/%Y")
    
    c.setFont("Helvetica", 12)
    c.setFillColor(colors.black)
    c.drawString(100, height - 140, f"Fecha de expedición: {fecha_visual}")
    c.drawString(100, height - 160, f"Vigencia hasta: {vigencia_visual}")
    
    # Datos del vehículo
    y_pos = height - 200
    c.setFont("Helvetica-Bold", 14)
    c.drawString(100, y_pos, "DATOS DEL VEHÍCULO:")
    
    y_pos -= 30
    c.setFont("Helvetica", 12)
    c.drawString(100, y_pos, f"Marca: {datos.get('marca', '')}")
    y_pos -= 25
    c.drawString(100, y_pos, f"Línea: {datos.get('linea', '')}")
    y_pos -= 25
    c.drawString(100, y_pos, f"Año: {datos.get('anio', '')}")
    y_pos -= 25
    c.drawString(100, y_pos, f"Serie: {datos.get('serie', '')}")
    y_pos -= 25
    c.drawString(100, y_pos, f"Motor: {datos.get('motor', '')}")
    y_pos -= 25
    c.drawString(100, y_pos, f"Propietario: {datos.get('nombre', '')}")
    
    # Generar QR
    qr_text = (
        f"Folio: {datos['folio']}\n"
        f"Marca: {datos.get('marca','')}\n"
        f"Línea: {datos.get('linea','')}\n"
        f"Año: {datos.get('anio','')}\n"
        f"Serie: {datos.get('serie','')}\n"
        f"Motor: {datos.get('motor','')}\n"
        f"Nombre: {datos.get('nombre','')}\n"
        f"SEMOVICДMX DIGITAL"
    )
    
    qr = qrcode.make(qr_text)
    qr_path = os.path.join(OUTPUT_DIR, f"{_slug(datos['folio'])}_qr.png")
    qr.save(qr_path)
    
    # Insertar QR en PDF
    c.drawImage(qr_path, width - 200, y_pos - 150, width=120, height=120)
    
    # Texto final
    c.setFont("Helvetica-Bold", 10)
    c.drawString(100, 100, "GOBIERNO DE LA CIUDAD DE MÉXICO")
    c.drawString(100, 85, "SECRETARÍA DE MOVILIDAD")
    c.drawString(100, 70, "Este documento es válido únicamente durante el período indicado")
    
    c.save()
    return out_path

def _upload_pdf(path_local, nombre_pdf):
    """Subir PDF a Supabase usando requests"""
    nombre_pdf = _slug(nombre_pdf).lstrip("/")
    with open(path_local, "rb") as f:
        data = f.read()
    
    try:
        supabase.storage.from_(BUCKET).upload(nombre_pdf, data)
    except:
        # Si falla, intentar de nuevo
        supabase.storage.from_(BUCKET).upload(nombre_pdf, data)
    
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{nombre_pdf}"

# Handlers del bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lock_release(update.effective_chat.id)
    await update.message.reply_text("👋 Bot listo. Usa /permiso para iniciar.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lock_release(update.effective_chat.id)
    await update.message.reply_text("❎ Flujo cancelado.")
    return ConversationHandler.END

async def permiso_init(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not lock_acquire(chat_id):
        await update.message.reply_text("⏳ Espera unos minutos.")
        return ConversationHandler.END
    
    await update.message.reply_text("🚗 Marca del vehículo:")
    return MARCA

async def step_marca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lock_bump(update.effective_chat.id)
    context.user_data['marca'] = update.message.text.strip()
    await update.message.reply_text("📱 Línea:")
    return LINEA

async def step_linea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lock_bump(update.effective_chat.id)
    context.user_data['linea'] = update.message.text.strip()
    await update.message.reply_text("📅 Año (4 dígitos):")
    return ANIO

async def step_anio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit() or len(txt) != 4:
        await update.message.reply_text("❌ Año inválido. Intenta de nuevo:")
        return ANIO
    
    lock_bump(update.effective_chat.id)
    context.user_data['anio'] = txt
    await update.message.reply_text("🔢 Serie:")
    return SERIE

async def step_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lock_bump(update.effective_chat.id)
    context.user_data['serie'] = update.message.text.strip()
    await update.message.reply_text("🔧 Motor:")
    return MOTOR

async def step_motor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lock_bump(update.effective_chat.id)
    context.user_data['motor'] = update.message.text.strip()
    await update.message.reply_text("👤 Nombre del contribuyente:")
    return NOMBRE

async def step_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['nombre'] = update.message.text.strip()
    
    try:
        folio = nuevo_folio()
        context.user_data['folio'] = folio
        
        await update.message.reply_text("📄 Generando PDF...")
        
        path = _make_pdf(context.user_data)
        nombre_pdf = f"{folio}_{int(time.time())}.pdf"
        url_pdf = _upload_pdf(path, nombre_pdf)
        
        # Enviar PDF
        with open(path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                caption=f"✅ PDF listo\nFolio: {folio}\n🔗 {url_pdf}"
            )
        
        # Guardar en base de datos
        supabase.table(TABLE_REGISTROS).insert({
            "folio": folio,
            "marca": context.user_data["marca"],
            "linea": context.user_data["linea"], 
            "anio": context.user_data["anio"],
            "numero_serie": context.user_data["serie"],
            "numero_motor": context.user_data["motor"],
            "nombre": context.user_data["nombre"],
            "entidad": "CDMX",
            "url_pdf": url_pdf,
            "fecha_expedicion": datetime.now().date().isoformat(),
            "fecha_vencimiento": (datetime.now().date() + timedelta(days=30)).isoformat(),
        }).execute()
        
        await update.message.reply_text("🎉 ¡Listo! Usa /permiso para otro.")
        
    except Exception as e:
        logging.exception("❌ Error generando permiso")
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        lock_release(update.effective_chat.id)
        return ConversationHandler.END

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Usa /permiso para iniciar")

# Configurar bot
def create_app():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("permiso", permiso_init)],
        states={
            MARCA: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_marca)],
            LINEA: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_linea)],
            ANIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_anio)],
            SERIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_serie)],
            MOTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_motor)],
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_nombre)],
        },
        fallbacks=[CommandHandler(["cancel", "stop"], cancel)],
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT, fallback))
    
    return application

# FastAPI app
app = FastAPI()
telegram_app = create_app()

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(f"{BASE_URL}/webhook")

@app.on_event("shutdown") 
async def shutdown():
    await telegram_app.bot.delete_webhook()
    await telegram_app.shutdown()

@app.get("/")
async def health():
    return {"ok": True, "status": "Bot funcionando"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
