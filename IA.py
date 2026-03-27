import os
import datetime
from flask import Flask, request
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

HTML_LOG_FILE = "historial_conversacion.html"

def registrar_mensaje(emisor: str, mensaje: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not os.path.exists(HTML_LOG_FILE):
        with open(HTML_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html>\n<html>\n<head>\n<meta charset='UTF-8'>\n<title>Historial de Conversación</title>\n")
            f.write("<style>\nbody{font-family: Arial, sans-serif; margin: 20px; background-color: #f9f9f9;}\n")
            f.write(".message{margin-bottom: 15px; padding: 15px; border-radius: 8px; max-width: 80%; line-height: 1.5;}\n")
            f.write(".ia{background-color: #e3f2fd; border-left: 5px solid #2196f3; margin-right: auto;}\n")
            f.write(".usuario{background-color: #f1f8e9; border-left: 5px solid #8bc34a; margin-left: auto; color: #333;}\n")
            f.write(".timestamp{font-size: 0.8em; color: #666; display: block; margin-top: 5px;}\n</style>\n</head>\n<body>\n")
            f.write("<h2>Historial de Conversación</h2>\n")
            
    with open(HTML_LOG_FILE, "a", encoding="utf-8") as f:
        clase = "ia" if emisor.upper() == "IA" else "usuario"
        f.write(f"<div class='message {clase}'>\n")
        f.write(f"<strong>{emisor}:</strong> {mensaje.strip()}<br>\n")
        f.write(f"<span class='timestamp'>{timestamp}</span>\n")
        f.write("</div>\n")

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
        speech_timeout="auto",
        timeout=5
    )

    # Mensaje inicial en español
    mensaje_inicial = "Hola, soy tu asistente virtual. Desde dónde y hacia dónde necesitas viajar hoy."
    registrar_mensaje("IA", mensaje_inicial)
    gather.say(
        mensaje_inicial,
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
        mensaje_disculpa = "Disculpa, no te entendí bien. Por favor repite tu dirección y destino."
        registrar_mensaje("IA", mensaje_disculpa)
        response.say(
            mensaje_disculpa,
            voice="alice",
            language="es-MX"
        )
        response.redirect("/voice", method="POST")
        return str(response), 200, {"Content-Type": "text/xml"}

    print(f"Cliente dijo: {texto_usuario}")
    registrar_mensaje("Usuario", texto_usuario)
    
    respuesta_ia = procesar_con_ia(texto_usuario)
    registrar_mensaje("IA", respuesta_ia)

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
        speech_timeout="auto",
        timeout=5
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
Responde en español, claro, corto y natural.

Reglas:
- Si el usuario dice origen y destino, confirma la solicitud.
- Si falta origen o destino, pide solo lo que falta.
- Si el usuario quiere terminar, despídete.
- No uses texto largo.

Mensaje del usuario:
{texto}
"""
        result = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt
        )
        return result.output_text.strip() or respuesta_simulada(texto)
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