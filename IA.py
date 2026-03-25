import os
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# En local usa 5000. En Render usará PORT automáticamente.
PORT = int(os.getenv("PORT", "5000"))
DEBUG = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")


@app.route("/", methods=["GET"])
def home():
    return "Servidor activo."


@app.route("/voice", methods=["GET", "POST"])
def voice():
    print("=== /voice ===")
    print("Method:", request.method)
    print("Values:", dict(request.values))

    response = VoiceResponse()

    # URL absoluta para que Twilio siempre sepa a dónde enviar el resultado de voz
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
        voice="Polly.Mia"
    )

    response.append(gather)

    response.say(
        "No pude escucharte. Por favor vuelve a llamar o intenta de nuevo.",
        voice="Polly.Mia"
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

    if texto_usuario:
        print(f"Cliente dijo: {texto_usuario}")

        respuesta_ia = procesar_con_ia(texto_usuario)

        process_url = request.url_root.rstrip("/") + "/process_speech"

        gather = Gather(
            input="speech",
            action=process_url,
            method="POST",
            language="es-MX",
            speech_timeout="auto",
            timeout=5
        )
        gather.say(respuesta_ia, voice="Polly.Mia")
        response.append(gather)

        response.say("No escuché más información. Hasta luego.", voice="Polly.Mia")
        response.hangup()
    else:
        response.say(
            "Disculpa, no te entendí bien. Por favor repite tu dirección y destino.",
            voice="Polly.Mia"
        )
        response.redirect("/voice", method="POST")

    return str(response), 200, {"Content-Type": "text/xml"}


def procesar_con_ia(texto: str) -> str:
    """
    Simulación de IA.
    Luego aquí puedes conectar OpenAI para extraer origen y destino.
    """
    texto_min = texto.lower()

    if any(word in texto_min for word in [
        "gracias", "eso es todo", "adiós", "adios", "no", "nada más", "nada mas"
    ]):
        return "Perfecto. Hemos recibido tu solicitud. Gracias por preferirnos. Que tengas un excelente día."

    if len(texto_min) > 10:
        return "Entendido. Estamos procesando tu ruta. Enseguida te enviaremos los datos del conductor. Deseas agregar algo más."

    return "Por favor indícame claramente la dirección exacta de recogida y tu destino."


if __name__ == "__main__":
    print(f"Iniciando servidor en puerto {PORT}...")
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)