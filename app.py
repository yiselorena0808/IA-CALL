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
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple
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

# ── Normalización calle/carrera (antes urban_address.py; inline para despliegue Render sin módulo extra) ─

MatchTypeUrban = Literal["cruce", "nomenclatura", "via_parcial", "referencia_libre"]


def _sanitize_match_type_urban(s: str) -> MatchTypeUrban:
    if s in ("cruce", "nomenclatura", "via_parcial", "referencia_libre"):
        return s  # type: ignore[return-value]
    return "referencia_libre"


@dataclass
class UrbanAddressResult:
    original_text: str = ""
    normalized_text: str = ""
    canonical: str = ""
    match_type: MatchTypeUrban = "referencia_libre"
    is_partial: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_text": self.original_text,
            "normalized_text": self.normalized_text,
            "canonical": self.canonical,
            "match_type": self.match_type,
            "is_partial": self.is_partial,
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "UrbanAddressResult":
        if not d:
            return UrbanAddressResult()
        return UrbanAddressResult(
            original_text=str(d.get("original_text") or ""),
            normalized_text=str(d.get("normalized_text") or ""),
            canonical=str(d.get("canonical") or ""),
            match_type=_sanitize_match_type_urban(str(d.get("match_type") or "referencia_libre")),
            is_partial=bool(d.get("is_partial")),
        )

    @staticmethod
    def empty() -> "UrbanAddressResult":
        return UrbanAddressResult()


_NUM_PART_URBAN = r"\d+[a-zA-ZáéíóúÁÉÍÓÚ°]*"


