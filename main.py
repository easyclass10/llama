import os
import asyncio
import random
import time
import hashlib
import logging
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.phone import RequestCallRequest
from telethon.tl.types import PhoneCallProtocol
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# Configuración de Logs para Render
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# --- CONFIGURACIÓN ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STR = os.environ.get("SESSION_STRING", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not API_ID or not API_HASH:
    logger.error("Faltan API_ID o API_HASH en las variables de entorno.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)

# --- GESTIÓN DE ASYNCIO (CORRECCIÓN CRÍTICA) ---
# Creamos un bucle nuevo para que corra en un hilo separado y nunca se cierre
loop = asyncio.new_event_loop()

def start_async_loop():
    """Mantiene el bucle de eventos corriendo en segundo plano."""
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()

# Iniciar el hilo del bucle
t = threading.Thread(target=start_async_loop, daemon=True)
t.start()

async def conectar_telegram_async():
    """Conecta y reconecta si es necesario."""
    if not client.is_connected():
        logger.info("Conectando a Telegram...")
        try:
            await client.connect()
        except Exception as e:
            logger.error(f"Error crítico conectando a Telegram: {e}")
            return False
            
    if not await client.is_user_authorized():
        logger.error("Sesión de Telegram inválida. Verifica SESSION_STRING.")
        return False
    return True

async def realizar_llamada_y_mensaje(numeros_llamada, numeros_mensaje, texto_alerta):
    await conectar_telegram_async()
    
    # 1. Enviar Mensajes
    for numero in numeros_mensaje:
        try:
            await client.send_message(numero, texto_alerta)
            logger.info(f"✅ MSG enviado a: {numero}")
        except Exception as e:
            logger.error(f"❌ Error MSG {numero}: {e}")

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
            logger.info(f"📞 Llamada iniciada: {numero}")
            await asyncio.sleep(2) # Pequeña pausa para evitar spam excesivo
        except errors.FloodWaitError as e:
            logger.warning(f"FloodWait en {numero}: esperar {e.seconds}s")
        except Exception as e:
            logger.error(f"❌ Error Call {numero}: {e}")

def ejecutar_alerta_async(user_id, msg):
    """Wrapper para llamar la función async desde el scheduler."""
    try:
        # Obtener contactos de forma sincrona (Supabase py es sync)
        res_c = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
        
        if res_c.data:
            nums_call = [c['telefono'] for c in res_c.data if c['es_primario']]
            nums_msg = [c['telefono'] for c in res_c.data]
            
            # Enviar tarea al loop que corre en el thread daemon
            future = asyncio.run_coroutine_threadsafe(
                realizar_llamada_y_mensaje(nums_call, nums_msg, msg), loop
            )
            future.result(timeout=60) # Esperar a que termine (con timeout)
    except Exception as e:
        logger.error(f"Error ejecutando alerta para user {user_id}: {e}")

def tarea_revisar_alertas():
    """Función del scheduler (Corre en thread separado)."""
    now_ms = int(time.time() * 1000)
    try:
        # Paso 1: Buscar alertas vencidas (activas y cuyo tiempo fin pasó)
        res = supabase.table('alertas').select("*").eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
        alertas_vencidas = res.data

        if alertas_vencidas:
            for alerta in alertas_vencidas:
                alerta_id = alerta['id']
                user_id = alerta['user_id']
                msg = alerta.get('mensaje_personalizado', "Alerta de seguridad activada.")
                
                logger.info(f"🚨 Procesando alerta ID {alerta_id} para user {user_id}")

                # Paso 2: Marcar como 'disparado' para evitar doble procesamiento inmediato
                # Nota: No marcamos como 'inactivo' aún por si falla el envío, queremos que se reintente o quede registrado.
                supabase.table('alertas').update({'estado': 'disparado'}).eq('id', alerta_id).execute()
                
                # Paso 3: Ejecutar lógica de Telegram
                ejecutar_alerta_async(user_id, msg)
                
                # Paso 4: Finalizar alerta
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta_id).execute()
                logger.info(f"🏁 Alerta {alerta_id} finalizada.")

    except Exception as e:
        logger.error(f"⚠️ Error en ciclo de alertas: {e}")

# --- SCHEDULER ---
# Revisamos cada 10 segundos
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10)
scheduler.start()

# --- RUTAS FLASK ---

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    data = request.json
    user_id = data.get('user_id')
    if not user_id: 
        return jsonify({"error": "Falta User ID"}), 400

    logger.info(f"Solicitud manual de emergencia para user {user_id}")
    # Ejecución directa
    ejecutar_alerta_async(user_id, "ALERTA MANUAL ACTIVADA")
    return jsonify({"status": "Comando de emergencia enviado"}), 200

@app.route('/check_telegram', methods=['GET'])
def check_telegram_status():
    """
    Nueva ruta solicitada para verificar el estado de la conexión.
    Útil para monitoreo de salud (Healthcheck).
    """
    # Verificamos el estado de manera asíncrona dentro del loop
    try:
        connected = asyncio.run_coroutine_threadsafe(client.is_connected_healthy(), loop).result(timeout=2)
        authorized = asyncio.run_coroutine_threadsafe(client.is_user_authorized(), loop).result(timeout=2)
        
        status = {
            "telegram_status": "ok" if (connected and authorized) else "error",
            "is_connected": connected,
            "is_authorized": authorized
        }
        return jsonify(status), 200
    except Exception as e:
        logger.error(f"Error checking status: {e}")
        return jsonify({"telegram_status": "error", "detail": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "online", "timestamp": datetime.now().isoformat()}), 200

if __name__ == '__main__':
    # Forzar la conexión inicial antes de recibir requests HTTP
    logger.info("Iniciando servicio y conectando cliente Telegram...")
    try:
        # Conectamos usando el loop
        asyncio.run_coroutine_threadsafe(conectar_telegram_async(), loop).result(timeout=15)
    except Exception as e:
        logger.error(f"No se pudo conectar al iniciar: {e}")
    
    port = int(os.environ.get("PORT", 8000))
    # Nota: threaded=True es necesario para Flask, pero el loop de asyncio corre en su propio thread daemon
    app.run(host='0.0.0.0', port=port, threaded=True)