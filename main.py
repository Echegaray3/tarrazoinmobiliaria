import os
import asyncio
import time
from collections import defaultdict
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

SYSTEM_PROMPT_FILE = Path(__file__).parent / "system_prompt.txt"
SYSTEM_PROMPT = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8") if SYSTEM_PROMPT_FILE.exists() else (
    f"Eres el asistente virtual de {BUSINESS_NAME}. Responde basándote estrictamente en tus instrucciones."
)

# ── Clientes ───────────────────────────────────────────────────────────────────
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── Helpers ────────────────────────────────────────────────────────────────────

# Rate Limiter State
_rate_limits = defaultdict(list)

def check_rate_limit(client_id: str):
    """Verifica si el cliente ha excedido el límite de peticiones por minuto."""
    now = time.time()
    _rate_limits[client_id] = [ts for ts in _rate_limits[client_id] if now - ts < 60]
    if len(_rate_limits[client_id]) >= MAX_REQUESTS_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Demasiados mensajes. Por favor, espera un minuto.")
    _rate_limits[client_id].append(now)


async def call_openai(messages: list, channel: str = "web") -> str:
    """Llama a OpenAI usando la Base de Conocimientos estática (SYSTEM_PROMPT)."""
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
    return {"status": "ok", "agent": BUSINESS_NAME, "model": OPENAI_MODEL}


@app.post("/chat")
async def chat(request: Request):
    """Endpoint del chat web. Acepta {messages: [{role, content}]}"""
    try:
        # Rate Limiting por IP
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
        return {"reply": reply}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)



@app.get("/proxy")
async def proxy():
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=15)
            html = r.text
            # Inject base tag to fix relative links
            base_tag = f"<base href='{SOURCE_URL}'>"
            if "<head>" in html:
                html = html.replace("<head>", f"<head>{base_tag}")
            elif "<head " in html:
                # Basic string replacement heuristic
                html = html.replace("<head ", f"<head>{base_tag}</head><head ", 1)
            else:
                html = base_tag + html
            return HTMLResponse(content=html, status_code=200)
    except Exception as e:
        return HTMLResponse(content=f"Error loading {SOURCE_URL}: {e}")


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    """Endpoint para Evolution API (WhatsApp). Procesa mensajes entrantes."""
    try:
        body = await request.json()
        
        # Extraer evento y mensaje de Evolution API v2
        event = body.get("event", "")
        if event != "messages.upsert":
            return {"status": "ignored", "event": event}
        
        data = body.get("data", {})
        
        # Ignorar mensajes propios
        if data.get("key", {}).get("fromMe", False):
            return {"status": "ignored", "reason": "own message"}
        
        # Extraer texto del mensaje
        message_content = data.get("message", {})
        text = (
            message_content.get("conversation") or
            message_content.get("extendedTextMessage", {}).get("text") or
            ""
        ).strip()
        
        if not text:
            return {"status": "ignored", "reason": "no text content"}
        
        remote_jid = data.get("key", {}).get("remoteJid", "")
        
        # Rate Limiting por número de teléfono
        if remote_jid:
            try:
                check_rate_limit(remote_jid)
            except HTTPException as e:
                return {"status": "rate_limited", "error": e.detail}
        
        # Generar respuesta
        reply = await call_openai([{"role": "user", "content": text}], channel="whatsapp")
        
        # Enviar respuesta via Evolution API
        if EVOLUTION_API_URL and EVOLUTION_API_KEY and EVOLUTION_INSTANCE_NAME:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"{EVOLUTION_API_URL.rstrip('/')}/message/sendText/{EVOLUTION_INSTANCE_NAME}",
                    headers={
                        "apikey": EVOLUTION_API_KEY,
                        "Content-Type": "application/json"
                    },
                    json={
                        "number": remote_jid,
                        "textMessage": {"text": reply}
                    }
                )
        
        return {"status": "ok", "replied": True}
    
    except Exception as e:
        print(f"[Webhook Error] {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
