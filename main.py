import os
import asyncio
import time
from collections import defaultdict
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AsyncOpenAI
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # Carga .env automaticamente al arrancar local

# ── Configuración ──────────────────────────────────────────────────────────────
BUSINESS_NAME   = os.getenv("BUSINESS_NAME", "Tarrazo inmobiliaria")
SOURCE_URL      = os.getenv("SOURCE_URL", "https://www.tarrazoinmobiliaria.com/")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
PORT            = int(os.getenv("PORT", "8000"))
MAX_REQUESTS_PER_MINUTE = int(os.getenv("MAX_REQUESTS_PER_MINUTE", "10"))

EVOLUTION_API_URL       = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY       = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME", "")

# ── TTL de demo ────────────────────────────────────────────────────────────────
# Formato ISO UTC: "2026-03-17T20:00:00Z". Si vacío, sin expiración.
DEMO_EXPIRES_AT = os.getenv("DEMO_EXPIRES_AT", "")

def demo_expired() -> bool:
    if not DEMO_EXPIRES_AT:
        return False
    try:
        expires = datetime.fromisoformat(DEMO_EXPIRES_AT.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > expires
    except Exception:
        return False

# ── Notificaciones de interés ──────────────────────────────────────────────────
N8N_WEBHOOK_URL        = os.getenv("N8N_WEBHOOK_URL", "https://n8n-produccion.navi-ol.xyz/webhook/1e166278-bf44-47d7-b2da-79c501300a88")
N8N_MESSAGE_LOGGER_URL = os.getenv("N8N_MESSAGE_LOGGER_URL", "https://n8n-produccion.navi-ol.xyz/webhook/mensaje_inmobilaria")

async def log_interaction(platform: str, user_message: str, bot_reply: str):
    """Envía un log del mensaje al webhook de n8n para almacenamiento centralizado."""
    payload = {
        "event": "new_message",
        "business_name": BUSINESS_NAME,
        "platform": platform,
        "user_message": user_message,
        "bot_reply": bot_reply,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(N8N_MESSAGE_LOGGER_URL, params=payload)
    except Exception as e:
        print(f"[Log Error] {e}")

async def notify_interest(business_name: str, timestamp: str, extra: str = ""):
    """Envía notificación al webhook de n8n cuando un cliente muestra interés."""
    payload = {
        "event": "demo_interest",
        "business_name": business_name,
        "timestamp": timestamp,
        "source_url": SOURCE_URL,
        "notes": extra
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(N8N_WEBHOOK_URL, params=payload)
            print(f"[Notify] n8n Webhook llamado para {business_name}")
    except Exception as e:
        print(f"[Notify Error] n8n Webhook: {e}")

# ── System Prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT_FILE = Path(__file__).parent / "system_prompt.txt"
SYSTEM_PROMPT = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8") if SYSTEM_PROMPT_FILE.exists() else (
    f"Eres el asistente virtual de {BUSINESS_NAME}. Responde basándote estrictamente en tus instrucciones."
)

# ── Clientes ───────────────────────────────────────────────────────────────────
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── Helpers ────────────────────────────────────────────────────────────────────
_rate_limits = defaultdict(list)

def check_rate_limit(client_id: str):
    now = time.time()
    _rate_limits[client_id] = [ts for ts in _rate_limits[client_id] if now - ts < 60]
    if len(_rate_limits[client_id]) >= MAX_REQUESTS_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Demasiados mensajes. Por favor, espera un minuto.")
    _rate_limits[client_id].append(now)


async def call_openai(messages: list, channel: str = "web") -> str:
    response = await openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        temperature=0.3,
        max_tokens=600 if channel == "whatsapp" else 1000,
    )
    return response.choices[0].message.content.strip()


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title=f"{BUSINESS_NAME} — Agente Informativo")

INDEX_HTML = Path(__file__).parent / "index.html"


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    expired = demo_expired()
    return {
        "status": "expired" if expired else "ok",
        "agent": BUSINESS_NAME,
        "model": OPENAI_MODEL,
        "demo_expires_at": DEMO_EXPIRES_AT or "never"
    }


@app.post("/chat")
async def chat(request: Request):
    if demo_expired():
        return JSONResponse({
            "reply": f"⏱️ Esta demo de *{BUSINESS_NAME}* ha finalizado. Contacta con **Navi** para activar tu propio asistente virtual.",
            "expired": True
        })

    try:
        client_ip = request.client.host if request.client else "unknown"
        try:
            check_rate_limit(client_ip)
        except HTTPException as e:
            return JSONResponse({"error": e.detail}, status_code=429)

        body = await request.json()
        messages = body.get("messages", [])
        if not messages:
            return JSONResponse({"error": "messages vacíos"}, status_code=400)

        reply = await call_openai(messages, channel="web")
        
        # Log interactivo (opcionalmente silenciado en errores)
        user_text = messages[-1].get("content", "") if messages else ""
        asyncio.create_task(log_interaction("web", user_text, reply))

        return {"reply": reply}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/interested")
async def interested(request: Request):
    """Recibe notificación de interés del cliente en el despliegue."""
    try:
        body = await request.json()
        extra = body.get("message", "")
        ts = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        await notify_interest(BUSINESS_NAME, ts, extra)
        return {"status": "ok", "message": "¡Perfecto! Nos pondremos en contacto contigo pronto."}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    """Endpoint para Evolution API (WhatsApp)."""
    try:
        body = await request.json()
        event = body.get("event", "")
        if event != "messages.upsert":
            return {"status": "ignored", "event": event}

        data = body.get("data", {})
        if data.get("key", {}).get("fromMe", False):
            return {"status": "ignored", "reason": "own message"}

        message_content = data.get("message", {})
        text = (
            message_content.get("conversation") or
            message_content.get("extendedTextMessage", {}).get("text") or
            ""
        ).strip()

        if not text:
            return {"status": "ignored", "reason": "no text content"}

        remote_jid = data.get("key", {}).get("remoteJid", "")
        if remote_jid:
            try:
                check_rate_limit(remote_jid)
            except HTTPException as e:
                return {"status": "rate_limited", "error": e.detail}

        reply = await call_openai([{"role": "user", "content": text}], channel="whatsapp")

        if EVOLUTION_API_URL and EVOLUTION_API_KEY and EVOLUTION_INSTANCE_NAME:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"{EVOLUTION_API_URL.rstrip('/')}/message/sendText/{EVOLUTION_INSTANCE_NAME}",
                    headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                    json={"number": remote_jid, "textMessage": {"text": reply}}
                )

        # Log interactivo (WhatsApp)
        asyncio.create_task(log_interaction("whatsapp", text, reply))

        return {"status": "ok", "replied": True}

    except Exception as e:
        print(f"[Webhook Error] {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
