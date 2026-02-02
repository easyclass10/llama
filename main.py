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

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Cliente Telegram Global
client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)

async def iniciar_telegram():
    """Inicia el cliente y verifica que la sesión sea válida."""
    try:
        if not client.is_connected():
            print("DEBUG: [TELEGRAM] Conectando al servidor...")
            await client.connect()
        
        if not await client.is_user_authorized():
            print("❌ ERROR: [TELEGRAM] La sesión no es válida o expiró.")
            sys.stdout.flush()
            return False
        
        me = await client.get_me()
        print(f"✅ CONEXIÓN EXITOSA: [TELEGRAM] Conectado como: {me.first_name}")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"❌ ERROR CRÍTICO: [TELEGRAM] Fallo al conectar: {e}")
        sys.stdout.flush()
        return False

async def realizar_llamada_y_mensaje(numeros_llamada, numeros_mensaje, texto_alerta):
    print(f"DEBUG: [TELEGRAM] Iniciando protocolo. Mensajes: {len(numeros_mensaje)}, Llamadas: {len(numeros_llamada)}")
    sys.stdout.flush()
    
    try:
        connected = await iniciar_telegram()
        if not connected:
            return

        # 1. ENVIAR MENSAJES
        for numero in numeros_mensaje:
            try:
                print(f"DEBUG: [MSG] Enviando a {numero}...")
                await client.send_message(numero, texto_alerta)
                print(f"✅ DEBUG: [MSG] ENVIADO a {numero}")
            except Exception as e:
                print(f"❌ DEBUG: [MSG] ERROR en {numero}: {e}")
            sys.stdout.flush()

        # 2. REALIZAR LLAMADAS
        for numero in numeros_llamada:
            try:
                print(f"DEBUG: [CALL] Llamando a {numero}...")
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
                print(f"📞 ✅ DEBUG: [CALL] INICIADA a {numero}")
                await asyncio.sleep(2) 
            except Exception as e:
                print(f"❌ DEBUG: [CALL] ERROR en {numero}: {e}")
            sys.stdout.flush()

    except Exception as e:
        print(f"🔥 DEBUG: [TELEGRAM] ERROR GENERAL: {e}")
        sys.stdout.flush()

def tarea_revisar_alertas():
    now_ms = int(time.time() * 1000)
    print(f"DEBUG: Ciclo de revisión... {datetime.fromtimestamp(now_ms/1000).strftime('%H:%M:%S')}")
    sys.stdout.flush()

    try:
        # PASO 1: Activo -> Disparado
        data_update = supabase.table('alertas').update({'estado': 'disparado'}).eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
        
        if data_update.data:
            print(f"DEBUG: Se dispararon {len(data_update.data)} alertas por tiempo.")

        # PASO 2: Procesar Disparados
        response = supabase.table('alertas').select("*").eq('estado', 'disparado').execute()
        alertas_a_procesar = response.data

        if alertas_a_procesar:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            for alerta in alertas_a_procesar:
                user_id = alerta['user_id']
                mensaje = alerta.get('mensaje_personalizado', "Alerta de seguridad activada.")
                
                res_contactos = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
                contactos = res_contactos.data
                
                if contactos:
                    nums_llamada = [c['telefono'] for c in contactos if c['es_primario']]
                    nums_mensaje = [c['telefono'] for c in contactos]
                    loop.run_until_complete(realizar_llamada_y_mensaje(nums_llamada, nums_mensaje, mensaje))
                
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta['id']).execute()
                print(f"🏁 Alerta {alerta['id']} finalizada.")
                sys.stdout.flush()
            
            loop.close()

    except Exception as e:
        print(f"Error en tarea programada: {e}")
        sys.stdout.flush()

# --- SCHEDULER ---
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10)
scheduler.start()

# --- RUTAS FLASK ---
@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    data = request.json
    user_id = data.get('user_id')
    if not user_id: return jsonify({"error": "Falta User ID"}), 400

    try:
        supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        # El scheduler la procesará en el siguiente ciclo (máximo 10 seg)
        return jsonify({"status": "Alerta disparada en sistema"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "alive"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)