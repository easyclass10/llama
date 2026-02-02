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
        return {"status": "connected", "username": me.username, "id": me.id, "first_name": me.first_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- LÓGICA DE EMERGENCIA (ACTUALIZADA) ---
async def realizar_llamada_y_mensaje_async(user_id, contactos):
    """
    Envía 'Alerta activa protocolo' y llama a TODOS los contactos.
    """
    if not contactos:
        print(f"[{datetime.now()}] ⚠️ User {user_id} no tiene contactos.")
        return

    if not await ensure_connection():
        print(f"[{datetime.now()}] ❌ ERROR CRÍTICO: Telegram no conectado para user {user_id}")
        return

    print(f"[{datetime.now()}] 🚨 Iniciando emergencia para User ID: {user_id}. Contactos: {len(contactos)}")
    
    # Recorremos TODOS los contactos para mensajes y llamadas
    for contacto in contactos:
        numero = contacto['telefono']
        print(f"[{datetime.now()}] 📞 Procesando contacto {contacto.get('nombre', 'Desconocido')} ({numero})...")
        
        try:
            # 1. Enviar Mensaje Fijo
            await client.send_message(numero, "Alerta activa protocolo")
            print(f"[{datetime.now()}] ✅ MSG enviado a {numero}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error MSG {numero}: {e}")

        try:
            # 2. Realizar Llamada
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
            print(f"[{datetime.now()}] 📞 Llamada iniciada a {numero}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error Call {numero}: {e}")
        
        # Pequeña pausa para no saturar Telegram
        await asyncio.sleep(1)

# --- SCHEDULER (REVISIÓN CADA 10 SEGUNDOS) ---
def tarea_revisar_alertas():
    now_ms = int(time.time() * 1000)
    print(f"[{datetime.now()}] ⏱️ Iniciando ciclo de revisión de alertas...")

    try:
        # 1. Buscar alertas 'activo' vencidas (Tiempo agotado)
        print(f"[{datetime.now()}] 🔍 Buscando alertas ACTIVAS vencidas...")
        res_activo = supabase.table('alertas').select("*").eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
        alertas_vencidas = res_activo.data

        # 2. Buscar alertas 'disparado' (Forzadas manualmente o pendientes)
        print(f"[{datetime.now()}] 🔍 Buscando alertas en estado DISPARADO...")
        res_disparado = supabase.table('alertas').select("*").eq('estado', 'disparado').execute()
        alertas_disparadas = res_disparado.data

        # Unificar listas de procesamiento
        alertas_a_procesar = alertas_vencidas + alertas_disparadas

        if alertas_a_procesar:
            print(f"[{datetime.now()}] 🚨 Se encontraron {len(alertas_a_procesar)} alertas para procesar.")
            
            for alerta in alertas_a_procesar:
                alerta_id = alerta['id']
                user_id = alerta['user_id']
                estado_actual = alerta['estado']

                # Bloqueo lógico: Si ya está disparado, evitamos actualizar la BD otra vez si ya se procesó
                # Pero si está activo, la pasamos a disparado para que no se procesen duplicados en este mismo ciclo
                if estado_actual == 'activo':
                    supabase.table('alertas').update({'estado': 'disparado'}).eq('id', alerta_id).execute()
                    print(f"[{datetime.now()}] 🔄 Alerta {alerta_id} marcada como DISPARADO.")

                # Obtener contactos
                res_c = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
                contactos = res_c.data or []
                
                if contactos:
                    # Ejecutar Telegram Async
                    future = asyncio.run_coroutine_threadsafe(
                        realizar_llamada_y_mensaje_async(user_id, contactos), loop
                    )
                    # Esperamos a que termine (opcional, para asegurar estado final correcto)
                    try:
                        future.result(timeout=60) 
                    except Exception as e:
                        print(f"[{datetime.now()}] ⚠️ Timeout o error ejecutando Telegram: {e}")
                
                # Finalizar: Poner en INACTIVO
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta_id).execute()
                print(f"[{datetime.now()}] ✅ Alerta {alerta_id} procesada y finalizada (Estado: INACTIVO).")
        else:
            print(f"[{datetime.now()}] 😴 No hay alertas pendientes.")

    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ ERROR GENERAL EN CICLO: {e}")

# Iniciar Scheduler (cada 10 segundos)
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
    """
    Ruta ejecutada por el Frontend (Código 2005).
    1. Actualiza estado a 'disparado'.
    2. Ejecuta la lógica de emergencia inmediatamente.
    """
    data = request.json
    user_id = data.get('user_id')
    if not user_id: return jsonify({"error": "Falta User ID"}), 400

    print(f"[{datetime.now()}] 🚨 Solicitud MANUAL de emergencia para User: {user_id}")

    try:
        # Paso 1: Forzar estado en DB a 'disparado'
        supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        
        # Paso 2: Ejecutar la lógica de emergencia inmediatamente (sin esperar al scheduler)
        tarea_revisar_alertas()
        
        return jsonify({"status": "Emergencia disparada manualmente"}), 200
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error en ejecución manual: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "online"}), 200

if __name__ == '__main__':
    print("🚀 Iniciando servidor...")
    # Conectar al inicio
    asyncio.run_coroutine_threadsafe(ensure_connection(), loop)
    
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)