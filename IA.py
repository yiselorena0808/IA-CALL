import os
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
PORT = int(os.getenv("PORT", os.getenv("PORT_CALLS", 5001)))
DEBUG = os.getenv("FLASK_DEBUG", "True").lower() in ("true", "1", "t")

@app.route("/voice", methods=['GET', 'POST'])
def voice():
    """Ruta inicial cuando entra la llamada de voz"""
    response = VoiceResponse()
    
    # Mensaje de bienvenida de Taxi PDX
    mensaje_bienvenida = "Hola, soy tu asistente virtual. ¿Desde dónde y hacia dónde necesitas viajar hoy?"
    
    # La etiqueta <Gather> escucha la respuesta de voz del usuario y la transcribe a texto
    # language='es-ES' configura el reconocimiento en español, o 'en-US' si es inglés
    gather = Gather(input='speech', action='/process_speech', language='es-ES', speechTimeout='auto')
    
    # voice='Polly.Mia' usa una voz neural realista de Amazon Polly disponible en Twilio
    gather.say(mensaje_bienvenida, voice='Polly.Mia')
    
    response.append(gather)
    
    # Si el usuario se queda en silencio y no dice nada
    response.say("No hemos escuchado tu respuesta. Por favor llama de nuevo. Hasta luego.", voice='Polly.Mia')
    response.hangup()
    
    return str(response)

@app.route("/process_speech", methods=['GET', 'POST'])
def process_speech():
    """Ruta que procesa lo que el usuario dijo durante la llamada"""
    response = VoiceResponse()
    
    # Twilio nos envía la transcripción de voz en la variable 'SpeechResult'
    if 'SpeechResult' in request.values:
        texto_usuario = request.values['SpeechResult']
        print(f"📞 El cliente dijo: {texto_usuario}")
        
        # Aquí se conecta la transcripción con tu IA (ej. ChatGPT/OpenAI) 
        # para entender direcciones y generar una respuesta dinámica.
        respuesta_ia = procesar_con_ia(texto_usuario)
        
        # Retornamos la respuesta hablada al usuario y volvemos a escuchar si es necesario
        gather = Gather(input='speech', action='/process_speech', language='es-ES', speechTimeout='auto')
        gather.say(respuesta_ia, voice='Polly.Mia')
        response.append(gather)
    else:
        # En caso de que falle el reconocimiento de voz
        response.say("Disculpa, no te he entendido bien. Por favor, repite tu pedido.", voice='Polly.Mia')
        response.redirect('/voice')
        
    return str(response)


def procesar_con_ia(texto):
    """
    Función simulada: Aquí conectarías a la API de OpenAI (ChatGPT) pasándole el texto
    para que extraiga el origen, destino y genere la respuesta de forma natural.
    """
    texto_min = texto.lower()
    if any(word in texto_min for word in ["gracias", "eso es todo", "adiós", "no", "nada más"]):
        return "Perfecto, un conductor de Taxi P D X estará contigo pronto. Gracias por preferirnos. Que tengas un excelente día."
    elif len(texto_min) > 10:
        return "Entendido. Procesando tu ruta con nuestro sistema. Enseguida te enviaremos un SMS o WhatsApp con los datos de tu conductor. ¿Deseas agregar algo más a tu pedido?"
    else:
        return "Por favor, indícame claramente la dirección exacta de recogida y tu destino dentro de la zona P D X."

if __name__ == "__main__":
    print(f"☎️ Iniciando el servidor de recepción de llamadas (Taxi PDX) en el puerto {PORT}...")
    app.run(host='0.0.0.0', port=PORT, debug=DEBUG)