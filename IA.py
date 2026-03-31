import json
import logging
import os
import sys
import time
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

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

# Prefijos: [IA][VOICE] [IA][SPEECH] [IA][OPENAI] [IA][BACKEND_REQUEST] [IA][BACKEND_RESPONSE] [IA][ERROR]


def _log(tag: str, msg: str, *args) -> None:
    log.info("[IA][%s] " + msg, tag, *args)


def _log_exc(tag: str, msg: str, exc: BaseException) -> None:
    log.exception("[IA][%s] %s: %s", tag, msg, exc)

app = Flask(__name__)

PORT = int(os.getenv("PORT", "5000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# Twilio Gather: más tolerante a pausas del usuario (evita cortes por silencio breve)
TWILIO_SPEECH_TIMEOUT = os.getenv("TWILIO_SPEECH_TIMEOUT", "auto").strip()
TWILIO_GATHER_TIMEOUT = int(os.getenv("TWILIO_GATHER_TIMEOUT", "25"))

client = OpenAI(api_key=OPENAI_API_KEY) if (OpenAI and OPENAI_API_KEY) else None

SOLICITUD_TELEFONICA_PATH = "/api/taxi/solicitud-telefonica"

GEOCODE_ENABLED = os.getenv("GEOCODE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y")
GEOCODE_PROVIDER = (os.getenv("GEOCODE_PROVIDER") or "nominatim").strip().lower()
NOMINATIM_URL = (os.getenv("NOMINATIM_URL") or "https://nominatim.openstreetmap.org/search").strip()
GEOCODE_COUNTRYCODES = (os.getenv("GEOCODE_COUNTRYCODES") or "co").strip()
GEOCODE_SUFFIX = (os.getenv("GEOCODE_SUFFIX") or "Popayán, Cauca, Colombia").strip()
GEOCODE_TIMEOUT = float(os.getenv("GEOCODE_TIMEOUT", "8"))


def _is_localhost_url(url: str) -> bool:
    u = (url or "").lower()
    return "localhost" in u or "127.0.0.1" in u or "::1" in u


def get_backend_url() -> str:
    """
    Origen sin sufijo /api duplicado (acepta BACKEND_URL .../api/ desde .env).
    """
    raw = (os.getenv("BACKEND_URL") or "http://127.0.0.1:8000").strip()
    if not raw:
        raw = "http://127.0.0.1:8000"

    parsed = urlparse(raw)
    if not parsed.netloc and parsed.path:
        parsed = urlparse(f"https://{raw.lstrip('/')}")

    path = (parsed.path or "").rstrip("/")
    while len(path) >= 4 and path.lower().endswith("/api"):
        path = path[:-4].rstrip("/")

    if parsed.scheme and parsed.netloc:
        path_part = path if path else ""
        base = urlunparse((parsed.scheme, parsed.netloc, path_part, "", "", ""))
    else:
        base = raw

    base = base.rstrip("/")

    if raw.rstrip("/") != base:
        _log("CONFIG", "BACKEND_URL normalizada: entrada=%r -> base=%r", raw, base)

    return base


def build_solicitud_telefonica_url() -> str:
    """
    URL final del POST, sin // ni /api/api/.
    Ej: https://host.ngrok-free.dev/api/taxi/solicitud-telefonica
    """
    base = get_backend_url().rstrip("/")
    path = SOLICITUD_TELEFONICA_PATH if SOLICITUD_TELEFONICA_PATH.startswith("/") else f"/{SOLICITUD_TELEFONICA_PATH}"
    return f"{base}{path}"


def _backend_post_headers(base: str) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if "ngrok" in base.lower():
        h["ngrok-skip-browser-warning"] = "true"
    return h


def backend_url_allows_post() -> bool:
    url = get_backend_url()
    if _is_localhost_url(url):
        if os.getenv("RENDER") == "true" and os.getenv("ALLOW_LOCALHOST_BACKEND", "").lower() != "true":
            _log(
                "ERROR",
                "BACKEND_URL es localhost en Render; POST bloqueado. Use URL pública o ALLOW_LOCALHOST_BACKEND=true",
            )
            return False
    return True


def twilio_public_base_url() -> str:
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
    url = build_solicitud_telefonica_url()

    _log("BACKEND_REQUEST", "BACKEND_URL (base)=%s", base)
    _log("BACKEND_REQUEST", "URL POST final=%s", url)
    _log("BACKEND_REQUEST", "Headers=%s", _backend_post_headers(base))

    if _is_localhost_url(base):
        _log("BACKEND_REQUEST", "ADVERTENCIA: base parece localhost (Render no alcanza tu PC).")

    if not backend_url_allows_post():
        _log("BACKEND_REQUEST", "POST omitido por política Render/localhost.")
        return

    _log("BACKEND_REQUEST", "Payload JSON=%s", json.dumps(payload, ensure_ascii=False, default=str))

    t0 = time.perf_counter()
    try:
        res = requests.post(
            url,
            json=payload,
            headers=_backend_post_headers(base),
            timeout=30,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log("BACKEND_RESPONSE", "status_code=%s tiempo_ms=%.0f", res.status_code, elapsed_ms)
        _log("BACKEND_RESPONSE", "body (primeros 2500 chars)=%s", (res.text or "")[:2500])
        if res.status_code >= 400:
            _log("ERROR", "Backend HTTP error status=%s body=%s", res.status_code, (res.text or "")[:2000])
    except requests.Timeout as exc:
        _log_exc("ERROR", "Timeout esperando al backend (>30s)", exc)
    except requests.RequestException as exc:
        _log_exc("ERROR", "Error de red/DNS/SSL hacia backend", exc)


def _nominatim_geocode(query: str) -> Optional[Tuple[float, float, str]]:
    q = (query or "").strip()
    if not q:
        return None

    headers = {
        "User-Agent": "ia-call/virtual-school-taxi (contact: admin)",
        "Accept": "application/json",
    }
    params = {
        "q": q,
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
    }
    if GEOCODE_COUNTRYCODES:
        params["countrycodes"] = GEOCODE_COUNTRYCODES

    _log("BACKEND_REQUEST", "Geocode Nominatim q=%r url=%s", q, NOMINATIM_URL)
    try:
        r = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=GEOCODE_TIMEOUT)
        if r.status_code != 200:
            _log("ERROR", "Geocode HTTP %s body=%s", r.status_code, (r.text or "")[:500])
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        row = data[0] or {}
        lat = float(row.get("lat") or 0.0)
        lon = float(row.get("lon") or 0.0)
        name = str(row.get("display_name") or "")
        if abs(lat) < 1e-9 and abs(lon) < 1e-9:
            return None
        return lat, lon, name
    except Exception as exc:
        _log_exc("ERROR", "Geocode exception", exc)
        return None


def maybe_geocode_pair(origen: str, destino: str) -> Tuple[Tuple[float, float, str], Tuple[float, float, str]]:
    """
    Retorna ((olat, olng, ometa), (dlat, dlng, dmeta)).
    Si no hay resultado, lat/lng quedan 0.0.
    """
    if not GEOCODE_ENABLED:
        return (0.0, 0.0, "geocode_disabled"), (0.0, 0.0, "geocode_disabled")

    if GEOCODE_PROVIDER != "nominatim":
        return (0.0, 0.0, f"provider_not_supported:{GEOCODE_PROVIDER}"), (0.0, 0.0, f"provider_not_supported:{GEOCODE_PROVIDER}")

    def _q(x: str) -> str:
        x = (x or "").strip()
        if not x:
            return x
        if GEOCODE_SUFFIX and GEOCODE_SUFFIX.lower() not in x.lower():
            return f"{x}, {GEOCODE_SUFFIX}"
        return x

    o = _nominatim_geocode(_q(origen))
    d = _nominatim_geocode(_q(destino))

    o_out = (o[0], o[1], o[2]) if o else (0.0, 0.0, "no_result")
    d_out = (d[0], d[1], d[2]) if d else (0.0, 0.0, "no_result")
    return o_out, d_out


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
            "backend_url_base": backend,
            "solicitud_url_completa": build_solicitud_telefonica_url(),
            "backend_post_blocked_on_render": blocked,
            "twilio_speech_timeout": TWILIO_SPEECH_TIMEOUT,
            "twilio_gather_timeout": TWILIO_GATHER_TIMEOUT,
        }
    ), 200


