import os
import asyncio
import random
import time
import hashlib
import logging  # Nuevo: para logs
import sys
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.phone import RequestCallRequest
from telethon.tl.types import PhoneCallProtocol
from telethon.errors import FloodWaitError  # Nuevo: para manejo flood
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# --- CONFIGURACIÓN DE LOGGING ---
logging.basicConfig(
    level=logging.INFO,  # Muestra INFO y superiores
    format='[%(asctime)s] %(levelname)s: %(message)s',  # Formato: [timestamp] LEVEL: Mensaje
    handlers=[logging.StreamHandler(sys.stdout)]  # Output a stdout para Render
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

# Validaciones iniciales
if not all([API_ID, API_HASH, SESSION_STR, SUPABASE_URL, SUPABASE_KEY]):
    logger.critical("Faltan variables de entorno críticas. App no puede iniciar.")
    sys.exit(1)

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info("Cliente Supabase inicializado.")

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
logger.info("Hilo de loop asyncio iniciado.")

async def ensure_connection():
    """Asegura que el cliente esté conectado y autorizado."""
    logger.info("Iniciando conexión a Telegram...")
    if not client.is_connected():
        await client.connect()
        logger.info("Conexión establecida.")
    authorized = await client.is_user_authorized()
    if authorized:
        me = await client.get_me()
        logger.info(f"Sesión autorizada como @{me.username} (ID: {me.id}).")
    else:
        logger.warning("Sesión no autorizada.")
    return authorized

async def get_telegram_status_async():
    """Verifica el estado real obteniendo info del usuario."""
    try:
        if not client.is_connected():
            await client.connect()
            logger.info("Conexión a Telegram establecida para verificación de estado.")
        
        if not await client.is_user_authorized():
            logger.error("Sesión no autorizada en verificación de estado.")
            return {"status": "error", "message": "Sesión no autorizada"}
        
        me = await client.get_me()
        logger.info(f"Estado verificado: Conectado como @{me.username}.")
        return {
            "status": "connected", 
            "username": me.username, 
            "id": me.id, 
            "first_name": me.first_name
        }
    except Exception as e:
        logger.error(f"Error en verificación de estado de Telegram: {str(e)}")
        return {"status": "error", "message": str(e)}

async def realizar_llamada_y_mensaje(numeros_llamada, numeros_mensaje, texto_alerta):
    if not await ensure_connection():
        logger.error("No se pudo conectar a Telegram para enviar alertas.")
        return

    # 1. Enviar Mensajes
    for numero in numeros_mensaje:
        try:
            logger.info(f"Enviando mensaje a {numero}: '{texto_alerta}'")
            await client.send_message(numero, texto_alerta)
            logger.info(f"Mensaje enviado exitosamente a {numero}.")
        except FloodWaitError as e:
            logger.warning(f"Flood wait detectado para {numero}. Esperando {e.seconds} segundos.")
            await asyncio.sleep(e.seconds)
            # Retry una vez
            await client.send_message(numero, texto_alerta)
            logger.info(f"Mensaje reenviado exitosamente a {numero} después de flood wait.")
        except Exception as e:
            logger.error(f"Error enviando mensaje a {numero}: {str(e)}")

    # 2. Realizar Llamadas
    for numero in numeros_llamada:
        try:
            logger.info(f"Iniciando llamada a {numero}.")
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
            logger.info(f"Llamada iniciada exitosamente a {numero}.")
            await asyncio.sleep(2)  # Espera para evitar flood
        except FloodWaitError as e:
            logger.warning(f"Flood wait detectado para llamada a {numero}. Esperando {e.seconds} segundos.")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Error iniciando llamada a {numero}: {str(e)}")

# --- SCHEDULER (TAREA CADA 10 SEGUNDOS) ---
def tarea_revisar_alertas():
    now_ms = int(time.time() * 1000)
    logger.info(f"Ejecutando tarea de revisión de alertas (timestamp actual: {now_ms}).")
    try:
        # Paso 1: Buscar alertas vencidas
        res = supabase.table('alertas').select("*").eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
        alertas_vencidas = res.data

        if alertas_vencidas:
            logger.info(f"Procesando {len(alertas_vencidas)} alertas vencidas.")
            for alerta in alertas_vencidas:
                logger.info(f"Procesando alerta ID {alerta['id']} para user_id {alerta['user_id']} (mensaje: {alerta.get('mensaje_personalizado', 'default')}).")
                # Marcar como disparado
                supabase.table('alertas').update({'estado': 'disparado'}).eq('id', alerta['id']).execute()
                logger.info(f"Alerta ID {alerta['id']} marcada como 'disparado'.")
                
                # Preparar datos
                user_id = alerta['user_id']
                msg = alerta.get('mensaje_personalizado', "Alerta de seguridad activada.")
                
                # Obtener contactos
                res_c = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
                contactos = res_c.data or []
                logger.info(f"Encontrados {len(contactos)} contactos para user_id {user_id}.")
                
                if contactos:
                    nums_call = [c['telefono'] for c in contactos if c['es_primario']]
                    nums_msg = [c['telefono'] for c in contactos]
                    logger.info(f"Números para llamadas: {nums_call}. Números para mensajes: {nums_msg}.")
                    
                    # Ejecutar Telegram async
                    future = asyncio.run_coroutine_threadsafe(
                        realizar_llamada_y_mensaje(nums_call, nums_msg, msg), loop
                    )
                    future.result()  # Espera para loggear sync (opcional, pero asegura orden en logs)
                
                # Finalizar alerta
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta['id']).execute()
                logger.info(f"Alerta ID {alerta['id']} finalizada y marcada como 'inactivo'.")

    except Exception as e:
        logger.error(f"Error en tarea de revisión de alertas: {str(e)}")

