import os, json, secrets, re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path

import requests
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# --- Prefijo bajo el que publicas la app (tu ejemplo: /ApiCampbell) ---
BASE_PREFIX = "/ApiCampbell"

# --- Paths base (Render usa /opt/render/project/src) ---
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Rooms API + WebRTC (con prefijo)")

app.mount(f"{BASE_PREFIX}/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --- ENV ---
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "CHANGE_ME_VERIFY_TOKEN")
WABA_PHONE_NUMBER_ID = os.getenv("WABA_PHONE_NUMBER_ID", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://tu-dominio")  # pon tu dominio con https
DEFAULT_CALLEE_PHONE = os.getenv("DEFAULT_CALLEE_PHONE", "")
ICE_SERVERS_JSON = os.getenv("ICE_SERVERS_JSON", '[{"urls":"stun:stun.l.google.com:19302"}]')

try:
    ICE_SERVERS = json.loads(ICE_SERVERS_JSON)
except Exception:
    ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]

# --------- MODELOS ---------
class CreateRoomBody(BaseModel):
    caller: str
    callee: str
    ttl_minutes: int = 60

class CreateRoomResponse(BaseModel):
    room_id: str
    caller_url: str
    callee_url: str
    expires_at: str

class RoomInfo(BaseModel):
    room_id: str
    participants: List[str]
    expires_at: str
    connected: int

# --------- STORAGE (memoria; en prod usa Redis) ---------
class Room:
    def __init__(self, caller: str, callee: str, ttl_minutes: int) -> None:
        self.created_at = datetime.utcnow()
        self.expires_at = self.created_at + timedelta(minutes=ttl_minutes)
        self.participants = [caller, callee]
        self.tokens: Dict[str, str] = {caller: secrets.token_urlsafe(16), callee: secrets.token_urlsafe(16)}
        self.clients: Dict[str, WebSocket] = {}  # token -> WebSocket

    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

rooms: Dict[str, Room] = {}

def normalize_e164(s: str) -> str:
    s = s.replace(" ", "")
    return s if s.startswith("+") else f"+{s}"

def build_room_link(room_id: str, token: str) -> str:
    # Incluimos el prefijo para que el link quede como https://dominio/ApiCampbell/room/ABC?t=TOKEN
    return f"{PUBLIC_BASE_URL}{BASE_PREFIX}/room/{room_id}?t={token}"

def create_room(caller: str, callee: str, ttl_minutes: int):
    caller = normalize_e164(caller)
    callee = normalize_e164(callee)
    room_id = secrets.token_urlsafe(6)
    room = Room(caller, callee, ttl_minutes)
    rooms[room_id] = room
    return (
        room_id,
        build_room_link(room_id, room.tokens[caller]),
        build_room_link(room_id, room.tokens[callee]),
        room.expires_at,
    )

# --------- API REST ---------
@app.post(f"{BASE_PREFIX}/api/rooms", response_model=CreateRoomResponse)
def api_create_room(body: CreateRoomBody):
    room_id, caller_url, callee_url, exp = create_room(body.caller, body.callee, body.ttl_minutes)
    return CreateRoomResponse(room_id=room_id, caller_url=caller_url, callee_url=callee_url, expires_at=exp.isoformat()+"Z")

@app.get(f"{BASE_PREFIX}/api/rooms/{{room_id}}", response_model=RoomInfo)
def api_room_info(room_id: str):
    room = rooms.get(room_id)
    if not room or room.is_expired():
        raise HTTPException(status_code=404, detail="Room not found or expired")
    return RoomInfo(room_id=room_id, participants=room.participants, expires_at=room.expires_at.isoformat()+"Z", connected=len(room.clients))

@app.delete(f"{BASE_PREFIX}/api/rooms/{{room_id}}")
def api_room_delete(room_id: str):
    room = rooms.pop(room_id, None)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    for ws in list(room.clients.values()):
        try:
            import anyio
            anyio.from_thread.run(ws.close)
        except Exception:
            pass
    return {"ok": True, "deleted": room_id}

# *** Alias para tus enlaces actuales: /ApiCampbell/api/room/{room_id} ***
@app.get(f"{BASE_PREFIX}/api/room/{{room_id}}", response_class=HTMLResponse)
def api_room_alias(room_id: str, request: Request):
    # Redirige a la página correcta (sin necesidad de token aquí)
    return RedirectResponse(url=f"{BASE_PREFIX}/room/{room_id}", status_code=302)

