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

# --- TELEGRAM SETUP ---
loop = asyncio.new_event_loop()
client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH, loop=loop)

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

t = threading.Thread(target=start_background_loop, args=(loop,), daemon=True)
t.start()

# --- FUNCIONES DE LOGGING ---
def log(msg):
    """Log helper que fuerza la salida en Render"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# --- TELEGRAM FUNCIONES ---
async def ensure_connection():
    if not client.is_connected():
        await client.connect()
    return await client.is_user_authorized()

async def get_telegram_status_async():
    try:
        if not client.is_connected(): await client.connect()
        if not await client.is_user_authorized(): return {"status": "error", "message": "No autorizado"}
        me = await client.get_me()
        return {"status": "connected", "username": me.username, "first_name": me.first_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def ejecutar_protocolo_emergencia(numeros_todos, texto_alerta):
    """
    Realiza llamadas y envía mensajes a TODOS los números proporcionados.
    """
    if not await ensure_connection():
        log("❌ ERROR CRÍTICO: Telegram no conectado.")
        return

    log(f"🚨 INICIANDO PROTOCOLO DE EMERGENCIA para {len(numeros_todos)} contactos.")

    # 1. Enviar Mensajes a TODOS
    for numero in numeros_todos:
        try:
            await client.send_message(numero, texto_alerta)
            log(f"✅ MENSAJE enviado a: {numero}")
        except Exception as e:
            log(f"❌ Error enviando mensaje a {numero}: {e}")

    # 2. Realizar Llamadas a TODOS (Como solicitaste)
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
            log(f"📞 LLAMADA iniciada a: {numero}")
            await asyncio.sleep(3) # Pausa para evitar bloqueo de Telegram
        except Exception as e:
            log(f"❌ Error llamando a {numero}: {e}")

# --- SCHEDULER: CEREBRO DEL SISTEMA ---
def tarea_revisar_alertas():
    now_ms = int(time.time() * 1000)
    
    try:
        # CONSULTA 1: Alertas disparadas manualmente (Boton Panico o Codigo 2005)
        res_disparadas = supabase.table('alertas').select("*").eq('estado', 'disparado').execute()
        
        # CONSULTA 2: Alertas activas cuyo tiempo ya expiró
        res_tiempo = supabase.table('alertas').select("*").eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
        
        # Combinar listas (evitando duplicados por ID si fuera necesario)
        alertas_a_procesar = res_disparadas.data + res_tiempo.data
        
        if not alertas_a_procesar:
            # log("💤 Sistema vigilando... sin alertas activas.") # Descomentar si quieres logs constantes
            return

        log(f"🔥 PROCESANDO {len(alertas_a_procesar)} ALERTAS DE EMERGENCIA.")

        for alerta in alertas_a_procesar:
            user_id = alerta['user_id']
            log(f"▶️ Procesando alerta ID: {alerta['id']} (Usuario: {user_id})")

            # 1. Obtener Contactos
            res_c = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
            contactos = res_c.data or []

            if contactos:
                # LISTA UNIFICADA: Llamamos y escribimos a TODOS los contactos encontrados
                lista_numeros = [c['telefono'] for c in contactos]
                
                mensaje_final = "Alerta activa protocolo" # TEXTO FIJO SOLICITADO
                
                # Ejecutar Telegram en el hilo Async
                asyncio.run_coroutine_threadsafe(
                    ejecutar_protocolo_emergencia(lista_numeros, mensaje_final), loop
                )
            else:
                log(f"⚠️ Alerta disparada pero el usuario {user_id} NO tiene contactos.")

            # 2. Cerrar alerta (Marcar como inactivo)
            supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta['id']).execute()
            log(f"🏁 Alerta {alerta['id']} finalizada y marcada como INACTIVO.")

    except Exception as e:
        log(f"⚠️ Error en el Loop de revisión: {e}")

# Iniciar Scheduler (Cada 10 segundos)
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10)
scheduler.start()
log("✅ Scheduler de seguridad iniciado.")

# --- RUTAS ---
@app.route('/telegram_status', methods=['GET'])
def telegram_status():
    future = asyncio.run_coroutine_threadsafe(get_telegram_status_async(), loop)
    try:
        result = future.result(timeout=10)
        return jsonify(result), 200 if result['status'] == 'connected' else 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    """Ruta llamada por código de pánico o botón de emergencia"""
    data = request.json
    user_id = data.get('user_id')
    if not user_id: return jsonify({"error": "Falta User ID"}), 400

    try:
        log(f"🚨 Recibida petición de emergencia forzada para User {user_id}")
        # Forzar estado a 'disparado' para que el scheduler lo recoja inmediatamente
        supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        
        # Ejecutar revisión manual ya mismo para no esperar 10s
        tarea_revisar_alertas() 
        return jsonify({"status": "Alerta iniciada"}), 200
    except Exception as e:
        log(f"❌ Error en endpoint emergencia: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/')
def index():
    return "Servidor de Seguridad Activo", 200

if __name__ == '__main__':
    asyncio.run_coroutine_threadsafe(ensure_connection(), loop)
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)