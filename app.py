"""
IA telefónica (Twilio) — un solo archivo para despliegue (Render, etc.).
Flujo: saludo → origen → sí/no destino → destino opcional → un solo POST al backend Laravel → cierre.

Incluye la lógica que antes estaba en ia_phone_logic.py (bbox Popayán, parse sí/no).
"""
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from twilio.twiml.voice_response import Gather, VoiceResponse

load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ── Lógica pura (antes ia_phone_logic.py) ─────────────────────────────────────

POPAYAN_MIN_LAT = float(os.getenv("POPAYAN_MIN_LAT", "2.32"))
POPAYAN_MAX_LAT = float(os.getenv("POPAYAN_MAX_LAT", "2.58"))
POPAYAN_MIN_LNG = float(os.getenv("POPAYAN_MIN_LNG", "-76.82"))
POPAYAN_MAX_LNG = float(os.getenv("POPAYAN_MAX_LNG", "-76.42"))


def in_popayan_bbox(lat: float, lng: float) -> bool:
    return (
        POPAYAN_MIN_LAT <= lat <= POPAYAN_MAX_LAT
        and POPAYAN_MIN_LNG <= lng <= POPAYAN_MAX_LNG
    )


def parse_si_no(texto: str, openai_client: Any = None, model: str = "gpt-4o-mini") -> Optional[bool]:
    """True = sí quiere indicar destino, False = no, None = no claro."""
    t = (texto or "").lower().strip()
    if not t:
        return None
    no_patterns = (
        r"^no$",
        r"\bno gracias\b",
        r"\bnop\b",
        r"\bmejor no\b",
        r"\bno quiero\b",
        r"\bal conductor\b",
        r"\bdirectamente al conductor\b",
        r"\bno deseo\b",
        r"\bnegativo\b",
    )
    si_patterns = (
        r"^s[ií]$",
        r"^si$",
        r"\bclaro\b",
        r"\bpor supuesto\b",
        r"\bdale\b",
        r"\bok\b",
        r"\bvale\b",
        r"\blisto\b",
        r"\bafirmativo\b",
        r"\bquiero indicar\b",
        r"\bsí quiero\b",
        r"\bsi quiero\b",
        r"\bdesde luego\b",
    )
    for p in no_patterns:
        if re.search(p, t):
            return False
    for p in si_patterns:
        if re.search(p, t):
            return True
    if openai_client is None:
        return None
    try:
        result = openai_client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        '¿El usuario responde SÍ o NO a "¿quieres indicar destino?" '
                        f'Responde solo una palabra: SI o NO o AMBIGUO.\nTexto: {texto}'
                    ),
                }
            ],
            max_tokens=5,
            timeout=8.0,
        )
        ans = (result.choices[0].message.content or "").strip().upper()
        if "SI" in ans and "NO" not in ans:
            return True
        if "NO" in ans and "SI" not in ans:
            return False
    except Exception:
        pass
    return None


# ── App Flask ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("ia_call")


def _log(tag: str, msg: str, *args) -> None:
    log.info("[IA][%s] " + msg, tag, *args)


def _log_exc(tag: str, msg: str, exc: BaseException) -> None:
    log.exception("[IA][%s] %s: %s", tag, msg, exc)


app = Flask(__name__)