# Iniciar Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10, max_instances=1)  # Fix: evita solapamientos
scheduler.start()
logger.info("Scheduler iniciado con intervalo de 10 segundos.")

# --- RUTAS FLASK ---

@app.route('/telegram_status', methods=['GET'])
def telegram_status():
    logger.info("Solicitud recibida para /telegram_status.")
    try:
        future = asyncio.run_coroutine_threadsafe(get_telegram_status_async(), loop)
        result = future.result(timeout=20)  # Aumentado timeout
        logger.info(f"Respuesta de estado: {result['status']}.")
        return jsonify(result), 200 if result['status'] == 'connected' else 500
    except Exception as e:
        logger.error(f"Error en /telegram_status: {str(e)}")
        return jsonify({"status": "error", "message": f"Timeout o error interno: {str(e)}"}), 500

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    data = request.json
    user_id = data.get('user_id')
    if not user_id:
        logger.warning("Solicitud a /ejecutar_emergencia sin user_id.")
        return jsonify({"error": "Falta User ID"}), 400

    logger.info(f"Solicitud para forzar emergencia para user_id {user_id}.")
    try:
        supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        logger.info(f"Alerta para user_id {user_id} marcada como 'disparado'.")
        tarea_revisar_alertas() 
        logger.info(f"Revisión manual ejecutada para user_id {user_id}.")
        return jsonify({"status": "Alerta disparada y procesada"}), 200
    except Exception as e:
        logger.error(f"Error en /ejecutar_emergencia para user_id {user_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    logger.info("Solicitud recibida para /ping.")
    return jsonify({"status": "online"}), 200

if __name__ == '__main__':
    # Al arrancar, intentamos conectar Telegram una vez
    future = asyncio.run_coroutine_threadsafe(ensure_connection(), loop)
    try:
        future.result()
        logger.info("Conexión inicial a Telegram exitosa.")
    except Exception as e:
        logger.error(f"Error en conexión inicial a Telegram: {str(e)}")
    
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Iniciando servidor Flask en puerto {port}.")
    app.run(host='0.0.0.0', port=port)