def _collapse_spaces_urban(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _unify_hyphen_cruce_urban(s: str) -> str:
    s = re.sub(r"\s*/\s*", " ", s)
    s = re.sub(
        rf"(calle\s+{_NUM_PART_URBAN})\s*-\s*(carrera\s+{_NUM_PART_URBAN})\b",
        r"\1 \2",
        s,
        flags=re.I,
    )
    s = re.sub(
        rf"(carrera\s+{_NUM_PART_URBAN})\s*-\s*(calle\s+{_NUM_PART_URBAN})\b",
        r"\1 \2",
        s,
        flags=re.I,
    )
    return s


def _expand_abbreviations_urban(s: str) -> str:
    t = s.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\bcalles\b", "calle", t)
    t = re.sub(r"\bcarreras\b", "carrera", t)
    t = re.sub(r"\bnúmero\b", "#", t)
    t = re.sub(r"\bnumero\b", "#", t)
    t = re.sub(r"\bnro\.?\b", "#", t)
    t = re.sub(r"\bn°\b", "#", t)
    t = re.sub(r"\bcl\.?\s+", "calle ", t)
    t = re.sub(r"\bcra\.?\s+", "carrera ", t)
    t = re.sub(r"\bkr\.?\s+", "carrera ", t)
    t = re.sub(r"\bk\.?\s+", "carrera ", t)
    t = re.sub(r"\bav\.?\s+", "avenida ", t)
    t = re.sub(r"\bdg\.?\s+", "diagonal ", t)
    t = re.sub(r"\btv\.?\s+", "transversal ", t)
    t = re.sub(r"\bc\s+(\d)", r"calle \1", t)
    t = re.sub(r"#\s*(\d+)\s+(\d+)\b", r"# \1-\2", t)
    t = _unify_hyphen_cruce_urban(t)
    return _collapse_spaces_urban(t)


def _title_via_urban(word: str) -> str:
    return word[:1].upper() + word[1:] if word else word


def _canonical_cruce_urban(c: str, k: str) -> str:
    return f"Calle {_title_via_urban(c)} con Carrera {_title_via_urban(k)}"


def normalize_urban_address(raw: str) -> UrbanAddressResult:
    original = (raw or "").strip()
    if not original:
        return UrbanAddressResult.empty()

    expanded = _expand_abbreviations_urban(original)

    m = re.search(
        rf"calle\s+({_NUM_PART_URBAN})\s+(?:con|y|e)\s+carrera\s+({_NUM_PART_URBAN})",
        expanded,
        re.I,
    )
    if m:
        c, k = m.group(1), m.group(2)
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=_canonical_cruce_urban(c, k),
            match_type="cruce",
            is_partial=False,
        )

    m = re.search(
        rf"carrera\s+({_NUM_PART_URBAN})\s+(?:con|y|e)\s+calle\s+({_NUM_PART_URBAN})",
        expanded,
        re.I,
    )
    if m:
        k, c = m.group(1), m.group(2)
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=_canonical_cruce_urban(c, k),
            match_type="cruce",
            is_partial=False,
        )

    m = re.search(
        rf"entre\s+calle\s+({_NUM_PART_URBAN})\s+y\s+carrera\s+({_NUM_PART_URBAN})\b",
        expanded,
        re.I,
    )
    if m:
        c, k = m.group(1), m.group(2)
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=_canonical_cruce_urban(c, k),
            match_type="cruce",
            is_partial=False,
        )

    m = re.search(
        rf"entre\s+carrera\s+({_NUM_PART_URBAN})\s+y\s+calle\s+({_NUM_PART_URBAN})\b",
        expanded,
        re.I,
    )
    if m:
        k, c = m.group(1), m.group(2)
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=_canonical_cruce_urban(c, k),
            match_type="cruce",
            is_partial=False,
        )

    m = re.search(
        rf"calle\s+({_NUM_PART_URBAN})\s+esquina\s+carrera\s+({_NUM_PART_URBAN})\b",
        expanded,
        re.I,
    )
    if m:
        c, k = m.group(1), m.group(2)
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=_canonical_cruce_urban(c, k),
            match_type="cruce",
            is_partial=False,
        )

    m = re.search(
        rf"carrera\s+({_NUM_PART_URBAN})\s+esquina\s+calle\s+({_NUM_PART_URBAN})\b",
        expanded,
        re.I,
    )
    if m:
        k, c = m.group(1), m.group(2)
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=_canonical_cruce_urban(c, k),
            match_type="cruce",
            is_partial=False,
        )

    m = re.search(
        rf"calle\s+({_NUM_PART_URBAN})\s+carrera\s+({_NUM_PART_URBAN})\b",
        expanded,
        re.I,
    )
    if m:
        c, k = m.group(1), m.group(2)
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=_canonical_cruce_urban(c, k),
            match_type="cruce",
            is_partial=False,
        )

    m = re.search(
        rf"carrera\s+({_NUM_PART_URBAN})\s+calle\s+({_NUM_PART_URBAN})\b",
        expanded,
        re.I,
    )
    if m:
        k, c = m.group(1), m.group(2)
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=_canonical_cruce_urban(c, k),
            match_type="cruce",
            is_partial=False,
        )

    m = re.search(
        rf"(calle|carrera)\s+({_NUM_PART_URBAN})\s*#\s*(\d+)\s*[-–]?\s*(\d+)",
        expanded,
        re.I,
    )
    if m:
        via, n1, p1, p2 = m.group(1), m.group(2), m.group(3), m.group(4)
        via_t = "Calle" if via.lower() == "calle" else "Carrera"
        canon = f"{via_t} {_title_via_urban(n1)} # {p1}-{p2}"
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=canon,
            match_type="nomenclatura",
            is_partial=False,
        )

    m = re.search(rf"\bcalle\s+({_NUM_PART_URBAN})(?:\s+(norte|sur|este|oeste))?\b", expanded, re.I)
    if m and not re.search(r"calle\s+\d+.*(?:con|y|e)\s+carrera", expanded, re.I):
        n = m.group(1)
        ori = m.group(2)
        tail = f" {ori}" if ori else ""
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=f"Calle {_title_via_urban(n)}{tail}",
            match_type="via_parcial",
            is_partial=True,
        )

    m = re.search(rf"\bcarrera\s+({_NUM_PART_URBAN})(?:\s+(norte|sur|este|oeste))?\b", expanded, re.I)
    if m and not re.search(r"carrera\s+\d+.*(?:con|y|e)\s+calle", expanded, re.I):
        n = m.group(1)
        ori = m.group(2)
        tail = f" {ori}" if ori else ""
        return UrbanAddressResult(
            original_text=original,
            normalized_text=expanded,
            canonical=f"Carrera {_title_via_urban(n)}{tail}",
            match_type="via_parcial",
            is_partial=True,
        )

    return UrbanAddressResult(
        original_text=original,
        normalized_text=expanded,
        canonical=_collapse_spaces_urban(original)[:300],
        match_type="referencia_libre",
        is_partial=False,
    )