PORT = int(os.getenv("PORT", "5000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

TWILIO_SPEECH_TIMEOUT = os.getenv("TWILIO_SPEECH_TIMEOUT", "auto").strip()
TWILIO_GATHER_TIMEOUT = int(os.getenv("TWILIO_GATHER_TIMEOUT", "25"))
TWILIO_VOICE = os.getenv("TWILIO_VOICE", "Polly.Mia").strip()

client = OpenAI(api_key=OPENAI_API_KEY) if (OpenAI and OPENAI_API_KEY) else None

SOLICITUD_TELEFONICA_PATH = "/api/taxi/solicitud-telefonica"

GEOCODE_ENABLED = os.getenv("GEOCODE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y")
GEOCODE_PROVIDER = (os.getenv("GEOCODE_PROVIDER") or "nominatim").strip().lower()
NOMINATIM_URL = (os.getenv("NOMINATIM_URL") or "https://nominatim.openstreetmap.org/search").strip()
GEOCODE_COUNTRYCODES = (os.getenv("GEOCODE_COUNTRYCODES") or "co").strip()
GEOCODE_SUFFIX = (os.getenv("GEOCODE_SUFFIX") or "Popayán, Cauca, Colombia").strip()
GEOCODE_TIMEOUT = float(os.getenv("GEOCODE_TIMEOUT", "8"))
GEOCODE_VIEWBOX = (os.getenv("GEOCODE_VIEWBOX") or "-76.82,2.58,-76.42,2.32").strip()
GEOCODE_BOUNDED = os.getenv("GEOCODE_BOUNDED", "true").strip().lower() in ("1", "true", "yes", "y")

MAX_SILENCE_BEFORE_HANGUP = int(os.getenv("MAX_SILENCE_BEFORE_HANGUP", "3"))

_SESSION_LOCK = threading.Lock()
_SESSIONS: Dict[str, "CallSession"] = {}
SESSION_TTL_SEC = int(os.getenv("CALL_SESSION_TTL_SEC", "7200"))


@dataclass
class CallSession:
    call_sid: str
    state: str = "waiting_origin"
    origen_text: Optional[str] = None
    destino_text: Optional[str] = None
    wants_destino: Optional[bool] = None
    service_created: bool = False
    silence_count: int = 0
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()


STATE_WAITING_ORIGIN = "waiting_origin"
STATE_WAITING_DEST_DECISION = "waiting_dest_decision"
STATE_WAITING_DESTINATION = "waiting_destination"
STATE_SERVICE_CREATED = "service_created"
STATE_FINISHED = "finished"


def _prune_sessions() -> None:
    now = time.time()
    dead = [k for k, s in _SESSIONS.items() if now - s.updated_at > SESSION_TTL_SEC]
    for k in dead:
        _SESSIONS.pop(k, None)


def get_session(call_sid: str) -> CallSession:
    with _SESSION_LOCK:
        _prune_sessions()
        if call_sid not in _SESSIONS:
            _SESSIONS[call_sid] = CallSession(call_sid=call_sid)
        s = _SESSIONS[call_sid]
        s.touch()
        return s


def reset_session(call_sid: str) -> None:
    with _SESSION_LOCK:
        _SESSIONS.pop(call_sid, None)


def _is_localhost_url(url: str) -> bool:
    u = (url or "").lower()
    return "localhost" in u or "127.0.0.1" in u or "::1" in u


def get_backend_url() -> str:
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


def gather_process_speech_url() -> str:
    """
    URL completa del siguiente paso (Twilio POST tras <Gather>).
    - Si usas proxy Laravel: define TWILIO_GATHER_ACTION_URL=https://API/api/twilio/ia/process-speech
      (misma URL que ve Twilio; Laravel reenvía a este servicio /process_speech).
    - Si Twilio apunta solo a Render: PUBLIC_BASE_URL=https://tu.onrender.com y se usa .../process_speech aquí.
    """
    full = (os.getenv("TWILIO_GATHER_ACTION_URL") or "").strip()
    if full:
        return full
    return twilio_public_base_url().rstrip("/") + "/process_speech"


def post_solicitud_telefonica(payload: dict) -> bool:
    base = get_backend_url()
    url = build_solicitud_telefonica_url()

    _log("BACKEND_REQUEST", "BACKEND_URL (base)=%s", base)
    _log("BACKEND_REQUEST", "URL POST final=%s", url)

    if _is_localhost_url(base):
        _log("BACKEND_REQUEST", "ADVERTENCIA: base parece localhost (Render no alcanza tu PC).")

    if not backend_url_allows_post():
        _log("BACKEND_REQUEST", "POST omitido por política Render/localhost.")
        return False

    _log("BACKEND_REQUEST", "Payload JSON=%s", json.dumps(payload, ensure_ascii=False, default=str))

    t0 = time.perf_counter()
    try:
        res = requests.post(
            url,
            json=payload,
            headers=_backend_post_headers(base),
            timeout=25,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log("BACKEND_RESPONSE", "status_code=%s tiempo_ms=%.0f", res.status_code, elapsed_ms)
        _log("BACKEND_RESPONSE", "body (primeros 2500 chars)=%s", (res.text or "")[:2500])
        if res.status_code >= 400:
            _log("ERROR", "Backend HTTP error status=%s body=%s", res.status_code, (res.text or "")[:2000])
            return False
        return True
    except requests.Timeout as exc:
        _log_exc("ERROR", "Timeout esperando al backend (>25s)", exc)
        return False
    except requests.RequestException as exc:
        _log_exc("ERROR", "Error de red/DNS/SSL hacia backend", exc)
        return False


def _nominatim_geocode(query: str) -> Optional[Tuple[float, float, str]]:
    q = (query or "").strip()
    if not q:
        return None

    if GEOCODE_SUFFIX and GEOCODE_SUFFIX.lower() not in q.lower():
        q = f"{q}, {GEOCODE_SUFFIX}"

    headers = {
        "User-Agent": "ia-call/virtual-school-taxi (contact: admin)",
        "Accept": "application/json",
    }
    params: Dict[str, Any] = {
        "q": q,
        "format": "json",
        "limit": 8,
        "addressdetails": 0,
    }
    if GEOCODE_COUNTRYCODES:
        params["countrycodes"] = GEOCODE_COUNTRYCODES
    if GEOCODE_VIEWBOX and GEOCODE_BOUNDED:
        params["viewbox"] = GEOCODE_VIEWBOX
        params["bounded"] = "1"

    _log("BACKEND_REQUEST", "Geocode Nominatim q=%r", q)
    try:
        r = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=GEOCODE_TIMEOUT)
        if r.status_code != 200:
            _log("ERROR", "Geocode HTTP %s body=%s", r.status_code, (r.text or "")[:500])
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        for row in data:
            if not isinstance(row, dict):
                continue
            lat = float(row.get("lat") or 0.0)
            lon = float(row.get("lon") or 0.0)
            if abs(lat) < 1e-9 and abs(lon) < 1e-9:
                continue
            if in_popayan_bbox(lat, lon):
                name = str(row.get("display_name") or "")
                return lat, lon, name
        return None
    except Exception as exc:
        _log_exc("ERROR", "Geocode exception", exc)
        return None


def geocode_address(text: str) -> Optional[Tuple[float, float, str]]:
    if not GEOCODE_ENABLED or GEOCODE_PROVIDER != "nominatim":
        return None
    return _nominatim_geocode(text)


def _extract_json_object(raw: str) -> dict:
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def extract_pickup_address(user_text: str) -> Tuple[Optional[str], str]:
    if not client:
        t = (user_text or "").strip()
        return (t if len(t) > 3 else None, "Por favor dime con más detalle dónde te recogemos en Popayán.")
    prompt = f"""Eres un asistente para taxi en Popayán, Cauca, Colombia.
El usuario habla por teléfono. Extrae SOLO el punto de RECOGIDA (origen).
Si dice "de X a Y", "desde X hasta Y" o "de X hacia Y", el origen es X (no Y).
Barrios, Sena Norte/Sur, iglesias, centros comerciales, conjuntos y lugares comunes son válidos.
Responde SOLO JSON: {{"origen": "texto normalizado o null", "nota": "breve"}}
Si no hay ninguna ubicación clara, origen=null.
Texto del usuario:
{user_text}
"""
    try:
        result = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=12.0,
        )
        data = _extract_json_object(result.choices[0].message.content or "")
        o = data.get("origen")
        if o is None or str(o).strip().lower() in ("null", "none", ""):
            return None, "No alcanzamos a entender bien el punto de recogida. ¿Nos lo repites, por favor?"
        return str(o).strip(), ""
    except Exception as exc:
        _log_exc("OPENAI", "extract_pickup_address", exc)
        return None, "Hubo un problema técnico. Intenta decir de nuevo tu punto de recogida."


def extract_destination_address(user_text: str) -> Tuple[Optional[str], str]:
    if not client:
        t = (user_text or "").strip()
        return (t if len(t) > 2 else None, "Indica tu destino en Popayán, por favor.")
    prompt = f"""Popayán, Cauca, Colombia. Extrae SOLO la dirección o lugar de DESTINO del viaje.
Responde SOLO JSON: {{"destino": "texto o null", "nota": "breve"}}
Texto:
{user_text}
"""
    try:
        result = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=12.0,
        )
        data = _extract_json_object(result.choices[0].message.content or "")
        d = data.get("destino")
        if d is None or str(d).strip().lower() in ("null", "none", ""):
            return None, "¿Cuál es tu destino? Puedes decir calle, carrera, barrio o un lugar conocido en Popayán."
        return str(d).strip(), ""
    except Exception as exc:
        _log_exc("OPENAI", "extract_destination_address", exc)
        return None, "Repite el destino, por favor."


