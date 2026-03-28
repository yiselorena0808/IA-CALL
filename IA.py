import os
from flask import Flask, request
import json
import requests
from dotenv import load_dotenv
from twilio.twiml.voice_response import VoiceResponse, Gather

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

load_dotenv()

app = Flask(__name__)

PORT = int(os.getenv("PORT", "5000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()

client = OpenAI(api_key=OPENAI_API_KEY) if (OpenAI and OPENAI_API_KEY) else None

@app.route("/", methods=["GET"])
def home():
    return "Servidor activo."

@app.route("/health", methods=["GET"])
def health():
    return {"ok": True}, 200

@app.route("/voice", methods=["GET", "POST"])
def voice():
    print("=== /voice ===")
    print("Method:", request.method)
    print("Values:", dict(request.values))

    response = VoiceResponse()
    process_url = request.url_root.replace("http://", "https://").rstrip("/") + "/process_speech"

    gather = Gather(
        input="speech",
        action=process_url,
        method="POST",
        language="es-MX",
        speech_timeout="3",
        timeout=15
    )

    # Mensaje inicial en español
    gather.say(
        "Hola, soy tu asistente virtual. Desde dónde y hacia dónde necesitas viajar hoy.",
        voice="alice",
        language="es-MX"
    )
    response.append(gather)

    # Mensaje de fallback en español
    response.say(
        "No pude escucharte. Por favor vuelve a llamar o intenta de nuevo.",
        voice="alice",
        language="es-MX"
    )
    response.hangup()

    return str(response), 200, {"Content-Type": "text/xml"}

@app.route("/process_speech", methods=["GET", "POST"])
def process_speech():
    print("=== /process_speech ===")
    print("Method:", request.method)
    print("Values:", dict(request.values))

    response = VoiceResponse()
    texto_usuario = request.values.get("SpeechResult", "").strip()

    if not texto_usuario:
        response.say(
            "Disculpa, no te entendí bien. Por favor repite tu dirección y destino.",
            voice="alice",
            language="es-MX"
        )
        response.redirect("/voice", method="POST")
        return str(response), 200, {"Content-Type": "text/xml"}

    print(f"Cliente dijo: {texto_usuario}")
    respuesta_ia = procesar_con_ia(texto_usuario)

    if es_cierre(texto_usuario):
        response.say(respuesta_ia, voice="alice", language="es-MX")
        response.hangup()
        return str(response), 200, {"Content-Type": "text/xml"}

    process_url = request.url_root.rstrip("/") + "/process_speech"
    gather = Gather(
        input="speech",
        action=process_url,
        method="POST",
        language="es-MX",
        speech_timeout="3",
        timeout=15
    )
    gather.say(respuesta_ia, voice="alice", language="es-MX")
    response.append(gather)

    response.say("No escuché más información. Hasta luego.", voice="alice", language="es-MX")
    response.hangup()

    return str(response), 200, {"Content-Type": "text/xml"}

def es_cierre(texto: str) -> bool:
    texto_min = texto.lower()
    return any(word in texto_min for word in [
        "gracias", "eso es todo", "adiós", "adios", "no", "nada más", "nada mas"
    ])

def procesar_con_ia(texto: str) -> str:
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
            response_format={"type": "json_object"}
        )
        
        data = json.loads(result.choices[0].message.content)
        origen = data.get("origen")
        destino = data.get("destino")
        mensaje = data.get("mensaje", respuesta_simulada(texto))
        
        # Si la IA identificó origen y destino, llamamos al backend
        if origen and destino:
            print(f"[*] Origen: {origen} | Destino: {destino}. Enviando al backend...")
            backend_url = os.getenv("BACKEND_URL", "http://localhost:8000")
            payload = {
                "pasajero_id": 1, # Se debería obtener dinámicamente
                "pasajero_nombre": "Usuario Telefónico",
                "origen": origen,
                "destino": destino,
                "origen_lat": 0.0, # Requiere Geocoding real para funcionar completo en el map
                "origen_lng": 0.0,
                "destino_lat": 0.0,
                "destino_lng": 0.0,
                "clase_vehiculo": "TAXI",
                "precio_estimado": 10000 
            }
            try:
                # Utilizamos la nueva ruta dedicada para la IA que crea el ServicioMovil directamente
                res = requests.post(f"{backend_url}/api/taxi/solicitud-telefonica", json=payload, timeout=5)
                print("[*] Respuesta del Backend:", res.status_code, res.text)
            except Exception as req_err:
                print("[!] Error al conectar con el backend:", req_err)
                
        return mensaje

    except Exception as e:
        print("Error OpenAI:", e)
        return respuesta_simulada(texto)

def respuesta_simulada(texto: str) -> str:
    texto_min = texto.lower()

    if es_cierre(texto_min):
        return "Perfecto. Hemos recibido tu solicitud. Gracias por preferirnos. Que tengas un excelente día."

    if len(texto_min) > 10:
        return "Entendido. Estamos procesando tu ruta. Enseguida te enviaremos los datos del conductor. ¿Deseas agregar algo más?"

    return "Por favor indícame claramente la dirección exacta de recogida y tu destino."

if __name__ == "__main__":
    print(f"Iniciando servidor en puerto {PORT}...")
    app.run(host="0.0.0.0", port=PORT, debug=False)