def merge_urban_with_llm(raw: str, llm_location: Optional[str]) -> UrbanAddressResult:
    u_audio = normalize_urban_address(raw or "")
    if u_audio.match_type in ("cruce", "nomenclatura", "via_parcial"):
        return u_audio

    u_llm = normalize_urban_address((llm_location or "").strip())
    if u_llm.match_type in ("cruce", "nomenclatura", "via_parcial"):
        u_llm.original_text = raw or u_llm.original_text
        return u_llm

    text = (llm_location or raw or "").strip()
    if not text:
        return UrbanAddressResult.empty()

    return UrbanAddressResult(
        original_text=raw,
        normalized_text=u_audio.normalized_text or _collapse_spaces_urban((llm_location or raw or "").lower()),
        canonical=text,
        match_type="referencia_libre",
        is_partial=False,
    )


def build_geocode_query_chain(urban: UrbanAddressResult, display_fallback: str) -> List[str]:
    seen: set = set()
    out: List[str] = []

    def add(q: str) -> None:
        q = _collapse_spaces_urban(q)
        if not q or len(q) < 2:
            return
        key = q.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(q)

    if urban.canonical:
        add(urban.canonical)
    if urban.normalized_text and urban.normalized_text != urban.canonical:
        add(urban.normalized_text[:400])
    if display_fallback and display_fallback.strip():
        add(display_fallback.strip()[:400])
    if urban.original_text and urban.original_text.strip():
        add(urban.original_text.strip()[:400])
    return out


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

BACKEND_HTTP_TIMEOUT = float(os.getenv("BACKEND_HTTP_TIMEOUT", "60"))

GEOCODE_ENABLED = os.getenv("GEOCODE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y")
GEOCODE_PROVIDER = (os.getenv("GEOCODE_PROVIDER") or "nominatim").strip().lower()
NOMINATIM_URL = (os.getenv("NOMINATIM_URL") or "https://nominatim.openstreetmap.org/search").strip()
GEOCODE_COUNTRYCODES = (os.getenv("GEOCODE_COUNTRYCODES") or "co").strip()
GEOCODE_SUFFIX = (os.getenv("GEOCODE_SUFFIX") or "Popayán, Cauca, Colombia").strip()
GEOCODE_TIMEOUT = float(os.getenv("GEOCODE_TIMEOUT", "5"))
GEOCODE_VIEWBOX = (os.getenv("GEOCODE_VIEWBOX") or "-76.82,2.58,-76.42,2.32").strip()
GEOCODE_BOUNDED = os.getenv("GEOCODE_BOUNDED", "true").strip().lower() in ("1", "true", "yes", "y")
# Nominatim público: máx. ~1 req/s por IP; paralelo + ráfagas → 429
GEOCODE_MIN_INTERVAL_SEC = max(0.5, float(os.getenv("GEOCODE_MIN_INTERVAL_SEC", "1.1")))
GEOCODE_MAX_RETRIES = max(1, int(os.getenv("GEOCODE_MAX_RETRIES", "4")))
GEOCODE_CACHE_SIZE = max(16, int(os.getenv("GEOCODE_CACHE_SIZE", "256")))
GEOCODE_USER_AGENT = (os.getenv("GEOCODE_USER_AGENT") or "").strip() or (
    "ia-call/virtual-school-taxi (https://github.com; contact: set GEOCODE_USER_AGENT)"
)