def build_payload(
    celular: Optional[str],
    origen: str,
    olat: float,
    olng: float,
    destino: Optional[str],
    dlat: float,
    dlng: float,
) -> dict:
    payload: Dict[str, Any] = {
        "pasajero_id": 1,
        "celular": celular,
        "pasajero_nombre": "Usuario Telefónico",
        "origen": origen,
        "origen_lat": float(olat),
        "origen_lng": float(olng),
        "clase_vehiculo": "TAXI",
        "precio_estimado": 0.0,
    }
    if destino and destino.strip():
        payload["destino"] = destino.strip()
        payload["destino_lat"] = float(dlat)
        payload["destino_lng"] = float(dlng)
    else:
        payload["destino"] = ""
        payload["destino_lat"] = 0.0
        payload["destino_lng"] = 0.0
    return payload


MSG_CIERRE_OK = (
    "Perfecto. Ya estamos gestionando una unidad hacia tu punto de recogida. "
    "En breve se comunicará contigo uno de nuestros conductores. "
    "Gracias por comunicarte con nosotros. Que tengas un excelente día."
)


def create_service_once(sess: CallSession, celular: Optional[str], origen: str, destino: Optional[str]) -> Tuple[bool, str]:
    if sess.service_created:
        _log("STATE", "Ignorando creación duplicada CallSid=%s", sess.call_sid)
        return True, MSG_CIERRE_OK

    g_o = geocode_address(origen)
    if not g_o:
        return False, (
            "No logramos ubicar ese punto dentro de Popayán. "
            "Por favor repite la dirección o el barrio con un poco más de detalle."
        )

    olat, olng, _ = g_o
    dlat, dlng = 0.0, 0.0
    if destino and destino.strip():
        g_d = geocode_address(destino)
        if not g_d:
            return False, (
                "No encontramos ese destino en Popayán. "
                "¿Puedes repetirlo indicando calle, carrera o un lugar conocido?"
            )
        dlat, dlng, _ = g_d

    payload = build_payload(celular, origen, olat, olng, destino.strip() if destino else None, dlat, dlng)
    if not post_solicitud_telefonica(payload):
        return False, (
            "No pudimos registrar tu solicitud en este momento. "
            "Por favor intenta de nuevo en unos segundos o contacta por la aplicación."
        )
    sess.service_created = True
    sess.state = STATE_SERVICE_CREATED
    return True, MSG_CIERRE_OK


