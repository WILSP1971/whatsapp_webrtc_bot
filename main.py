import os, json, secrets, re
from typing import Dict, List, Optional
from pathlib import Path

import requests
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="WhatsApp → WebRTC Videocall")

# --- Asegura carpetas ---
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"; STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR = BASE_DIR / "templates"; TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

# Static & templates
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Env config
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "TWSCodeJG#75")
WABA_PHONE_NUMBER_ID = os.getenv("WABA_PHONE_NUMBER_ID", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://whatsapp-webrtc-bot-2.onrender.com").rstrip("/")
DEFAULT_CALLEE_PHONE = os.getenv("DEFAULT_CALLEE_PHONE", "")
ICE_SERVERS_JSON = os.getenv("ICE_SERVERS_JSON", '[{"urls":"stun:stun.l.google.com:19302"}]')

try:
    ICE_SERVERS = json.loads(ICE_SERVERS_JSON)
except Exception:
    ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]

# In-memory room storage (demo only)
class Room:
    def __init__(self) -> None:
        self.clients: List[WebSocket] = []
    def is_full(self) -> bool:
        return len(self.clients) >= 2

rooms: Dict[str, Room] = {}

def create_room_id() -> str:
    return secrets.token_urlsafe(6)

# ✅ ÚNICA función: /room/ (singular)
def build_room_link(room_id: str) -> str:
    return f"{PUBLIC_BASE_URL}/room/{room_id}"

# WhatsApp API helpers
def send_whatsapp_text(to_e164: str, body: str) -> requests.Response:
    if not WABA_PHONE_NUMBER_ID or not WHATSAPP_TOKEN:
        print("⚠️ Missing WABA_PHONE_NUMBER_ID or WHATSAPP_TOKEN")
    url = f"https://graph.facebook.com/v22.0/{WABA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_e164, "type": "text", "text": {"body": body}}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    try: print("WhatsApp send resp:", resp.status_code, resp.text[:500])
    except Exception: pass
    return resp

def parse_callee(text: str) -> Optional[str]:
    match = re.search(r"(\+?\d{8,15})", text.replace(" ", ""))
    if match:
        num = match.group(1)
        if not num.startswith("+"): num = "+" + num
        return num
    return None

@app.get("/")
def root():
    return {"ok": True, "msg": "WhatsApp → WebRTC Videocall backend running."}

# ============== WhatsApp Webhook ==============
@app.get("/webhook")
async def verify(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge") or "", status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)

@app.post("/webhook")
async def incoming(request: Request):
    data = await request.json()
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []) or []:
                    from_user = msg.get("from")
                    wa_id = f"+{from_user}" if from_user and not from_user.startswith("+") else from_user
                    text = (msg.get("text", {}) or {}).get("body", "").strip().lower() if msg.get("type")=="text" else ""
                    if text.startswith("videollamada") or text.startswith("video"):
                        callee = parse_callee(text) or DEFAULT_CALLEE_PHONE
                        if not callee:
                            send_whatsapp_text(wa_id, "Dime a quién invitar: escribe por ejemplo\n`videollamada +573001234567`")
                            continue
                        room_id = create_room_id()
                        link = build_room_link(room_id)
                        send_whatsapp_text(wa_id, f"🎥 Creé tu sala de videollamada:\n{link}\n\nCompártela si deseas.")
                        if callee != wa_id:
                            send_whatsapp_text(callee, f"📞 {wa_id} te ha invitado a una videollamada:\n{link}")
                    else:
                        send_whatsapp_text(wa_id, "Escribe:\n`videollamada +573001234567`\npara crear una sala y enviar el enlace.")
        return JSONResponse({"status": "received"})
    except Exception as e:
        print("Webhook error:", e)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=200)

# ============== WebRTC Signaling (WebSocket) ==============
@app.websocket("/ws/{room_id}")
async def ws_room(websocket: WebSocket, room_id: str):
    await websocket.accept()
    room = rooms.get(room_id) or Room()
    rooms.setdefault(room_id, room)

    if room.is_full():
        await websocket.send_json({"type": "full", "message": "Room is full"})
        await websocket.close()
        return

    room.clients.append(websocket)
    role = "caller" if len(room.clients) == 1 else "callee"
    await websocket.send_json({"type": "role", "role": role, "iceServers": ICE_SERVERS})

    if len(room.clients) == 2:
        for ws in list(room.clients):
            try: await ws.send_json({"type": "ready"})
            except Exception: pass

    try:
        while True:
            msg = await websocket.receive_text()
            for ws in list(room.clients):
                if ws is not websocket:
                    await ws.send_text(msg)
    except WebSocketDisconnect:
        pass
    finally:
        try: room.clients.remove(websocket)
        except ValueError: pass
        if not room.clients:
            rooms.pop(room_id, None)

# ============== Room Page + Alias ==============
@app.get("/room/{room_id}", response_class=HTMLResponse)
def room_page(room_id: str, request: Request):
    # pasamos base_prefix vacío por si tu template lo usa
    return templates.TemplateResponse("room.html", {"request": request, "room_id": room_id, "base_prefix": ""})

# Alias: /rooms/{id} -> /room/{id}
@app.get("/rooms/{room_id}")
def alias_rooms(room_id: str):
    return RedirectResponse(url=f"/room/{room_id}", status_code=302)
