import os
import asyncio
import random
import time
import hashlib
import logging
import sys
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.phone import RequestCallRequest
from telethon.tl.types import PhoneCallProtocol
from telethon.errors import FloodWaitError
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# Configuración de logging para Render (stdout)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# --- CONFIGURACIÓN ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STR = os.environ.get("SESSION_STRING", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Validaciones
if not all([API_ID, API_HASH, SESSION_STR, SUPABASE_URL, SUPABASE_KEY]):
    logger.critical("Faltan variables de entorno. App no inicia.")
    sys.exit(1)

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info("Supabase inicializado.")

# --- TELEGRAM ASYNC ---
loop = asyncio.new_event_loop()
client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH, loop=loop)

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

t = threading.Thread(target=start_background_loop, args=(loop,), daemon=True)
t.start()
logger.info("Hilo asyncio iniciado.")

async def ensure_connection():
    logger.info("Conectando a Telegram...")
    if not client.is_connected():
        await client.connect()
        logger.info("Conexión establecida.")
    authorized = await client.is_user_authorized()
    if authorized:
        me = await client.get_me()
        logger.info(f"Autorizado como @{me.username} (ID: {me.id}).")
    else:
        logger.warning("No autorizado.")
    return authorized

async def get_telegram_status_async():
    try:
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            logger.error("Sesión no autorizada.")
            return {"status": "error", "message": "Sesión no autorizada"}
        me = await client.get_me()
        logger.info(f"Estado: Conectado como @{me.username}.")
        return {"status": "connected", "username": me.username, "id": me.id, "first_name": me.first_name}
    except Exception as e:
        logger.error(f"Error en estado: {str(e)}")
        return {"status": "error", "message": str(e)}

async def ejecutar_emergencia(user_id, alerta_id=None):
    if not await ensure_connection():
        logger.error("No conectado a Telegram para emergencia.")
        return

    # Obtener todos los contactos
    res_c = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
    contactos = res_c.data or []
    logger.info(f"Contactos encontrados para user_id {user_id}: {len(contactos)}.")

    if not contactos:
        logger.warning(f"No contactos para user_id {user_id}.")
        return

    nums_all = [c['telefono'] for c in contactos]
    texto_alerta = "Alerta activa protocolo"
    logger.info(f"Ejecutando emergencia para user_id {user_id}. Mensaje: '{texto_alerta}'. Números: {nums_all}.")

    # Enviar mensajes a TODOS
    for numero in nums_all:
        try:
            logger.info(f"Enviando mensaje a {numero}: '{texto_alerta}'.")
            await client.send_message(numero, texto_alerta)
            logger.info(f"Mensaje enviado a {numero}.")
        except FloodWaitError as e:
            logger.warning(f"Flood wait para mensaje {numero}. Esperando {e.seconds}s.")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Error mensaje {numero}: {str(e)}")

    # Llamadas a TODOS
    for numero in nums_all:
        try:
            logger.info(f"Iniciando llamada a {numero}.")
            entity = await client.get_input_entity(numero)
            g_a = bytes([random.randint(0, 255) for _ in range(256)])
            g_a_hash = hashlib.sha256(g_a).digest()
            await client(RequestCallRequest(
                user_id=entity,
                random_id=random.randint(0, 0x7fffffff),
                g_a_hash=g_a_hash,
                protocol=PhoneCallProtocol(udp_p2p=True, udp_reflector=True, min_layer=92, max_layer=92, library_versions=['1.0.0']),
                video=False
            ))
            logger.info(f"Llamada iniciada a {numero}.")
            await asyncio.sleep(2)
        except FloodWaitError as e:
            logger.warning(f"Flood wait para llamada {numero}. Esperando {e.seconds}s.")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Error llamada {numero}: {str(e)}")

# --- SCHEDULER CADA 10 SEGUNDOS ---
def tarea_revisar_alertas():
    now_ms = int(time.time() * 1000)
    logger.info(f"Revisión de alertas iniciada (now: {now_ms}).")
    try:
        # Seleccionar activo y expirado O disparado
        res = supabase.table('alertas').select("*").or_(f"and(estado.eq.activo,tiempo_fin.lt.{now_ms}),estado.eq.disparado").execute()
        alertas_pendientes = res.data

        if alertas_pendientes:
            logger.info(f"Procesando {len(alertas_pendientes)} alertas pendientes.")
            for alerta in alertas_pendientes:
                logger.info(f"Procesando alerta ID {alerta['id']} (user_id: {alerta['user_id']}, estado: {alerta['estado']}).")
                # Ejecutar emergencia
                future = asyncio.run_coroutine_threadsafe(ejecutar_emergencia(alerta['user_id'], alerta['id']), loop)
                future.result()  # Esperar para log ordenado
                # Set inactivo
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta['id']).execute()
                logger.info(f"Alerta ID {alerta['id']} finalizada como 'inactivo'.")
    except Exception as e:
        logger.error(f"Error en revisión: {str(e)}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10, max_instances=1)
scheduler.start()
logger.info("Scheduler iniciado.")

# --- RUTAS ---

@app.route('/telegram_status', methods=['GET'])
def telegram_status():
    logger.info("Solicitud /telegram_status.")
    try:
        future = asyncio.run_coroutine_threadsafe(get_telegram_status_async(), loop)
        result = future.result(timeout=20)
        logger.info(f"Estado: {result['status']}.")
        return jsonify(result), 200 if result['status'] == 'connected' else 500
    except Exception as e:
        logger.error(f"Error /telegram_status: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    data = request.json
    user_id = data.get('user_id')
    if not user_id:
        logger.warning("Solicitud sin user_id.")
        return jsonify({"error": "Falta User ID"}), 400
    logger.info(f"Forzando emergencia para user_id {user_id}.")
    try:
        # Set disparado (para que scheduler lo procese si no inmediato)
        supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        logger.info(f"Set 'disparado' para user_id {user_id}.")
        tarea_revisar_alertas()
        return jsonify({"status": "Emergencia procesada"}), 200
    except Exception as e:
        logger.error(f"Error force_trigger: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    logger.info("Solicitud /ping.")
    return jsonify({"status": "online"}), 200

if __name__ == '__main__':
    future = asyncio.run_coroutine_threadsafe(ensure_connection(), loop)
    try:
        future.result()
        logger.info("Conexión inicial OK.")
    except Exception as e:
        logger.error(f"Error inicial: {str(e)}")
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Iniciando en puerto {port}.")
    app.run(host='0.0.0.0', port=port)