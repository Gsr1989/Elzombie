# app.py
import os
import time
import json
import asyncio
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse
import httpx

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Supabase
from supabase import create_client, Client

# Opcional para recargar schema (si proporcionas DATABASE_URL)
try:
    import psycopg2
except Exception:
    psycopg2 = None  # si no está instalado, seguimos sin NOTIFY

# ========= Config =========
APP_URL = os.getenv("APP_URL", "").rstrip("/")
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # service key
DATABASE_URL = os.getenv("DATABASE_URL", "")       # opcional para NOTIFY pgrst
FOLIO_PREFIX = os.getenv("FOLIO_PREFIX", "05")
FOLIO_COLUMN = os.getenv("FOLIO_COLUMN", "fol")    # tu tabla usa 'fol' (no 'folio') según tus capturas

TELEGRAM_API = f"https://api.telegram.org/bot{TG_TOKEN}"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Nombres de tablas (ajusta si los cambiaste)
TABLE_FOLIOS = "folios_unicos"

# ========= App =========
app = FastAPI(title="permiso-bot")

# ------------ Utils -------------
def now_ts() -> int:
    return int(time.time())

def build_pdf_and_get_url(folio: str) -> str:
    """
    Aquí va tu generación real de PDF y subida a Storage.
    De momento devolvemos una URL "dummy" para que el flujo no se rompa.
    """
    # TODO: reemplaza por tu lógica real de PDF + Supabase Storage
    return f"https://storage.example/pdfs/{folio}.pdf"

def reload_postgrest_schema() -> None:
    """
    NOTIFY pgrst, 'reload schema';
    Solo si DATABASE_URL está disponible y psycopg2 instalado.
    """
    if not (DATABASE_URL and psycopg2):
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("NOTIFY pgrst, 'reload schema';")
        conn.close()
        print("==> PostgREST schema reloaded")
    except Exception as e:
        print("WARN: failed to NOTIFY pgrst:", repr(e))

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=0.5, max=6),
    retry=retry_if_exception_type(Exception),
)
def supabase_insert_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inserta en Supabase especificando columnas (evita problemas con caché).
    Si ve el error de caché (PGRST204) reintenta tras recargar el schema.
    """
    try:
        res = supabase.table(TABLE_FOLIOS).insert(row).execute()
        return res.data or {}
    except Exception as e:
        msg = str(e)
        # Cuando cambiaste columnas y PostgREST no las ve aún:
        if "PGRST204" in msg or "cache" in msg or "No se pudo encontrar la columna" in msg:
            reload_postgrest_schema()
        raise

async def tg_send(chat_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})

async def tg_set_webhook() -> Dict[str, Any]:
    url = f"{APP_URL}/webhook"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{TELEGRAM_API}/setWebhook", data={
            "url": url,
            "allowed_updates": json.dumps(["message"]),
            "drop_pending_updates": "true",
        })
        return r.json()

def compute_dates() -> tuple[int, int]:
    """
    Ajusta a tus reglas. Aquí: expedición = ahora, vencimiento = ahora + 30 días.
    """
    now = now_ts()
    venc = now + 30 * 24 * 3600
    return now, venc

def make_next_folio(n: int) -> str:
    # FOLIO_PREFIX + 6 dígitos: ej 05 000043 -> 05000043
    return f"{FOLIO_PREFIX}{n:06d}"

def extract_text(payload: Dict[str, Any]) -> tuple[Optional[int], str]:
    msg = (payload.get("message") or {})
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    return chat_id, text

# ------------ Startup -------------
@app.on_event("startup")
async def on_startup():
    # Intenta recargar schema en arranque (por si hubo migraciones)
    reload_postgrest_schema()
    # Opcional: setear webhook automáticamente si APP_URL está definido
    if APP_URL:
        try:
            resp = await tg_set_webhook()
            print("setWebhook:", resp)
        except Exception as e:
            print("WARN setWebhook:", repr(e))

# ------------ Health -------------
@app.get("/health")
async def health():
    return {"ok": True, "ts": now_ts()}

# ------------ Set webhook manual -------------
@app.get("/set-webhook")
async def set_webhook():
    if not APP_URL:
        return JSONResponse({"ok": False, "error": "APP_URL vacío"}, status_code=400)
    try:
        data = await tg_set_webhook()
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"ok": False, "error": repr(e)}, status_code=500)

# ------------ Telegram webhook -------------
@app.post("/webhook", response_class=PlainTextResponse)
async def webhook(req: Request, bg: BackgroundTasks):
    try:
        payload = await req.json()
    except Exception:
        return PlainTextResponse("bad json", status_code=400)

    # Responde rápido; procesa en background
    bg.add_task(process_update, payload)
    return PlainTextResponse("ok", status_code=200)

# ------------ Lógica del bot -------------
async def process_update(payload: Dict[str, Any]) -> None:
    chat_id, text = extract_text(payload)
    if not chat_id or not text:
        return

    if text in ("/permiso", "/permisos"):
        await tg_send(chat_id, "⏳ Generando tu permiso…")

        # 1) Consecutivo: usamos el timestamp mod grande (simple y libre de bloqueo).
        #    Si quieres 100% sin colisiones, crea una secuencia en DB y léela aquí.
        correlativo = now_ts() % 10_000_000  # 0..9,999,999
        folio = make_next_folio(correlativo)

        fecha_exp, fecha_ven = compute_dates()
        url_pdf = build_pdf_and_get_url(folio)

        # Inserta SOLO columnas existentes (según tus capturas)
        row = {
            "prefijo": FOLIO_PREFIX,
            FOLIO_COLUMN: folio,
            "entidad": "CDMX",
            "fecha_expedicion": fecha_exp,
            "fecha_vencimiento": fecha_ven,
            "url_pdf": url_pdf,
        }

        try:
            supabase_insert_row(row)
            await tg_send(chat_id, f"✅ Listo.\nFolio: {folio}\nPDF: {url_pdf}")
        except Exception as e:
            # Si falla el insert, igual entregamos el PDF
            await tg_send(chat_id, f"⚠️ No se pudo guardar en la base, pero tu PDF fue generado.\nFolio: {folio}\nPDF: {url_pdf}")
            print("ERROR insert Supabase:", repr(e))
    elif text == "/start":
        await tg_send(chat_id, "Hola. Usa /permiso para generar tu permiso.")
    else:
        # Silencioso para otros textos
        pass