_GEOCODE_CACHE: OrderedDict = OrderedDict()
_GEOCODE_CACHE_LOCK = threading.Lock()
_NOMINATIM_HTTP_LOCK = threading.Lock()
_NOMINATIM_LAST_REQUEST_END = 0.0

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
    # Metadatos dirección urbana (JSON-compatible para backend operador)
    origen_ia: Optional[Dict[str, Any]] = None
    destino_ia: Optional[Dict[str, Any]] = None

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
            timeout=BACKEND_HTTP_TIMEOUT,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log("BACKEND_RESPONSE", "status_code=%s tiempo_ms=%.0f", res.status_code, elapsed_ms)
        _log("BACKEND_RESPONSE", "body (primeros 2500 chars)=%s", (res.text or "")[:2500])
        if res.status_code >= 400:
            _log("ERROR", "Backend HTTP error status=%s body=%s", res.status_code, (res.text or "")[:2000])
            return False
        return True
    except requests.Timeout as exc:
        _log_exc("ERROR", f"Timeout esperando al backend (>{BACKEND_HTTP_TIMEOUT:g}s)", exc)
        return False
    except requests.RequestException as exc:
        _log_exc("ERROR", "Error de red/DNS/SSL hacia backend", exc)
        return False


def _geocode_cache_get(key: str) -> Optional[Tuple[float, float, str]]:
    with _GEOCODE_CACHE_LOCK:
        if key in _GEOCODE_CACHE:
            _GEOCODE_CACHE.move_to_end(key)
            return _GEOCODE_CACHE[key]
    return None


def _geocode_cache_set(key: str, val: Tuple[float, float, str]) -> None:
    with _GEOCODE_CACHE_LOCK:
        _GEOCODE_CACHE[key] = val
        _GEOCODE_CACHE.move_to_end(key)
        while len(_GEOCODE_CACHE) > GEOCODE_CACHE_SIZE:
            _GEOCODE_CACHE.popitem(last=False)


def _nominatim_geocode(query: str) -> Optional[Tuple[float, float, str]]:
    global _NOMINATIM_LAST_REQUEST_END
    q = (query or "").strip()
    if not q:
        return None

    if GEOCODE_SUFFIX and GEOCODE_SUFFIX.lower() not in q.lower():
        q = f"{q}, {GEOCODE_SUFFIX}"

    cached = _geocode_cache_get(q)
    if cached is not None:
        return cached

    headers = {
        "User-Agent": GEOCODE_USER_AGENT,
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
    r: Optional[requests.Response] = None
    try:
        for attempt in range(GEOCODE_MAX_RETRIES):
            with _NOMINATIM_HTTP_LOCK:
                now = time.monotonic()
                wait = _NOMINATIM_LAST_REQUEST_END + GEOCODE_MIN_INTERVAL_SEC - now
                if wait > 0:
                    time.sleep(wait)
                try:
                    r = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=GEOCODE_TIMEOUT)
                finally:
                    _NOMINATIM_LAST_REQUEST_END = time.monotonic()

            if r is None:
                return None

            if r.status_code == 200:
                break

            if r.status_code == 429 and attempt < GEOCODE_MAX_RETRIES - 1:
                ra = r.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else min(2.0**attempt, 30.0)
                except ValueError:
                    delay = min(2.0**attempt, 30.0)
                _log("GEOCODE", "Nominatim 429, reintento en %.1fs (intento %s/%s)", delay, attempt + 1, GEOCODE_MAX_RETRIES)
                time.sleep(delay)
                continue

            if r.status_code in (502, 503, 504) and attempt < GEOCODE_MAX_RETRIES - 1:
                delay = min(2.0**attempt, 15.0)
                _log("GEOCODE", "Nominatim %s, reintento en %.1fs", r.status_code, delay)
                time.sleep(delay)
                continue

            _log("ERROR", "Geocode HTTP %s body=%s", r.status_code, (r.text or "")[:500])
            return None

        if r is None or r.status_code != 200:
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
                out = (lat, lon, name)
                _geocode_cache_set(q, out)
                return out
        return None
    except Exception as exc:
        _log_exc("ERROR", "Geocode exception", exc)
        return None


