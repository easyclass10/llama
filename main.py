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
        
        # Verificamos si estamos autorizados (si la sesión sirve)
        if not await client.is_user_authorized():
            print("❌ ERROR: [TELEGRAM] La sesión no es válida o expiró. Recrea el SESSION_STRING.")
            return False
        
        me = await client.get_me()
        print(f"✅ CONEXIÓN EXITOSA: [TELEGRAM] Conectado como: {me.first_name} (@{me.username})")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"❌ ERROR CRÍTICO: [TELEGRAM] Fallo al conectar: {e}")
        sys.stdout.flush()
        return False

async def realizar_llamada_y_mensaje(numeros_llamada, numeros_mensaje, texto_alerta):
    print(f"DEBUG: [TELEGRAM] Iniciando protocolo. Mensajes a enviar: {len(numeros_mensaje)}, Llamadas: {len(numeros_llamada)}")
    sys.stdout.flush()
    
    try:
        await iniciar_telegram()
        print("DEBUG: [TELEGRAM] Cliente conectado exitosamente.")
        sys.stdout.flush()
        
        # 1. ENVIAR MENSAJES
        for numero in numeros_mensaje:
            try:
                print(f"DEBUG: [MSG] Intentando enviar a {numero}...")
                sys.stdout.flush()
                # Usamos el número directamente, Telethon lo resuelve si está en contactos o es formato internacional
                await client.send_message(numero, texto_alerta)
                print(f"✅ DEBUG: [MSG] Mensaje ENVIADO a {numero}")
            except Exception as e:
                print(f"❌ DEBUG: [MSG] ERROR enviando a {numero}: {e}")
            sys.stdout.flush()

        # 2. REALIZAR LLAMADAS
        for numero in numeros_llamada:
            try:
                print(f"DEBUG: [CALL] Intentando llamar a {numero}...")
                sys.stdout.flush()
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
                print(f"📞 ✅ DEBUG: [CALL] Llamada INICIADA a {numero}")
                sys.stdout.flush()
                await asyncio.sleep(2) 
            except Exception as e:
                print(f"❌ DEBUG: [CALL] ERROR llamando a {numero}: {e}")
            sys.stdout.flush()

    except Exception as e:
        print(f"🔥 DEBUG: [TELEGRAM] ERROR CRÍTICO EN PROTOCOLO: {e}")
        sys.stdout.flush()

def tarea_revisar_alertas():
    """
    Máquina de estados:
    1. Activo + Vencido -> Disparado
    2. Disparado -> Ejecutar Acciones -> Inactivo
    """
    now_ms = int(time.time() * 1000)
    print(f"DEBUG: Ciclo de revisión... {datetime.fromtimestamp(now_ms/1000).strftime('%H:%M:%S')}")
    sys.stdout.flush()

    try:
        # --- PASO 1: DETECTAR VENCIDOS (Activo -> Disparado) ---
        # Buscamos alertas activas cuyo tiempo haya pasado y las marcamos como disparado
        data_update = supabase.table('alertas') \
            .update({'estado': 'disparado'}) \
            .eq('estado', 'activo') \
            .lt('tiempo_fin', now_ms) \
            .execute()
        
        if data_update.data:
            print(f"DEBUG: Se dispararon {len(data_update.data)} alertas por tiempo vencido.")
            sys.stdout.flush()

        # --- PASO 2: PROCESAR DISPARADOS (Disparado -> Inactivo) ---
        # Buscamos todo lo que esté en 'disparado' (ya sea por tiempo vencido arriba o botón de pánico)
        response = supabase.table('alertas').select("*").eq('estado', 'disparado').execute()
        alertas_a_procesar = response.data

        if alertas_a_procesar:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            for alerta in alertas_a_procesar:
                user_id = alerta['user_id']
                mensaje = alerta.get('mensaje_personalizado', "Alerta de seguridad activada.")
                print(f"PROCESANDO ALERTA DISPARADA: {user_id}")
                sys.stdout.flush()
                
                # Obtener contactos
                res_contactos = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
                contactos = res_contactos.data
                
                if not contactos:
                    print(f"Usuario {user_id} no tiene contactos. Cerrando alerta.")
                else:
                    nums_llamada = [c['telefono'] for c in contactos if c['es_primario']]
                    nums_mensaje = [c['telefono'] for c in contactos]

                    # Ejecutar Telegram
                    loop.run_until_complete(realizar_llamada_y_mensaje(nums_llamada, nums_mensaje, mensaje))
                
                # --- PASO FINAL: CERRAR ALERTA (Disparado -> Inactivo) ---
                # Esto asegura que no se vuelva a ejecutar en el siguiente ciclo
                supabase.table('alertas').update({'estado': 'inactivo'}).eq('id', alerta['id']).execute()
                print(f"Alerta {alerta['id']} finalizada y pasada a inactivo.")
                sys.stdout.flush()
            
            loop.close()

    except Exception as e:
        print(f"Error en tarea programada: {e}")
        sys.stdout.flush()

    if alertas_vencidas:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Validamos conexión antes de proceder
        if loop.run_until_complete(iniciar_telegram()):
            for alerta in alertas_vencidas:
                # ... (ejecutar protocolo)
        else:
            print("⚠️ DEBUG: Saltando ejecución de Telegram por falta de conexión.")
        
        loop.close()