def _gather_kwargs():
    """Parámetros Twilio Gather reutilizables (pausas más tolerantes)."""
    return {
        "input": "speech",
        "language": "es-MX",
        "speech_timeout": TWILIO_SPEECH_TIMEOUT,
        "timeout": TWILIO_GATHER_TIMEOUT,
    }


@app.route("/voice", methods=["GET", "POST"])
def voice():
    _log("VOICE", "Entrada llamada method=%s", request.method)
    _log("VOICE", "form_keys=%s", list(request.values.keys()))
    _log("VOICE", "CallSid=%s From=%s To=%s",
        request.values.get("CallSid", ""),
        request.values.get("From", ""),
        request.values.get("To", ""))

    response = VoiceResponse()
    process_url = twilio_public_base_url() + "/process_speech"

    gather = Gather(action=process_url, method="POST", **_gather_kwargs())
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
    _log("SPEECH", "Entrada method=%s", request.method)
    _log("SPEECH", "form_keys=%s", list(request.values.keys()))
    _log(
        "SPEECH",
        "CallSid=%s From=%s To=%s",
        request.values.get("CallSid", ""),
        request.values.get("From", ""),
        request.values.get("To", ""),
    )

    conf = request.values.get("Confidence", "")
    if conf:
        _log("SPEECH", "Confidence(stt)=%s", conf)

    response = VoiceResponse()
    texto_crudo = request.values.get("SpeechResult", "") or ""
    texto_usuario = texto_crudo.strip()
    caller_id = request.values.get("From", "").replace("whatsapp:", "")

    _log("SPEECH", "SpeechResult crudo (len=%s)=%s", len(texto_crudo), texto_crudo[:800])
    _log("SPEECH", "Texto limpio caller_id=%s texto=%s", caller_id, texto_usuario[:800])

    if not texto_usuario:
        _log("SPEECH", "Sin SpeechResult: posible silencio o timeout STT; reintentando /voice")
        response.say(
            "Disculpa, no te entendí bien. Por favor repite tu dirección y destino.",
            voice="alice",
            language="es-MX",
        )
        voice_url = twilio_public_base_url() + "/voice"
        response.redirect(voice_url, method="POST")
        return str(response), 200, {"Content-Type": "text/xml"}

    respuesta_ia, envio_backend = procesar_con_ia(texto_usuario, caller_id)
    _log("SPEECH", "Mensaje TTS a reproducir (preview)=%s", respuesta_ia[:400])
    _log("SPEECH", "¿Se intentó POST al backend en este turno?=%s", envio_backend)

    if envio_backend:
        response.say(
            "Un momento, estamos registrando tu solicitud.",
            voice="alice",
            language="es-MX",
        )

    if es_cierre(texto_usuario):
        _log("SPEECH", "Detección cierre de llamada (despedida) sobre texto usuario")
        response.say(respuesta_ia, voice="alice", language="es-MX")
        response.hangup()
        return str(response), 200, {"Content-Type": "text/xml"}

    process_url = twilio_public_base_url() + "/process_speech"
    gather = Gather(action=process_url, method="POST", **_gather_kwargs())
    gather.say(respuesta_ia, voice="alice", language="es-MX")
    response.append(gather)
    response.say("Si necesitas algo más, habla ahora. Si no, puedes colgar.", voice="alice", language="es-MX")
    response.hangup()

    return str(response), 200, {"Content-Type": "text/xml"}