def _city_short_label() -> str:
    part = (GEOCODE_SUFFIX or "Popayán, Cauca, Colombia").split(",")[0].strip()
    return part or "Popayán"


def _display_label_urban(urban: UrbanAddressResult) -> str:
    c = (urban.canonical or "").strip()
    if not c:
        return ""
    return f"{c}, {_city_short_label()}"


def _geocode_chain(queries: List[str]) -> Optional[Tuple[float, float, str]]:
    for q in queries:
        qq = (q or "").strip()
        if len(qq) < 2:
            continue
        r = _nominatim_geocode(qq)
        if r:
            _log("GEOCODE", "cadena OK q=%r", qq[:220])
            return r
    return None


def geocode_address(text: str) -> Optional[Tuple[float, float, str]]:
    if not GEOCODE_ENABLED or GEOCODE_PROVIDER != "nominatim":
        return None
    return _nominatim_geocode(text)


def geocode_origin_and_optional_destination(
    origen: str,
    destino: Optional[str],
    origen_queries: Optional[List[str]] = None,
    destino_queries: Optional[List[str]] = None,
) -> Tuple[Optional[Tuple[float, float, str]], Optional[Tuple[float, float, str]]]:
    """
    Geocodifica en serie: Nominatim público no admite peticiones paralelas (~1 req/s por IP).
    Cada pierna puede pasar varias variantes de texto (normalizada → parcial → libre).
    """
    if not GEOCODE_ENABLED or GEOCODE_PROVIDER != "nominatim":
        return None, None
    o = (origen or "").strip()
    if not o:
        return None, None
    d = (destino or "").strip()
    o_q = origen_queries if origen_queries is not None else [o]
    d_q = destino_queries if destino_queries is not None else ([d] if d else [])
    t0 = time.perf_counter()
    try:
        g_o = _geocode_chain(o_q)
        if not d:
            g_d = None
        else:
            g_d = _geocode_chain(d_q) if d_q else _nominatim_geocode(d)
    except Exception as exc:
        _log_exc("ERROR", "geocode origen/destino", exc)
        g_o, g_d = None, None
    _log(
        "GEOCODE",
        "serie OK origen=%s destino=%s tiempo_ms=%.0f",
        g_o is not None,
        g_d is not None,
        (time.perf_counter() - t0) * 1000.0,
    )
    return g_o, g_d


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
Prioriza direcciones por calle y/o carrera: cruces (calle 5 con carrera 9, calle 5 carrera 9, entre calle X y carrera Y),
nomenclatura con placa (carrera 6 # 12-34), una sola vía (calle 15, cl 25 norte, cra 8). Abreviaturas: cl, cra, kr, k.
Barrios y lugares conocidos solo si no hay calle/carrera clara.
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
            timeout=8.0,
        )
        data = _extract_json_object(result.choices[0].message.content or "")
        o = data.get("origen")
        if o is None or str(o).strip().lower() in ("null", "none", ""):
            fb = (user_text or "").strip()
            if len(fb) >= 4:
                _log("OPENAI", "extract_pickup_address fallback texto usuario (origen vacío en JSON)")
                return fb, ""
            return None, (
                "No alcanzamos a entender bien el punto de recogida. ¿Nos lo repites, por favor? "
                "Intenta decir una calle, carrera, barrio o lugar conocido en Popayán."
            )
        return str(o).strip(), ""
    except Exception as exc:
        _log_exc("OPENAI", "extract_pickup_address", exc)
        fb = (user_text or "").strip()
        if len(fb) >= 4:
            _log("OPENAI", "extract_pickup_address fallback tras excepción OpenAI")
            return fb, ""
        return None, "Hubo un problema técnico. Intenta decir de nuevo tu punto de recogida."