# --- SCHEDULER ---
scheduler = BackgroundScheduler()
# Reduje el intervalo a 10 segundos para que reaccione más rápido al cambio de estado
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=10)
scheduler.start()

# --- RUTAS FLASK ---

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    """
    Ruta para Botón de Pánico o Código Falso.
    Cambia estado a 'disparado' inmediatamente y deja que el Scheduler lo procese (o lo procesa aquí).
    Para respuesta inmediata, lo procesamos aquí y cerramos a inactivo.
    """
    data = request.json
    user_id = data.get('user_id')
    
    if not user_id:
        return jsonify({"error": "Falta User ID"}), 400

    print(f"DEBUG: Emergencia manual recibida para {user_id}")
    sys.stdout.flush()

    try:
        # 1. Obtener la alerta activa (si existe) para sacar el mensaje personalizado
        res_alerta = supabase.table('alertas').select("*").eq('user_id', user_id).execute()
        
        mensaje = "AYUDA: Emergencia de seguridad activada."
        if res_alerta.data:
            # Si había un mensaje configurado, lo usamos
            alerta_data = res_alerta.data[0]
            if alerta_data.get('mensaje_personalizado'):
                mensaje = alerta_data.get('mensaje_personalizado')
            
            # Actualizamos el estado a disparado inmediatamente
            supabase.table('alertas').update({'estado': 'disparado'}).eq('id', alerta_data['id']).execute()

        # 2. Obtener contactos
        res_contactos = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
        contactos = res_contactos.data
        
        if not contactos:
            # Si no hay contactos, simplemente cerramos la alerta
            supabase.table('alertas').update({'estado': 'inactivo'}).eq('user_id', user_id).execute()
            return jsonify({"status": "Alerta registrada (sin contactos)"}), 200
        
        nums_llamada = [c['telefono'] for c in contactos if c['es_primario']]
        nums_mensaje = [c['telefono'] for c in contactos]

        # 3. Ejecutar Telegram
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(realizar_llamada_y_mensaje(nums_llamada, nums_mensaje, mensaje))
        loop.close()
        
        # 4. FINALIZAR (Pasar a inactivo)
        supabase.table('alertas').update({'estado': 'inactivo'}).eq('user_id', user_id).execute()
        
        return jsonify({"status": "Protocolo ejecutado y cerrado"}), 200

    except Exception as e:
        print(f"ERROR CRÍTICO MANUAL: {str(e)}") 
        sys.stdout.flush()
        return jsonify({"error": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "alive"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)