# --------- Página HTML ---------
@app.get(f"{BASE_PREFIX}/room/{{room_id}}", response_class=HTMLResponse)
def room_page(room_id: str, request: Request):
    room = rooms.get(room_id)
    if not room or room.is_expired():
        return HTMLResponse("<h1>Sala no existe o expirada</h1>", status_code=404)
    # pasamos base_prefix para que el JS arme bien la URL del WS
    return templates.TemplateResponse("room.html", {"request": request, "room_id": room_id, "base_prefix": BASE_PREFIX})

# --------- WebSocket de señalización ---------
@app.websocket(f"{BASE_PREFIX}/ws/{{room_id}}")
async def ws_room(websocket: WebSocket, room_id: str, t: Optional[str] = Query(default=None)):
    room = rooms.get(room_id)
    await websocket.accept()
    if not room or room.is_expired():
        await websocket.send_json({"type": "error", "message": "Room not found or expired"})
        await websocket.close(); return
    if not t or t not in room.tokens.values():
        await websocket.send_json({"type": "error", "message": "Invalid token"})
        await websocket.close(); return

    if len(room.clients) >= 2 and t not in room.clients:
        await websocket.send_json({"type": "full", "message": "Room is full"})
        await websocket.close(); return

    room.clients[t] = websocket
    role = "caller" if len(room.clients) == 1 else "callee"
    await websocket.send_json({"type": "role", "role": role, "iceServers": ICE_SERVERS})
    if len(room.clients) == 2:
        for ws in list(room.clients.values()):
            try: await ws.send_json({"type": "ready"})
            except Exception: pass

    try:
        while True:
            msg = await websocket.receive_text()
            for tok, ws in list(room.clients.items()):
                if tok != t:
                    await ws.send_text(msg)
    except WebSocketDisconnect:
        pass
    finally:
        room.clients.pop(t, None)
        if not room.clients and room.is_expired():
            rooms.pop(room_id, None)

# --------- Opcional: webhook de WhatsApp para crear salas desde el chat ---------
def parse_callee(text: str) -> Optional[str]:
    m = re.search(r"(\+?\d{8,15})", text.replace(" ", ""))
    return normalize_e164(m.group(1)) if m else None

def send_whatsapp_text(to: str, body: str):
    if not WABA_PHONE_NUMBER_ID or not WHATSAPP_TOKEN: return
    url = f"https://graph.facebook.com/v20.0/{WABA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type":"application/json"}
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":body}}
    try: requests.post(url, headers=headers, json=payload, timeout=30)
    except Exception: pass

@app.get(f"{BASE_PREFIX}/webhook")
def verify(request: Request):
    p = dict(request.query_params)
    if p.get("hub.mode")=="subscribe" and p.get("hub.verify_token")==VERIFY_TOKEN:
        return PlainTextResponse(p.get("hub.challenge",""), status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)

@app.post(f"{BASE_PREFIX}/webhook")
async def incoming(request: Request):
    data = await request.json()
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    wa_id = normalize_e164(msg.get("from",""))
                    text = msg.get("text",{}).get("body","").lower().strip() if msg.get("type")=="text" else ""
                    if text.startswith("videollamada") or text.startswith("video"):
                        callee = parse_callee(text) or DEFAULT_CALLEE_PHONE
                        if not callee:
                            send_whatsapp_text(wa_id, "Usa: videollamada +573001234567"); continue
                        room_id, caller_url, callee_url, _ = create_room(wa_id, callee, 60)
                        send_whatsapp_text(wa_id, f"🎥 Sala: {caller_url}")
                        if callee != wa_id: send_whatsapp_text(callee, f"📞 {wa_id} te invitó: {callee_url}")
                    else:
                        send_whatsapp_text(wa_id, "Usa: videollamada +573001234567")
        return JSONResponse({"status":"received"})
    except Exception as e:
        return JSONResponse({"status":"error","detail":str(e)})

@app.get("/")
def root():
    return {"ok": True, "msg": "Rooms API + WebRTC con prefijo", "base_prefix": BASE_PREFIX}