def extract_destination_address(user_text: str) -> Tuple[Optional[str], str]:
    if not client:
        t = (user_text or "").strip()
        return (t if len(t) > 2 else None, "Indica tu destino en Popayán, por favor.")
    prompt = f"""Popayán, Cauca, Colombia. Extrae SOLO la dirección o lugar de DESTINO del viaje.
Prioriza calle y carrera: cruces (calle 5 con carrera 9, calle 5 carrera 9, entre calle X y carrera Y),
placas (carrera 6 # 12-34), una vía (calle 15 norte). Abreviaturas: cl, cra, kr, k.
Si no hay nomenclatura de vías, acepta lugar conocido o barrio.
Responde SOLO JSON: {{"destino": "texto o null", "nota": "breve"}}
Texto:
{user_text}
"""
    try:
        result = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=8.0,
        )
        data = _extract_json_object(result.choices[0].message.content or "")
        d = data.get("destino")
        if d is None or str(d).strip().lower() in ("null", "none", ""):
            fb = (user_text or "").strip()
            if len(fb) >= 3:
                _log("OPENAI", "extract_destination_address fallback texto usuario")
                return fb, ""
            return None, "¿Cuál es tu destino? Puedes decir calle, carrera, barrio o un lugar conocido en Popayán."
        return str(d).strip(), ""
    except Exception as exc:
        _log_exc("OPENAI", "extract_destination_address", exc)
        fb = (user_text or "").strip()
        if len(fb) >= 3:
            return fb, ""
        return None, "Repite el destino, por favor."


def build_payload(
    celular: Optional[str],
    origen: str,
    olat: float,
    olng: float,
    destino: Optional[str],
    dlat: float,
    dlng: float,
    ia_meta: Optional[Dict[str, Any]] = None,
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
    if ia_meta:
        payload["ia_address_meta"] = json.dumps(ia_meta, ensure_ascii=False)
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

    t0 = time.perf_counter()
    dest_norm = destino.strip() if destino else None
    u_o = UrbanAddressResult.from_dict(sess.origen_ia or {})
    u_d = UrbanAddressResult.from_dict(sess.destino_ia or {}) if dest_norm else UrbanAddressResult.empty()

    q_o = build_geocode_query_chain(u_o, origen)
    q_d = build_geocode_query_chain(u_d, dest_norm or "") if dest_norm else None

    _log(
        "ADDRESS",
        "geocode entrada origen raw=%r norm=%r tipo=%s parcial=%s queries=%s",
        u_o.original_text,
        u_o.normalized_text,
        u_o.match_type,
        u_o.is_partial,
        [x[:80] for x in q_o[:5]],
    )
    if dest_norm:
        _log(
            "ADDRESS",
            "geocode entrada destino raw=%r norm=%r tipo=%s parcial=%s queries=%s",
            u_d.original_text,
            u_d.normalized_text,
            u_d.match_type,
            u_d.is_partial,
            [x[:80] for x in (q_d or [])[:5]],
        )

    g_o, g_d = geocode_origin_and_optional_destination(origen, dest_norm, q_o, q_d if dest_norm else None)
    if not g_o:
        _log("GEOCODE", "create_service_once origen sin resultado tras cadena")
        return False, (
            "No logramos identificar esa dirección en este momento. "
            "Por favor repite tu punto de recogida. Intenta decir una calle, carrera, barrio o lugar conocido en Popayán."
        )

    olat, olng, geo_o = g_o
    _log("GEOCODE", "create_service_once origen hit display_name=%r", (geo_o or "")[:200])
    dlat, dlng = 0.0, 0.0
    geo_d = ""
    if dest_norm:
        if not g_d:
            _log("GEOCODE", "create_service_once destino sin resultado tras cadena")
            return False, (
                "No encontramos ese destino en Popayán en este momento. "
                "¿Puedes repetirlo indicando calle, carrera o un lugar conocido?"
            )
        dlat, dlng, geo_d = g_d
        _log("GEOCODE", "create_service_once destino hit display_name=%r", (geo_d or "")[:200])

    _log(
        "GEOCODE",
        "create_service_once geocode_ms=%.0f origen_ok=%s dest_ok=%s",
        (time.perf_counter() - t0) * 1000.0,
        True,
        bool(dest_norm and g_d),
    )

    ia_meta: Dict[str, Any] = {
        "origen": {
            **(sess.origen_ia or {}),
            "display_label": _display_label_urban(u_o) or origen,
            "geocoded_preview": (geo_o or "")[:300],
        },
    }
    if dest_norm:
        ia_meta["destino"] = {
            **(sess.destino_ia or {}),
            "display_label": _display_label_urban(u_d) or (dest_norm or ""),
            "geocoded_preview": (geo_d or "")[:300],
        }

    payload = build_payload(celular, origen, olat, olng, dest_norm, dlat, dlng, ia_meta=ia_meta)
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
        "Hola, Bienvenido soy tu asistente virtual. "
        "¿Desde dónde te podemos recoger hoy? "
        "Puedes decirme una dirección de recogida."
    )
    body, status, headers = twiml_gather_message(saludo)
    return body, status, headers


