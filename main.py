import os
import asyncio
import random
import time
import hashlib
import threading
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
        await client.connect()
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

async def realizar_emergencia(numeros_todos, texto_alerta):
    """Realiza llamadas y mensajes a TODOS los números proporcionados."""
    if not await ensure_connection():
        print("LOG RENDER: ❌ ERROR: No se pudo conectar a Telegram.")
        return

    print(f"LOG RENDER: 🚨 INICIANDO PROTOCOLO DE EMERGENCIA PARA {len(numeros_todos)} CONTACTOS")

    # 1. Enviar Mensajes a TODOS
    for numero in numeros_todos:
        try:
            await client.send_message(numero, texto_alerta)
            print(f"LOG RENDER: ✅ MSG enviado a: {numero}")
        except Exception as e:
            print(f"LOG RENDER: ❌ Error MSG {numero}: {e}")

    # 2. Realizar Llamadas a TODOS
    for numero in numeros_todos:
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
            print(f"LOG RENDER: 📞 Llamada iniciada a: {numero}")
            await asyncio.sleep(2) # Espera técnica para evitar bloqueo por flood
        except Exception as e:
            print(f"LOG RENDER: ❌ Error Call {numero}: {e}")

# --- SCHEDULER (TAREA CADA 10 SEGUNDOS) ---
def tarea_revisar_alertas():
    """Revisa la BD en busca de estados 'disparado' o tiempos agotados."""
    now_ms = int(time.time() * 1000)
    print(f"LOG RENDER: 🔍 Revisando alertas... (Time: {now_ms})")
    
    try:
        # Traemos todo lo que NO esté inactivo para filtrar en Python (más seguro para lógica compleja)
        res = supabase.table('alertas').select("*").neq('estado', 'inactivo').execute()
        alertas_activas = res.data

        if not alertas_activas:
            return

        for alerta in alertas_activas:
            trigger = False
            razon = ""

            # CASO 1: Estado ya es 'disparado'
            if alerta['estado'] == 'disparado':
                trigger = True
                razon = "Estado DISPARADO detectado en BD"

            # CASO 2: Estado es 'activo' pero se acabó el tiempo
            elif alerta['estado'] == 'activo' and alerta['tiempo_fin'] < now_ms:
                trigger = True
                razon = "Tiempo agotado"

            if trigger:
                print(f"LOG RENDER: ⚠️ EJECUTANDO EMERGENCIA. Razón: {razon}. Usuario: {alerta['user_id']}")
                
                # Actualizar inmediatamente a 'inactivo' para que no se ejecute dos veces en el siguiente ciclo
                # mientras se procesa este.
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta['id']).execute()
                
                # Obtener contactos
                user_id = alerta['user_id']
                # Mensaje fijo solicitado
                msg = "Alerta activa protocolo" 
                
                res_c = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
                contactos = res_c.data or []
                
                if contactos:
                    # REQUERIMIENTO: Llamar a TODOS y mandar mensaje a TODOS
                    nums_todos = [c['telefono'] for c in contactos]
                    
                    print(f"LOG RENDER: 📨 Enviando tarea a Telegram para {nums_todos}")
                    
                    # Ejecutar en el hilo de Telegram
                    asyncio.run_coroutine_threadsafe(
                        realizar_emergencia(nums_todos, msg), loop
                    )
                else:
                    print("LOG RENDER: ⚠️ Alerta disparada pero usuario no tiene contactos.")

    except Exception as e:
        print(f"LOG RENDER: ⚠️ Error en ciclo del scheduler: {e}")

# Iniciar Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10)
scheduler.start()

# --- RUTAS FLASK ---

@app.route('/telegram_status', methods=['GET'])
def telegram_status():
    try:
        future = asyncio.run_coroutine_threadsafe(get_telegram_status_async(), loop)
        result = future.result(timeout=10)
        return jsonify(result), 200 if result['status'] == 'connected' else 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    """Endpoint llamado por el frontend (código de pánico)"""
    data = request.json
    user_id = data.get('user_id')
    if not user_id: return jsonify({"error": "Falta User ID"}), 400

    print(f"LOG RENDER: ⚡ Petición manual de emergencia recibida para {user_id}")
    try:
        # Forzar estado a 'disparado' para que el scheduler (o ejecución inmediata) lo tome
        supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        
        # Ejecutar revisión manual inmediata para no esperar 10s
        tarea_revisar_alertas() 
        return jsonify({"status": "Alerta disparada"}), 200
    except Exception as e:
        print(f"LOG RENDER: ❌ Error en endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "online"}), 200

if __name__ == '__main__':
    asyncio.run_coroutine_threadsafe(ensure_connection(), loop)
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)