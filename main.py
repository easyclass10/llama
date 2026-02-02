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
    if not client.is_connected():
        await client.connect()

async def realizar_llamada_y_mensaje(numeros_llamada, numeros_mensaje, texto_alerta):
    """
    numeros_llamada: Lista de los 3 números principales.
    numeros_mensaje: Lista de los 5 números (incluye los 3 anteriores).
    """
    await iniciar_telegram()
    
    if not numeros_mensaje:
        print("DEBUG: No hay números para mensajes - saltando")
        sys.stdout.flush()
    else:
        # 1. ENVIAR MENSAJES (A los 5 contactos)
        for numero in numeros_mensaje:
            try:
                entity = await client.get_input_entity(numero)
                await client.send_message(entity, texto_alerta)
                print(f"Mensaje enviado a {numero}")
                sys.stdout.flush()
            except Exception as e:
                print(f"Error mensaje a {numero}: {e}")
                sys.stdout.flush()

    if not numeros_llamada:
        print("DEBUG: No hay números primarios para llamadas - saltando")
        sys.stdout.flush()
    else:
        # 2. REALIZAR LLAMADAS (A los 3 contactos principales)
        # Nota: Telegram puede bloquear si haces muchas llamadas simultáneas.
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
                print(f"Llamada iniciada a {numero}")
                sys.stdout.flush()
                await asyncio.sleep(5) # Espera entre llamadas para evitar ban
            except Exception as e:
                print(f"Error llamada a {numero}: {e}")
                sys.stdout.flush()

def tarea_revisar_alertas():
    """
    Esta función se ejecuta automáticamente cada X segundos.
    Busca en Supabase relojes vencidos.
    """
    now_ms = int(time.time() * 1000)
    print(f"DEBUG: Iniciando revisión de alertas programada... Tiempo actual server: {now_ms} ({datetime.fromtimestamp(now_ms/1000).strftime('%Y-%m-%d %H:%M:%S')})")
    sys.stdout.flush()
    try:
        # Buscar alertas activas cuyo tiempo haya pasado (tiempo actual > tiempo_fin)
        response = supabase.table('alertas').select("*").eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
        print(f"DEBUG: Alertas vencidas encontradas: {len(response.data)}")
        sys.stdout.flush()
        alertas_vencidas = response.data

        if alertas_vencidas:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            for alerta in alertas_vencidas:
                user_id = alerta['user_id']
                mensaje = alerta.get('mensaje_personalizado', "Estoy en peligro activa protocolo de busqueda.")
                
                # Obtener contactos de este usuario
                res_contactos = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
                contactos = res_contactos.data
                
                nums_llamada = [c['telefono'] for c in contactos if c['es_primario']]
                nums_mensaje = [c['telefono'] for c in contactos] # Todos reciben mensaje

                print(f"Ejecutando protocolo para usuario {user_id} con {len(nums_mensaje)} mensajes y {len(nums_llamada)} llamadas")
                sys.stdout.flush()
                
                # Ejecutar alertas en Telegram
                loop.run_until_complete(realizar_llamada_y_mensaje(nums_llamada, nums_mensaje, mensaje))
                
                # Marcar alerta como 'disparado' en DB para no repetirla
                supabase.table('alertas').update({'estado': 'disparado'}).eq('id', alerta['id']).execute()
            
            loop.close()
    except Exception as e:
        print(f"Error en tarea programada: {e}")
        sys.stdout.flush()

# --- SCHEDULER ---
# Ejecuta la revisión cada 30 segundos para mejor precisión en timers cortos
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", seconds=30)
scheduler.start()

# --- RUTAS FLASK ---

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    data = request.json
    user_id = data.get('user_id')
    print(f"DEBUG: Recibida señal de emergencia para usuario {user_id}") 
    sys.stdout.flush()
    
    if not user_id:
        print("DEBUG: Falta user_id - retornando 400")
        sys.stdout.flush()
        return jsonify({"error": "Falta User ID"}), 400

    try:
        # 1. Obtener datos de Supabase
        res_alerta = supabase.table('alertas').select("*").eq('user_id', user_id).eq('estado', 'activo').execute()
        if not res_alerta.data:
            print("DEBUG: No hay alerta activa para este user_id")
            sys.stdout.flush()
            return jsonify({"error": "No alerta activa"}), 404
        mensaje = res_alerta.data[0].get('mensaje_personalizado', "Ayuda, emergencia.")
        tiempo_fin = res_alerta.data[0]['tiempo_fin']
        print(f"DEBUG: Alerta encontrada - tiempo_fin: {tiempo_fin}, mensaje: {mensaje}. Alertas encontradas: {len(res_alerta.data)}")  
        sys.stdout.flush()

        res_contactos = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
        contactos = res_contactos.data
        print(f"DEBUG: Contactos encontrados: {len(contactos)}. Primarios: {sum(1 for c in contactos if c['es_primario'])}")  
        sys.stdout.flush()
        
        if not contactos:
            print("DEBUG: No hay contactos - no se ejecuta Telegram, pero protocolo 'exitoso'")
            sys.stdout.flush()
            # Aún actualiza DB para consistencia
            update_res = supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
            print(f"DEBUG: Resultado update (sin contactos): {update_res.data}")
            sys.stdout.flush()
            return jsonify({"status": "Protocolo ejecutado (sin contactos)"}), 200
        
        nums_llamada = [c['telefono'] for c in contactos if c['es_primario']]
        nums_mensaje = [c['telefono'] for c in contactos]

        # 2. Telegram
        print("DEBUG: Iniciando bucle de Telegram...") 
        sys.stdout.flush()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(realizar_llamada_y_mensaje(nums_llamada, nums_mensaje, mensaje))
        loop.close()
        print("DEBUG: Telegram completado.") 
        sys.stdout.flush()
        
        # 3. Actualizar DB
        print("DEBUG: Actualizando estado en Supabase...") 
        sys.stdout.flush()
        update_res = supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        print(f"DEBUG: Resultado update: {update_res.data}") 
        sys.stdout.flush()
        
        return jsonify({"status": "Protocolo ejecutado"}), 200

    except Exception as e:
        print(f"ERROR CRÍTICO: {str(e)}") 
        sys.stdout.flush()
        return jsonify({"error": str(e)}), 500

@app.route('/ping', methods=['GET'])
def ping():
    print("DEBUG: Ping recibido - scheduler alive")
    sys.stdout.flush()
    return jsonify({"status": "alive"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)