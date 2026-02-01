import os
import asyncio
import random
from flask import Flask, request, jsonify
from flask_cors import CORS
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.phone import RequestCallRequest
from telethon.tl.types import PhoneCallProtocol
import hashlib

app = Flask(__name__)
CORS(app)

# Configuración desde Variables de Entorno de Render
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
# Aquí pegaremos el string que generaremos en el paso 2
SESSION_STR = os.environ.get("SESSION_STRING", "")

async def telegram_task(target, message):
    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    await client.connect()
    
    try:
        if not await client.is_user_authorized():
            return {"error": "La sesión ha expirado."}

        # --- CAMBIO AQUÍ: Obtener la entidad completa ---
        # Esto resuelve el error "Invalid object ID"
        try:
            entity = await client.get_input_entity(target)
        except Exception as e:
            return {"error": f"No se encontró al usuario: {str(e)}"}

        # 1. Enviar Mensaje usando la entidad resuelta
        await client.send_message(entity, message)

        # 2. Solicitar Llamada con un Hash más estructurado
        # Nota: Generamos un hash que Telegram acepte como válido para el protocolo DH
        g_a = bytes([random.randint(0, 255) for _ in range(256)])
        g_a_hash = hashlib.sha256(g_a).digest()

        try:
            await client(RequestCallRequest(
                user_id=entity,
                random_id=random.randint(0, 0x7fffffff),
                g_a_hash=g_a_hash,
                protocol=PhoneCallProtocol(
                    udp_p2p=True,
                    udp_reflector=True,
                    min_layer=92,
                    max_layer=92,
                    library_versions=['1.0.0']
                ),
                video=False
            ))
            # Damos un pequeño respiro para que el servidor procese la solicitud
            await asyncio.sleep(2) 
            return {"status": "Mensaje enviado y señal de llamada lanzada"}
        except Exception as call_error:
            return {"status": "Mensaje enviado", "error_llamada": str(call_error)}
    
    except Exception as e:
        return {"error": f"Error en el proceso: {str(e)}"}
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