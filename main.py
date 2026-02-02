import os
import asyncio
import random
import time
import hashlib
import sys
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.phone import RequestCallRequest
from telethon.tl.types import PhoneCallProtocol
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
CORS(app)

# --- CONFIGURACIÓN ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STR = os.environ.get("SESSION_STRING", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)

# Loop global para manejar tareas async desde el scheduler sync
loop = asyncio.get_event_loop()

async def conectar_telegram():
    """Mantiene la conexión activa."""
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        print("❌ ERROR: Sesión de Telegram inválida.")
        return False
    return True

async def realizar_llamada_y_mensaje(numeros_llamada, numeros_mensaje, texto_alerta):
    await conectar_telegram()
    
    # 1. Enviar Mensajes
    for numero in numeros_mensaje:
        try:
            await client.send_message(numero, texto_alerta)
            print(f"✅ MSG enviado: {numero}")
        except Exception as e:
            print(f"❌ Error MSG {numero}: {e}")

    # 2. Realizar Llamadas
    for numero in numeros_llamada:
        try:
            entity = await client.get_input_entity(numero)
            g_a = bytes([random.randint(0, 255) for _ in range(256)])
            g_a_hash = hashlib.sha256(g_a).digest()

            await client(RequestCallRequest(
                user_id=entity,
                random_id=random.randint(0, 0x7fffffff),
                g_a_hash=g_a_hash,
                protocol=PhoneCallProtocol(
                    udp_p2p=True, udp_reflector=True, min_layer=92, max_layer=92, library_versions=['1.0.0']
                ),
                video=False
            ))
            print(f"📞 Llamada iniciada: {numero}")
            await asyncio.sleep(1) 
        except Exception as e:
            print(f"❌ Error Call {numero}: {e}")

def tarea_revisar_alertas():
    """Función envuelta para correr en el loop global desde el scheduler."""
    now_ms = int(time.time() * 1000)
    try:
        # Paso 1: Buscar alertas vencidas
        res = supabase.table('alertas').select("*").eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
        alertas_vencidas = res.data

        if alertas_vencidas:
            for alerta in alertas_vencidas:
                # Actualizar a disparado inmediatamente para evitar doble procesamiento
                supabase.table('alertas').update({'estado': 'disparado'}).eq('id', alerta['id']).execute()
                
                user_id = alerta['user_id']
                msg = alerta.get('mensaje_personalizado', "Alerta de seguridad activada.")
                
                # Obtener contactos
                res_c = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
                if res_c.data:
                    nums_call = [c['telefono'] for c in res_c.data if c['es_primario']]
                    nums_msg = [c['telefono'] for c in res_c.data]
                    
                    # Ejecutar la corrutina en el loop principal
                    asyncio.run_coroutine_threadsafe(
                        realizar_llamada_y_mensaje(nums_call, nums_msg, msg), loop
                    )
                
                # Finalizar alerta
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta['id']).execute()
                print(f"🏁 Alerta {alerta['id']} procesada.")

    except Exception as e:
        print(f"⚠️ Error en ciclo: {e}")

# --- SCHEDULER ---
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10)
scheduler.start()

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    data = request.json
    user_id = data.get('user_id')
    if not user_id: return jsonify({"error": "Falta User ID"}), 400

    # Forzamos el estado a disparado para que el scheduler lo pesque o lo ejecutamos directo
    supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
    # Ejecución inmediata para no esperar los 10s del scheduler
    tarea_revisar_alertas()
    return jsonify({"status": "Alerta disparada"}), 200

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "online"}), 200

if __name__ == '__main__':
    # Inicializar Telegram antes de arrancar Flask
    loop.run_until_complete(conectar_telegram())
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)