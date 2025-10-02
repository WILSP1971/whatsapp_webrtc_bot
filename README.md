# WhatsApp Bot → WebRTC Videollamada (Python + FastAPI)

**Qué hace:** Un bot de WhatsApp (vía Cloud API) NO inicia una llamada de WhatsApp (no es posible con la API),
pero **coordina** una videollamada web creando una **sala WebRTC 1:1** y enviando el enlace a ambos usuarios.

## Importante (limitación de la plataforma)
- La **WhatsApp Business Platform (Cloud API)** **no permite** iniciar llamadas de voz o video dentro de WhatsApp.
  Este proyecto crea una **sala WebRTC** accesible por URL HTTPS. El bot comparte ese enlace por WhatsApp.

## Arquitectura
- **FastAPI** expone:
  - `/webhook` (GET/POST) para el webhook de WhatsApp.
  - `/room/{room_id}` página HTML con la videollamada.
  - `/ws/{room_id}` WebSocket para señalización (intercambio de SDP/ICE).
- Cuando un usuario escribe `videollamada +573001234567` al bot, el backend crea un `room_id` y envía el **mismo enlace** al emisor y al destinatario.

## Requisitos
- Python 3.10+
- Cuenta de **WhatsApp Cloud API** (número, `WABA_PHONE_NUMBER_ID`, `WHATSAPP_TOKEN` y webhook configurado).
- **HTTPS público** (dominio o ngrok). Para WhatsApp y para WebRTC es esencial.

## Instalación
```bash
cd whatsapp_webrtc_bot
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edita .env con tus valores reales
```

## Ejecutar en local (desarrollo)
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

- Expón tu servidor con **ngrok** o similar:
  ```bash
  ngrok http 8000
  ```
  Copia la URL `https://xxxxx.ngrok.app` en `PUBLIC_BASE_URL` dentro del `.env`.

## Configurar el Webhook en Meta
- **Verify Token**: usa `VERIFY_TOKEN` de tu `.env`.
- **Callback URL**: `{PUBLIC_BASE_URL}/webhook`
- Suscribe el objeto `whatsapp_business_account` y el campo `messages`.

## Probar el flujo
1. Desde tu WhatsApp envía al bot: `videollamada +573001234567` (reemplaza por el número del destinatario, formato E.164).
2. El bot responde con un enlace `PUBLIC_BASE_URL/room/<room_id>` para ti y para el destinatario.
3. Ambos abren el enlace, aceptan permisos de cámara y micrófono, y la llamada se establece p2p con WebRTC.

## Notas WebRTC/Red
- Por defecto usa STUN público de Google. En redes estrictas, añade un **TURN server** en `ICE_SERVERS_JSON`.
- Este ejemplo acepta **máximo 2 participantes** por sala.

## Producción
- Coloca FastAPI detrás de un **reverse proxy** (Nginx) con HTTPS válido.
- Usa un **TURN** propio para máxima fiabilidad NAT.
- Asegura rutas con expiración de `room_id` si necesitas mayor control.# whatsapp_webrtc_bot
