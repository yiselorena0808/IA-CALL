import json
import logging
import os
import sys
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from twilio.twiml.voice_response import Gather, VoiceResponse

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("ia_call")


def _ia(msg: str, *args) -> None:
    log.info("[IA] " + msg, *args)

app = Flask(__name__)

PORT = int(os.getenv("PORT", "5000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

client = OpenAI(api_key=OPENAI_API_KEY) if (OpenAI and OPENAI_API_KEY) else None

# Endpoint Laravel (mismo contrato que el panel operador / CrearServicioModal)
SOLICITUD_TELEFONICA_PATH = "/api/taxi/solicitud-telefonica"


def _is_localhost_url(url: str) -> bool:
    u = (url or "").lower()
    return "localhost" in u or "127.0.0.1" in u or "::1" in u


def get_backend_url() -> str:
    """BACKEND_URL sin barra final. Ej: https://api.midominio.com"""
    return (os.getenv("BACKEND_URL") or "http://127.0.0.1:8000").strip().rstrip("/")


def backend_url_allows_post() -> bool:
    """
    En Render (RENDER=true), no enviar a localhost salvo ALLOW_LOCALHOST_BACKEND=true.
    Evita que el POST muera en silencio apuntando al default local.
    """
    url = get_backend_url()
    if _is_localhost_url(url):
        if os.getenv("RENDER") == "true" and os.getenv("ALLOW_LOCALHOST_BACKEND", "").lower() != "true":
            log.error(
                "BACKEND_URL apunta a localhost (%s) en Render. "
                "Configura BACKEND_URL con la URL pública del Laravel o "
                "ALLOW_LOCALHOST_BACKEND=true solo para depuración.",
                url,
            )
            return False
    return True


def twilio_public_base_url() -> str:
    """
    URL absoluta HTTPS para Twilio (Gather action / redirect).
    Prioriza PUBLIC_BASE_URL en Render (proxy/SSL) y corrige http→https.
    """
    explicit = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        if not explicit.startswith("http"):
            explicit = "https://" + explicit
        if explicit.startswith("http://"):
            explicit = "https://" + explicit[len("http://") :]
        return explicit
    root = request.url_root.rstrip("/")
    if root.startswith("http://"):
        root = "https://" + root[len("http://") :]
    return root


def post_solicitud_telefonica(payload: dict) -> None:
    base = get_backend_url()
    url = f"{base}{SOLICITUD_TELEFONICA_PATH}"

    _ia("BACKEND_URL configurada: %s", base)
    _ia("URL destino completa: %s", url)
    _ia("Path esperado: %s (debe coincidir exactamente)", SOLICITUD_TELEFONICA_PATH)

    if _is_localhost_url(base):
        _ia(
            "ADVERTENCIA: BACKEND_URL parece localhost (%s). En Render el POST no llegará a tu PC.",
            base,
        )

    if not backend_url_allows_post():
        _ia(
            "POST al backend OMITIDO (bloqueo Render+localhost). "
            "Define BACKEND_URL=https://tu-ngrok-o-dominio.com o ALLOW_LOCALHOST_BACKEND=true"
        )
        return

    _ia("Payload JSON a enviar: %s", json.dumps(payload, ensure_ascii=False, default=str))

    try:
        res = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        _ia("Respuesta backend status_code=%s", res.status_code)
        body_preview = (res.text or "")[:2000]
        _ia("Response body (primeros 2000 chars): %s", body_preview)
        if res.status_code >= 400:
            log.error(
                "[IA] Fallo backend solicitud telefónica status=%s body=%s",
                res.status_code,
                res.text[:2000],
            )
    except requests.RequestException as exc:
        log.exception("[IA] Error de red al llamar al backend URL=%s err=%s", url, exc)


@app.route("/", methods=["GET"])
def home():
    return "Servidor activo (IA llamadas)."


@app.route("/health", methods=["GET"])
def health():
    backend = get_backend_url()
    blocked = (
        _is_localhost_url(backend)
        and os.getenv("RENDER") == "true"
        and os.getenv("ALLOW_LOCALHOST_BACKEND", "").lower() != "true"
    )
    return jsonify(
        {
            "ok": True,
            "service": "ia-call",
            "backend_url": backend,
            "backend_post_blocked_on_render": blocked,
            "solicitud_path": SOLICITUD_TELEFONICA_PATH,
        }
    ), 200


@app.route("/voice", methods=["GET", "POST"])
def voice():
    _ia("Request Twilio recibido en /voice method=%s", request.method)
    _ia("Twilio form keys: %s", list(request.values.keys()))

    response = VoiceResponse()
    process_url = twilio_public_base_url() + "/process_speech"

    gather = Gather(
        input="speech",
        action=process_url,
        method="POST",
        language="es-MX",
        speech_timeout="3",
        timeout=15,
    )
    gather.say(
        "Hola, soy tu asistente virtual. Desde dónde y hacia dónde necesitas viajar hoy.",
        voice="alice",
        language="es-MX",
    )
    response.append(gather)
    response.say(
        "No pude escucharte. Por favor vuelve a llamar o intenta de nuevo.",
        voice="alice",
        language="es-MX",
    )
    response.hangup()

    return str(response), 200, {"Content-Type": "text/xml"}


@app.route("/process_speech", methods=["GET", "POST"])
def process_speech():
    _ia("Request Twilio recibido en /process_speech method=%s", request.method)
    _ia("Twilio form keys: %s", list(request.values.keys()))
    # Valores útiles de Twilio (sin credenciales)
    safe_debug = {
        k: (request.values.get(k) or "")[:120]
        for k in ("CallSid", "From", "To", "SpeechResult", "Confidence")
        if k in request.values
    }
    _ia("Campos Twilio (recortados): %s", safe_debug)

    response = VoiceResponse()
    texto_usuario = request.values.get("SpeechResult", "").strip()
    caller_id = request.values.get("From", "").replace("whatsapp:", "")

    if not texto_usuario:
        response.say(
            "Disculpa, no te entendí bien. Por favor repite tu dirección y destino.",
            voice="alice",
            language="es-MX",
        )
        voice_url = twilio_public_base_url() + "/voice"
        response.redirect(voice_url, method="POST")
        return str(response), 200, {"Content-Type": "text/xml"}

    _ia("Texto transcrito (SpeechResult), caller_id=%s texto=%s", caller_id, texto_usuario[:500])
    respuesta_ia = procesar_con_ia(texto_usuario, caller_id)

    if es_cierre(texto_usuario):
        response.say(respuesta_ia, voice="alice", language="es-MX")
        response.hangup()
        return str(response), 200, {"Content-Type": "text/xml"}

    process_url = twilio_public_base_url() + "/process_speech"
    gather = Gather(
        input="speech",
        action=process_url,
        method="POST",
        language="es-MX",
        speech_timeout="3",
        timeout=15,
    )
    gather.say(respuesta_ia, voice="alice", language="es-MX")
    response.append(gather)
    response.say("No escuché más información. Hasta luego.", voice="alice", language="es-MX")
    response.hangup()

    return str(response), 200, {"Content-Type": "text/xml"}


def es_cierre(texto: str) -> bool:
    texto_min = texto.lower()
    return any(
        word in texto_min
        for word in [
            "gracias",
            "eso es todo",
            "adiós",
            "adios",
            "no",
            "nada más",
            "nada mas",
        ]
    )


def procesar_con_ia(texto: str, celular: Optional[str] = None) -> str:
    if not client:
        _ia("OpenAI client no disponible (falta API key o paquete); no se enviará POST al backend desde IA simulada.")
        return respuesta_simulada(texto)

    try:
        prompt = f"""
Eres un asistente telefónico para solicitar transporte.
Debes responder SIEMPRE en formato JSON con la siguiente estructura estricta:
{{
  "origen": "dirección de recogida reconocida, o null si falta",
  "destino": "dirección de destino reconocida, o null si falta",
  "mensaje": "Respuesta en español, clara, corta y natural."
}}

Reglas:
- Si el usuario dice origen y destino, tu 'mensaje' debe confirmar que procesarás la solicitud y se enviará la unidad.
- Si falta origen o destino, tu 'mensaje' debe pedir específicamente lo que falta.
- Si el usuario quiere terminar la llamada, despídete en el 'mensaje'.
- No uses texto largo.

Mensaje del usuario:
{texto}
"""
        result = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )

        raw_content = result.choices[0].message.content
        _ia("OpenAI respuesta JSON (raw): %s", (raw_content or "")[:1500])

        data = json.loads(raw_content)
        origen = data.get("origen")
        destino = data.get("destino")
        mensaje = data.get("mensaje", respuesta_simulada(texto))

        _ia(
            "OpenAI parseado: origen=%r destino=%r (ambos deben ser no vacíos para POST backend)",
            origen,
            destino,
        )

        if origen and destino:
            _ia("Origen y destino OK; iniciando POST a Laravel solicitud-telefonica.")
            payload = {
                "pasajero_id": 1,
                "celular": celular or None,
                "pasajero_nombre": "Usuario Telefónico",
                "origen": origen,
                "destino": destino,
                "origen_lat": 0.0,
                "origen_lng": 0.0,
                "destino_lat": 0.0,
                "destino_lng": 0.0,
                "clase_vehiculo": "TAXI",
                "precio_estimado": 0.0,
            }
            post_solicitud_telefonica(payload)
        else:
            _ia(
                "NO se envía POST al backend: falta origen o destino en JSON (origen=%r destino=%r)",
                origen,
                destino,
            )

        return mensaje

    except Exception as e:
        log.exception("[IA] Error OpenAI o parseo JSON: %s", e)
        return respuesta_simulada(texto)


def respuesta_simulada(texto: str) -> str:
    texto_min = texto.lower()

    if es_cierre(texto_min):
        return "Perfecto. Hemos recibido tu solicitud. Gracias por preferirnos. Que tengas un excelente día."

    if len(texto_min) > 10:
        return "Entendido. Estamos procesando tu ruta. Enseguida te enviaremos los datos del conductor. ¿Deseas agregar algo más?"

    return "Por favor indícame claramente la dirección exacta de recogida y tu destino."


if __name__ == "__main__":
    _ia(
        "Iniciando servidor puerto=%s BACKEND_URL=%s RENDER=%s",
        PORT,
        get_backend_url(),
        os.getenv("RENDER", ""),
    )
    app.run(host="0.0.0.0", port=PORT, debug=False)