def _gather_kwargs():
    return {
        "input": "speech",
        "language": "es-MX",
        "speech_timeout": TWILIO_SPEECH_TIMEOUT,
        "timeout": TWILIO_GATHER_TIMEOUT,
    }


def twiml_gather_message(msg: str) -> Tuple[str, int, Dict[str, str]]:
    process_url = gather_process_speech_url()
    response = VoiceResponse()
    gather = Gather(action=process_url, method="POST", **_gather_kwargs())
    gather.say(msg, voice=TWILIO_VOICE, language="es-MX")
    response.append(gather)
    return str(response), 200, {"Content-Type": "text/xml; charset=utf-8"}


def twiml_say_hangup(msg: str) -> Tuple[str, int, Dict[str, str]]:
    response = VoiceResponse()
    response.say(msg, voice=TWILIO_VOICE, language="es-MX")
    response.hangup()
    return str(response), 200, {"Content-Type": "text/xml; charset=utf-8"}


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
            "service": "ia-call-python",
            "backend_url_base": backend,
            "solicitud_url_completa": build_solicitud_telefonica_url(),
            "backend_post_blocked_on_render": blocked,
            "twilio_speech_timeout": TWILIO_SPEECH_TIMEOUT,
            "twilio_gather_timeout": TWILIO_GATHER_TIMEOUT,
            "twilio_voice": TWILIO_VOICE,
            "public_base_url": (os.getenv("PUBLIC_BASE_URL") or "").strip() or "inferido del request",
            "gather_action_url": gather_process_speech_url(),
        }
    ), 200


