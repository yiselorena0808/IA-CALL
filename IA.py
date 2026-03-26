import os
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
DEBUG = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")

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
    process_url = request.url_root.rstrip("/") + "/process_speech"

    gather = Gather(
        input="speech",
        action=process_url,
        method="POST",
        language="es-MX",
        speech_timeout="auto",
        timeout=5
    )

    gather.say(
        "Hola, soy tu asistente virtual. Desde dónde y hacia dónde necesitas viajar hoy.",
        voice="alice",
        language="es-ES"
    )
    response.append(gather)

    response.say(
        "No pude escucharte. Por favor vuelve a llamar o intenta de nuevo.",
        voice="alice",
        language="es-ES"
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
            language="es-ES"
        )
        response.redirect("/voice", method="POST")
        return str(response), 200, {"Content-Type": "text/xml"}

    print(f"Cliente dijo: {texto_usuario}")
    respuesta_ia = procesar_con_ia(texto_usuario)

    if es_cierre(texto_usuario):
        response.say(
            respuesta_ia,
            voice="alice",
            language="es-ES"
        )
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

    gather.say(
        respuesta_ia,
        voice="alice",
        language="es-ES"
    )
    response.append(gather)

    response.say(
        "No escuché más información. Hasta luego.",
        voice="alice",
        language="es-ES"
    )
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
- No uses listas ni texto largo.

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
        return "Entendido. Estamos procesando tu ruta. Enseguida te enviaremos los datos del conductor. Deseas agregar algo más."

    return "Por favor indícame claramente la dirección exacta de recogida y tu destino."


if __name__ == "__main__":
    print(f"Iniciando servidor en puerto {PORT}...")
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)