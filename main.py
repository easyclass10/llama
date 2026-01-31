import os
import asyncio
import random
from flask import Flask, request, jsonify
from flask_cors import CORS
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.phone import RequestCallRequest
from telethon.tl.types import PhoneCallProtocol

app = Flask(__name__)
CORS(app)

# Configuración desde Variables de Entorno de Render
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
# Aquí pegaremos el string que generaremos en el paso 2
SESSION_STR = os.environ.get("SESSION_STRING", "")

async def telegram_task(target, message):
    # Usamos StringSession para no depender de archivos .session
    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await client.connect()
    
    try:
        if not await client.is_user_authorized():
            return {"error": "La sesión ha expirado. Genera una nueva."}

        # 1. Enviar Mensaje
        await client.send_message(target, message)

        # 2. Solicitar Llamada
        await client(RequestCallRequest(
            user_id=target,
            random_id=random.randint(0, 0x7fffffff),
            g_a_hash=os.urandom(32),
            protocol=PhoneCallProtocol(
                udp_p2p=True,
                udp_reflector=True,
                min_layer=92,
                max_layer=92,
                library_versions=['1.0.0']
            ),
            video=False
        ))
        return {"status": "Mensaje y llamada enviados con éxito"}
    
    except Exception as e:
        return {"error": str(e)}
    finally:
        await client.disconnect()

@app.route('/ejecutar', methods=['POST'])
def handle_request():
    data = request.json
    usuario = data.get('usuario_destino')
    mensaje = data.get('mensaje')
    
    if not usuario or not mensaje:
        return jsonify({"error": "Faltan datos"}), 400

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    resultado = loop.run_until_complete(telegram_task(usuario, mensaje))
    return jsonify(resultado)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)