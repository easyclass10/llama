import os
import asyncio
import random
import time
import hashlib
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
    
    # 1. ENVIAR MENSAJES (A los 5 contactos)
    for numero in numeros_mensaje:
        try:
            entity = await client.get_input_entity(numero)
            await client.send_message(entity, texto_alerta)
            print(f"Mensaje enviado a {numero}")
        except Exception as e:
            print(f"Error mensaje a {numero}: {e}")

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
            await asyncio.sleep(5) # Espera entre llamadas para evitar ban
        except Exception as e:
            print(f"Error llamada a {numero}: {e}")

def tarea_revisar_alertas():
    """
    Esta función se ejecuta automáticamente cada X minutos.
    Busca en Supabase relojes vencidos.
    """
    print("Revisando alertas vencidas...")
    try:
        # Buscar alertas activas cuyo tiempo haya pasado (tiempo actual > tiempo_fin)
        now_ms = int(time.time() * 1000)
        
        response = supabase.table('alertas').select("*").eq('estado', 'activo').lt('tiempo_fin', now_ms).execute()
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

                print(f"Ejecutando protocolo para usuario {user_id}")
                
                # Ejecutar alertas en Telegram
                loop.run_until_complete(realizar_llamada_y_mensaje(nums_llamada, nums_mensaje, mensaje))
                
                # Marcar alerta como 'disparado' en DB para no repetirla
                supabase.table('alertas').update({'estado': 'disparado'}).eq('id', alerta['id']).execute()
            
            loop.close()
    except Exception as e:
        print(f"Error en tarea programada: {e}")

# --- SCHEDULER ---
# Ejecuta la revisión cada 1 minuto (5 minutos es mucho tiempo para una emergencia)
scheduler = BackgroundScheduler()
scheduler.add_job(func=tarea_revisar_alertas, trigger="interval", minutes=1)
scheduler.start()

# --- RUTAS FLASK ---

@app.route('/ejecutar_emergencia', methods=['POST'])
def force_trigger():
    data = request.json
    user_id = data.get('user_id')
    print(f"DEBUG: Recibida señal de emergencia para usuario {user_id}") # Log nuevo
    
    if not user_id:
        return jsonify({"error": "Falta User ID"}), 400

    try:
        # 1. Obtener datos de Supabase
        res_alerta = supabase.table('alertas').select("mensaje_personalizado").eq('user_id', user_id).execute()
        mensaje = "Ayuda, emergencia."
        if res_alerta.data:
            mensaje = res_alerta.data[0].get('mensaje_personalizado', mensaje)
        print(f"DEBUG: Mensaje recuperado: {mensaje}") # Log nuevo

        res_contactos = supabase.table('contactos').select("*").eq('user_id', user_id).execute()
        contactos = res_contactos.data
        print(f"DEBUG: Contactos encontrados: {len(contactos)}") # Log nuevo
        
        nums_llamada = [c['telefono'] for c in contactos if c['es_primario']]
        nums_mensaje = [c['telefono'] for c in contactos]

        # 2. Telegram
        print("DEBUG: Iniciando bucle de Telegram...") # Log nuevo
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(realizar_llamada_y_mensaje(nums_llamada, nums_mensaje, mensaje))
        loop.close()
        print("DEBUG: Telegram completado.") # Log nuevo
        
        # 3. Actualizar DB (ESTE ES EL PASO QUE TE FALTA VER)
        print("DEBUG: Actualizando estado en Supabase...") # Log nuevo
        update_res = supabase.table('alertas').update({'estado': 'disparado'}).eq('user_id', user_id).execute()
        print(f"DEBUG: Resultado update: {update_res.data}") # Log nuevo
        
        return jsonify({"status": "Protocolo ejecutado"}), 200

    except Exception as e:
        print(f"ERROR CRÍTICO: {str(e)}") # Esto te dirá exactamente qué falló
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)