@app.route("/voice", methods=["GET", "POST"])
def voice():
    _log("VOICE", "Entrada llamada method=%s", request.method)
    call_sid = request.values.get("CallSid") or "unknown"
    sess = get_session(call_sid)
    if sess.state not in (
        STATE_WAITING_ORIGIN,
        STATE_WAITING_DEST_DECISION,
        STATE_WAITING_DESTINATION,
        STATE_SERVICE_CREATED,
        STATE_FINISHED,
    ):
        sess.state = STATE_WAITING_ORIGIN

    saludo = (
        "Hola, ¿cómo estás? Bienvenido a nuestro asistente virtual. "
        "¿Desde dónde te podemos recoger hoy? "
        "Puedes decirme una dirección, un barrio, o un lugar conocido en Popayán."
    )
    body, status, headers = twiml_gather_message(saludo)
    return body, status, headers


@app.route("/process_speech", methods=["GET", "POST"])
def process_speech():
    call_sid = request.values.get("CallSid") or "unknown"
    caller_id = request.values.get("From", "").replace("whatsapp:", "")
    texto_crudo = request.values.get("SpeechResult", "") or ""
    texto_usuario = texto_crudo.strip()

    _log("SPEECH", "CallSid=%s SpeechResult len=%s", call_sid, len(texto_crudo))

    sess = get_session(call_sid)

    if sess.state == STATE_FINISHED or (sess.service_created and sess.state == STATE_SERVICE_CREATED):
        body, status, headers = twiml_say_hangup("Gracias por tu llamada. Hasta pronto.")
        return body, status, headers

    if not texto_usuario:
        sess.silence_count += 1
        _log("SPEECH", "Silencio count=%s estado=%s", sess.silence_count, sess.state)

        if sess.silence_count >= MAX_SILENCE_BEFORE_HANGUP:
            reset_session(call_sid)
            body, status, headers = twiml_say_hangup(
                "No recibimos respuesta. Vamos a finalizar la llamada por ahora. "
                "Gracias por comunicarte con nosotros. Hasta pronto."
            )
            return body, status, headers

        if sess.state == STATE_WAITING_ORIGIN:
            if sess.silence_count == 1:
                msg = (
                    "¿Sigues ahí? Por favor, cuéntanos desde dónde podemos recogerte en Popayán. "
                    "Puede ser una calle, carrera o un barrio."
                )
            else:
                msg = (
                    "Seguimos atentos. Si deseas un taxi, indícanos tu punto de recogida. "
                    "Si no escuchamos respuesta en un momento, cerraremos la llamada."
                )
        elif sess.state == STATE_WAITING_DEST_DECISION:
            if sess.silence_count == 1:
                msg = (
                    "No logramos escucharte bien. ¿Quieres indicarnos también tu destino final? "
                    "Puedes decir sí o no."
                )
            else:
                msg = "¿Deseas decirnos el destino? Responde sí o no, por favor."
        elif sess.state == STATE_WAITING_DESTINATION:
            if sess.silence_count == 1:
                msg = "¿Sigues ahí? Indícanos tu destino final en Popayán, por favor."
            else:
                msg = "Repite el destino cuando puedas, o di no si prefieres indicarlo al conductor."
        else:
            msg = "¿Sigues ahí? Por favor responde cuando puedas."

        body, status, headers = twiml_gather_message(msg)
        return body, status, headers

    sess.silence_count = 0

    if sess.state == STATE_WAITING_ORIGIN:
        origen, hint = extract_pickup_address(texto_usuario)
        if not origen:
            body, status, headers = twiml_gather_message(hint or "Cuéntanos, por favor, dónde te recogemos en Popayán.")
            return body, status, headers

        sess.origen_text = origen
        sess.state = STATE_WAITING_DEST_DECISION
        msg = (
            "Perfecto. ¿Deseas indicarnos también tu destino final? "
            "Si deseas compartirlo, di sí. Si prefieres indicarlo directamente al conductor, di no."
        )
        body, status, headers = twiml_gather_message(msg)
        return body, status, headers

    if sess.state == STATE_WAITING_DEST_DECISION:
        decision = parse_si_no(texto_usuario, client, OPENAI_MODEL)
        if decision is None:
            body, status, headers = twiml_gather_message(
                "No te escuchamos claro. ¿Quieres decirnos el destino del viaje? "
                "Responde solo sí o no."
            )
            return body, status, headers
        if decision is False:
            ok, closing = create_service_once(sess, caller_id, sess.origen_text or "", None)
            if not ok:
                body, status, headers = twiml_gather_message(closing)
                return body, status, headers
            sess.state = STATE_FINISHED
            reset_session(call_sid)
            body, status, headers = twiml_say_hangup(closing)
            return body, status, headers

        sess.wants_destino = True
        sess.state = STATE_WAITING_DESTINATION
        body, status, headers = twiml_gather_message(
            "Muy bien. Por favor, indícanos tu destino final en Popayán. "
            "Puede ser una dirección, un barrio o un lugar conocido."
        )
        return body, status, headers

    if sess.state == STATE_WAITING_DESTINATION:
        dest, hint = extract_destination_address(texto_usuario)
        if not dest:
            body, status, headers = twiml_gather_message(hint or "¿Cuál es tu destino en Popayán?")
            return body, status, headers

        sess.destino_text = dest
        ok, closing = create_service_once(sess, caller_id, sess.origen_text or "", dest)
        if not ok:
            body, status, headers = twiml_gather_message(closing)
            return body, status, headers
        sess.state = STATE_FINISHED
        reset_session(call_sid)
        body, status, headers = twiml_say_hangup(closing)
        return body, status, headers

    body, status, headers = twiml_say_hangup("Gracias por comunicarte con nosotros. Hasta pronto.")
    return body, status, headers


if __name__ == "__main__":
    _log(
        "CONFIG",
        "Arranque puerto=%s BACKEND_URL_raw=%s base=%s url_post=%s RENDER=%s voz=%s PUBLIC_BASE_URL=%s",
        PORT,
        (os.getenv("BACKEND_URL") or ""),
        get_backend_url(),
        build_solicitud_telefonica_url(),
        os.getenv("RENDER", ""),
        TWILIO_VOICE,
        (os.getenv("PUBLIC_BASE_URL") or ""),
    )
    app.run(host="0.0.0.0", port=PORT, debug=False)
