import os
import asyncio
import random
import time
import hashlib
import sys
import threading
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

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- CONFIGURACIÓN DE TELEGRAM ASYNC EN HILO SEPARADO ---
# Creamos el loop y lo asignamos al cliente para consistencia
loop = asyncio.new_event_loop()
client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH, loop=loop)

def start_background_loop(loop):
    """Inicia el loop asyncio en un hilo separado."""
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Iniciamos el hilo del loop inmediatamente
t = threading.Thread(target=start_background_loop, args=(loop,), daemon=True)
t.start()

async def ensure_connection():
    """Asegura que el cliente esté conectado y autorizado."""
    if not client.is_connected():
        try:
            await client.connect()
        except Exception as e:
            print(f"❌ Error conectando: {e}")
            return False
    return await client.is_user_authorized()

async def get_telegram_status_async():
    """Verifica el estado real obteniendo info del usuario."""
    try:
        if not client.is_connected():
            await client.connect()
        
        if not await client.is_user_authorized():
            return {"status": "error", "message": "Sesión no autorizada"}
        
        me = await client.get_me()
        return {
            "status": "connected", 
            "username": me.username, 
            "id": me.id, 
            "first_name": me.first_name
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def realizar_llamada_y_mensaje(numeros_llamada, numeros_mensaje, texto_alerta):
    if not await ensure_connection():
        print("❌ ERROR: No se pudo conectar a Telegram para enviar alertas.")
        return

    # 1. Enviar Mensajes
    for numero in numeros_mensaje:
        try:
            await client.send_message(numero, texto_alerta)
            print(f"✅ MSG enviado a: {numero}")
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
            print(f"📞 Llamada iniciada a: {numero}")
            await asyncio.sleep(2) 
        except Exception as e:
            print(f"❌ Error Call {numero}: {e}")

# --- SCHEDULER (TAREA CADA 10 SEGUNDOS) ---
def tarea_revisar_alertas():
    now_ms = int(time.time() * 1000)
    try:
        # Paso 1: Buscar alertas vencidas
        res = supabase.table('alertas').select("*").eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
        alertas_vencidas = res.data

        if alertas_vencidas:
            print(f"⏰ Procesando {len(alertas_vencidas)} alertas vencidas.")
            for alerta in alertas_vencidas:
                # Marcar como disparado INMEDIATAMENTE para evitar duplicidad
                supabase.table('alertas').update({'estado': 'disparado'}).eq('id', alerta['id']).execute()
                
                # Preparar datos
                user_id = alerta['user_id']
                msg = alerta.get('mensaje_personalizado', "Alerta de seguridad activada.")
                
                # Obtener contactos
                res_c = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
                contactos = res_c.data or []
                
                if contactos:
                    nums_call = [c['telefono'] for c in contactos if c['es_primario']]
                    nums_msg = [c['telefono'] for c in contactos]
                    
                    # Enviar tarea al loop de Telegram (Fire and Forget para no bloquear scheduler)
                    asyncio.run_coroutine_threadsafe(
                        realizar_llamada_y_mensaje(nums_call, nums_msg, msg), loop
                    )
                
                # Finalizar alerta en DB
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta['id']).execute()
                print(f"🏁 Alerta {alerta['id']} finalizada.")

    except Exception as e:
        print(f"⚠️ Error en tarea programada: {e}")

# Iniciar Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10)
scheduler.start()

# --- RUTAS FLASK ---

@app.route('/telegram_status', methods=['GET'])
def telegram_status():
    """Ruta para verificar conexión con Telegram."""
    try:
        future = asyncio.run_coroutine_threadsafe(get_telegram_status_async(), loop)
        result = future.result(timeout=10)
        return jsonify(result), 200 if result['status'] == 'connected' else 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"Timeout o error interno: {str(e)}"}), 500

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    data = request.json
    user_id = data.get('user_id')
    if not user_id: return jsonify({"error": "Falta User ID"}), 400

    try:
        # Forzar estado en DB
        supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        # Ejecutar revisión manual inmediata
        tarea_revisar_alertas() 
        return jsonify({"status": "Alerta disparada y procesada"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "online", "timestamp": datetime.now().isoformat()}), 200

if __name__ == '__main__':
    print("🚀 Iniciando servidor...")
    # CORRECCIÓN: Esperamos explícitamente a que Telegram conecte antes de abrir Flask
    try:
        connected = asyncio.run_coroutine_threadsafe(ensure_connection(), loop).result(timeout=20)
        if connected:
            print("✅ Telegram conectado correctamente.")
        else:
            print("⚠️ ADVERTENCIA: Telegram no pudo conectar, revisa logs y variables de entorno.")
    except Exception as e:
        print(f"❌ Error crítico al conectar al inicio: {e}")
    
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)