def _speech_from_request() -> str:
    """Twilio puede enviar el resultado bajo distintas claves según versión / Gather."""
    for key in ("SpeechResult", "StableSpeechResult", "UnstableSpeechResult"):
        v = (request.values.get(key) or "").strip()
        if v:
            return v
    return ""


@app.route("/process_speech", methods=["GET", "POST"])
def process_speech():
    call_sid = request.values.get("CallSid") or "unknown"
    caller_id = request.values.get("From", "").replace("whatsapp:", "")
    texto_crudo = request.values.get("SpeechResult", "") or ""
    texto_usuario = _speech_from_request()
    sess = get_session(call_sid)

    _log(
        "SPEECH",
        "CallSid=%s estado=%s SpeechResult_len=%s texto_len=%s preview=%r",
        call_sid,
        sess.state,
        len(texto_crudo),
        len(texto_usuario),
        (texto_usuario[:120] + "…") if len(texto_usuario) > 120 else texto_usuario,
    )

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
        origen_llm, hint = extract_pickup_address(texto_usuario)
        urban_o = merge_urban_with_llm(texto_usuario, origen_llm)
        origen = (urban_o.canonical or (origen_llm or "").strip() or "").strip()
        if not origen:
            origen = (texto_usuario or "").strip()
        sess.origen_ia = urban_o.to_dict()
        sess.origen_text = origen
        _log(
            "ADDRESS",
            "waiting_origin raw=%r norm=%r tipo=%s parcial=%s canon=%r display=%r",
            urban_o.original_text,
            urban_o.normalized_text,
            urban_o.match_type,
            urban_o.is_partial,
            urban_o.canonical,
            _display_label_urban(urban_o),
        )
        _log("STATE", "waiting_origin extraído origen=%r hint_len=%s", origen, len(hint or ""))
        if not origen or len(origen) < 2:
            body, status, headers = twiml_gather_message(
                hint
                or "No logramos identificar esa dirección en este momento. ¿Podrías repetir tu punto de recogida? "
                "Intenta decir una calle, carrera, barrio o lugar conocido en Popayán."
            )
            return body, status, headers

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
        dest_llm, hint = extract_destination_address(texto_usuario)
        urban_d = merge_urban_with_llm(texto_usuario, dest_llm)
        dest = (urban_d.canonical or (dest_llm or "").strip() or "").strip()
        if not dest:
            dest = (texto_usuario or "").strip()
        sess.destino_ia = urban_d.to_dict()
        sess.destino_text = dest
        _log(
            "ADDRESS",
            "waiting_destination raw=%r norm=%r tipo=%s parcial=%s canon=%r display=%r",
            urban_d.original_text,
            urban_d.normalized_text,
            urban_d.match_type,
            urban_d.is_partial,
            urban_d.canonical,
            _display_label_urban(urban_d),
        )
        _log("STATE", "waiting_destination extraído destino=%r", dest)
        if not dest or len(dest) < 2:
            body, status, headers = twiml_gather_message(
                hint or "No logramos entender el destino. ¿Podrías repetirlo? Di una calle, carrera, barrio o lugar conocido en Popayán."
            )
            return body, status, headers

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