def es_cierre(texto: str) -> bool:
    """
    Despedida explícita. No usar la palabra suelta 'no' (provocaba cortes en frases normales).
    """
    t = (texto or "").lower().strip()
    frases = (
        "gracias",
        "eso es todo",
        "adiós",
        "adios",
        "nada más",
        "nada mas",
        "listo gracias",
        "ya está",
        "ya esta",
        "chao",
        "hasta luego",
    )
    return any(p in t for p in frases)


def procesar_con_ia(texto: str, celular: Optional[str] = None) -> Tuple[str, bool]:
    """
    Retorna (mensaje_para_usuario, se_hizo_post_backend).
    """
    if not client:
        _log("OPENAI", "Cliente OpenAI no configurado; modo simulado, sin POST backend.")
        return respuesta_simulada(texto), False

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
        _log("OPENAI", "Enviando prompt a modelo=%s (texto usuario len=%s)", OPENAI_MODEL, len(texto))

        t0 = time.perf_counter()
        result = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        _log("OPENAI", "Respuesta OpenAI recibida tiempo_ms=%.0f", (time.perf_counter() - t0) * 1000.0)

        raw_content = result.choices[0].message.content
        _log("OPENAI", "JSON raw (primeros 1500 chars)=%s", (raw_content or "")[:1500])

        data = json.loads(raw_content)
        origen = data.get("origen")
        destino = data.get("destino")
        mensaje = data.get("mensaje", respuesta_simulada(texto))

        o_ok = origen not in (None, "", "null")
        d_ok = destino not in (None, "", "null")
        _log(
            "OPENAI",
            "Parseado origen_ok=%s destino_ok=%s origen=%r destino=%r",
            o_ok,
            d_ok,
            origen,
            destino,
        )

        if o_ok and d_ok:
            _log("OPENAI", "Origen y destino completos; ejecutando POST Laravel.")
            (olat, olng, odisp), (dlat, dlng, ddisp) = maybe_geocode_pair(origen, destino)
            _log("BACKEND_REQUEST", "Geocode resultado origen=(%.6f, %.6f) meta=%r destino=(%.6f, %.6f) meta=%r",
                 olat, olng, odisp, dlat, dlng, ddisp)
            payload = {
                "pasajero_id": 1,
                "celular": celular or None,
                "pasajero_nombre": "Usuario Telefónico",
                "origen": origen,
                "destino": destino,
                "origen_lat": float(olat),
                "origen_lng": float(olng),
                "destino_lat": float(dlat),
                "destino_lng": float(dlng),
                "clase_vehiculo": "TAXI",
                "precio_estimado": 0.0,
            }
            post_solicitud_telefonica(payload)
            return mensaje, True

        _log("OPENAI", "No POST backend: falta origen o destino válido en JSON.")
        return mensaje, False

    except Exception as e:
        _log_exc("ERROR", "Fallo OpenAI o JSON", e)
        return respuesta_simulada(texto), False


def respuesta_simulada(texto: str) -> str:
    texto_min = texto.lower()

    if es_cierre(texto_min):
        return "Perfecto. Hemos recibido tu solicitud. Gracias por preferirnos. Que tengas un excelente día."

    if len(texto_min) > 10:
        return "Entendido. Estamos procesando tu ruta. Enseguida te enviaremos los datos del conductor. ¿Deseas agregar algo más?"

    return "Por favor indícame claramente la dirección exacta de recogida y tu destino."


if __name__ == "__main__":
    _log("CONFIG", "Arranque puerto=%s BACKEND_URL_raw=%s base=%s url_post=%s RENDER=%s",
         PORT,
         (os.getenv("BACKEND_URL") or ""),
         get_backend_url(),
         build_solicitud_telefonica_url(),
         os.getenv("RENDER", ""))
    app.run(host="0.0.0.0", port=PORT, debug=False)
