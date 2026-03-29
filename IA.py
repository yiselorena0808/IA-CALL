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
    if not backend_url_allows_post():
        return
    base = get_backend_url()
    url = f"{base}{SOLICITUD_TELEFONICA_PATH}"
    log.info("Enviando POST a backend: %s", url)
    try:
        res = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        log.info("Respuesta backend HTTP %s: %s", res.status_code, res.text[:500])
        if res.status_code >= 400:
            log.error(
                "Fallo backend al crear solicitud telefónica: status=%s body=%s",
                res.status_code,
                res.text[:1000],
            )
    except requests.RequestException as exc:
        log.exception("Error de red al llamar al backend: %s", exc)


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
    log.info("/voice method=%s", request.method)
    log.debug("Values keys: %s", list(request.values.keys()))

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
    log.info("/process_speech method=%s", request.method)

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

    log.info("Cliente (%s) dijo: %s", caller_id, texto_usuario[:200])
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

        data = json.loads(result.choices[0].message.content)
        origen = data.get("origen")
        destino = data.get("destino")
        mensaje = data.get("mensaje", respuesta_simulada(texto))

        if origen and destino:
            log.info("Origen y destino detectados; enviando al backend Laravel.")
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

        return mensaje

    except Exception as e:
        log.error("Error OpenAI: %s", e)
        return respuesta_simulada(texto)


def respuesta_simulada(texto: str) -> str:
    texto_min = texto.lower()

    if es_cierre(texto_min):
        return "Perfecto. Hemos recibido tu solicitud. Gracias por preferirnos. Que tengas un excelente día."

    if len(texto_min) > 10:
        return "Entendido. Estamos procesando tu ruta. Enseguida te enviaremos los datos del conductor. ¿Deseas agregar algo más?"

    return "Por favor indícame claramente la dirección exacta de recogida y tu destino."


if __name__ == "__main__":
    log.info("Iniciando servidor en puerto %s (BACKEND_URL=%s)", PORT, get_backend_url())
    app.run(host="0.0.0.0", port=PORT, debug=False)
