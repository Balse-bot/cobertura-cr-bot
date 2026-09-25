import os
import io
import re
import json
import logging
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from math import radians, sin, cos, sqrt, atan2
import requests
import telebot
from telebot import types

TOKEN = "8621312939:AAHQjsKsDUkedKEKzD1HmJyJ0q4S7Qu2NnA"
DB_POSTES = "posteria_optimizada.db"

# 🔒 SEGURIDAD: Reemplaza este número por tu ID de Telegram.
# Escríbele /id al bot para saber cuál es el tuyo.
ADMINS = [1402264487]  

# Calibración exacta
UMBRAL_COBERTURA_DIRECTA = 70
UMBRAL_AL_BORDE = 500
LIMITE_COBERTURA = 400

FACTOR_MAX_RUTA_VS_LINEAL = 4
CANDIDATOS_A_EVALUAR = 10
METROS_POR_GRADO = 111320

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cobertura_bot")

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)


# ==========================================
# 1. INICIALIZACIÓN DE BASE DE DATOS Y CACHÉ
# ==========================================

def inicializar_bd():
    """Crea las tablas de postes y de caché si no existen."""
    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS postes (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, lat REAL, lon REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_coords ON postes(lat, lon)")
    
    # Tablas de caché para prevenir bloqueos de IP
    c.execute("CREATE TABLE IF NOT EXISTS cache_nominatim (coords_key TEXT PRIMARY KEY, json_data TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS cache_osrm (ruta_key TEXT PRIMARY KEY, distancia REAL)")
    conn.commit()
    conn.close()

def forzar_reconstruccion_bd(zip_path):
    """Borra la red vieja y carga el nuevo ZIP/KMZ directamente."""
    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("DELETE FROM postes") # Borrar red anterior
    
    total_insertados = 0
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            kml_file = _extraer_kml(z)
            if kml_file:
                with kml_file:
                    context = ET.iterparse(kml_file, events=('end',))
                    batch = []
                    for event, elem in context:
                        tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                        if tag == 'Placemark':
                            coords = elem.find('.//{*}coordinates')
                            if coords is not None and coords.text:
                                partes = coords.text.strip().split()
                                if partes:
                                    try:
                                        lon, lat, *_ = partes[0].split(',')
                                        name_tag = elem.find('{*}name')
                                        cod = name_tag.text.strip() if (name_tag is not None and name_tag.text) else "S/C"
                                        batch.append((cod, float(lat), float(lon)))
                                        if len(batch) >= 5000:
                                            c.executemany("INSERT INTO postes (codigo, lat, lon) VALUES (?, ?, ?)", batch)
                                            conn.commit()
                                            total_insertados += len(batch)
                                            batch = []
                                    except (ValueError, IndexError):
                                        pass
                            elem.clear()
                    if batch:
                        c.executemany("INSERT INTO postes (codigo, lat, lon) VALUES (?, ?, ?)", batch)
                        conn.commit()
                        total_insertados += len(batch)
    except Exception as e:
        logger.exception("Error reconstruyendo BD: %s", e)
    finally:
        conn.close()
    return total_insertados

def _extraer_kml(z):
    kml_candidatos = [n for n in z.namelist() if n.lower().endswith('.kml')]
    if kml_candidatos: return z.open(kml_candidatos[0])
    kmz_candidatos = [n for n in z.namelist() if n.lower().endswith('.kmz')]
    if kmz_candidatos:
        kmz_bytes = z.read(kmz_candidatos[0])
        kmz_zip = zipfile.ZipFile(io.BytesIO(kmz_bytes))
        kml_internos = [n for n in kmz_zip.namelist() if n.lower().endswith('.kml')]
        if kml_internos: return kmz_zip.open(kml_internos[0])
    return None

def inicializar_desde_zip():
    inicializar_bd()
    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM postes")
    count = c.fetchone()[0]
    conn.close()
    
    if count == 0:
        archivos = [f for f in os.listdir('.') if f.endswith('.zip') or f.endswith('.kmz')]
        if archivos:
            forzar_reconstruccion_bd(archivos[0])


# ==========================================
# 2. FUNCIONES GEOGRÁFICAS (CON CACHÉ)
# ==========================================

def dms_a_decimal(grados, minutos, segundos, direccion):
    decimal = float(grados) + (float(minutos) / 60) + (float(segundos) / 3600)
    if direccion.upper() in ['S', 'W', 'O']: decimal *= -1
    return decimal

def obtener_geodireccion(lat, lon):
    coords_key = f"{round(lat, 4)},{round(lon, 4)}"
    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("SELECT json_data FROM cache_nominatim WHERE coords_key = ?", (coords_key,))
    row = c.fetchone()
    
    if row:
        conn.close()
        return json.loads(row[0])

    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}&zoom=18&addressdetails=1"
    headers = {"User-Agent": "CoberturaCR_TelegramBot/4.0"}
    
    res_data = {"provincia": "N/D", "canton": "N/D", "distrito": "N/D", "barrio": "N/D", "direccion": "N/D"}
    try:
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            addr = r.json().get("address", {})
            res_data["barrio"] = addr.get("neighbourhood") or addr.get("residential") or addr.get("quarter") or addr.get("hamlet") or "No especificado"
            calle = addr.get("road") or addr.get("pedestrian") or addr.get("path") or ""
            numero = addr.get("house_number") or ""
            res_data["direccion"] = f"{calle} {numero}".strip() if calle else "Vía pública / Sin denominación"
            res_data["provincia"] = addr.get("state") or addr.get("region") or "N/D"
            res_data["canton"] = addr.get("county") or addr.get("municipality") or addr.get("city") or "N/D"
            res_data["distrito"] = addr.get("city_district") or addr.get("suburb") or addr.get("town") or addr.get("village") or "N/D"
            
            c.execute("INSERT OR REPLACE INTO cache_nominatim (coords_key, json_data) VALUES (?, ?)", (coords_key, json.dumps(res_data)))
            conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
        
    return res_data

def haversine_metros(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    delta_phi = radians(lat2 - lat1)
    delta_lambda = radians(lon2 - lon1)
    a = sin(delta_phi / 2)**2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def obtener_distancia_calle(lat1, lon1, lat2, lon2):
    ruta_key = f"{round(lat1, 5)},{round(lon1, 5)}_{round(lat2, 5)},{round(lon2, 5)}"
    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("SELECT distancia FROM cache_osrm WHERE ruta_key = ?", (ruta_key,))
    row = c.fetchone()
    
    if row:
        conn.close()
        return row[0]

    url = f"http://router.project-osrm.org/route/v1/foot/{lon1},{lat1};{lon2},{lat2}?overview=false"
    headers = {"User-Agent": "CoberturaCR_TelegramBot/4.0"}
    dist_final = None
    try:
        r = requests.get(url, headers=headers, timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "Ok":
                dist_final = data["routes"][0]["distance"]
                c.execute("INSERT OR REPLACE INTO cache_osrm (ruta_key, distancia) VALUES (?, ?)", (ruta_key, dist_final))
                conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
        
    return dist_final


# ==========================================
# 3. LÓGICA DE COBERTURA
# ==========================================

def consultar_cobertura_detallada(lat_user, lon_user):
    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM postes")
    if c.fetchone()[0] == 0:
        conn.close()
        return {"error": "db_vacia"}

    delta = (UMBRAL_AL_BORDE * 3) / METROS_POR_GRADO
    c.execute("SELECT codigo, lat, lon FROM postes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
              (lat_user - delta, lat_user + delta, lon_user - delta, lon_user + delta))
    candidatos = c.fetchall()
    conn.close()

    if not candidatos: return {"encontrado": False}

    candidatos_dist = []
    for cod, lat, lon in candidatos:
        d_lineal = haversine_metros(lat_user, lon_user, lat, lon)
        candidatos_dist.append((cod, lat, lon, d_lineal))

    candidatos_dist.sort(key=lambda x: x[3])
    mejores_n = candidatos_dist[:CANDIDATOS_A_EVALUAR]

    lista_evaluada = []
    for cod, lat, lon, d_lineal in mejores_n:
        d_calle = obtener_distancia_calle(lat_user, lon_user, lat, lon)
        if d_calle is not None:
            if d_lineal > 0 and d_calle > d_lineal * FACTOR_MAX_RUTA_VS_LINEAL:
                distancia_final = d_lineal
                tipo = "Línea recta"
            else:
                distancia_final = d_calle
                tipo = "Ruta por calles"
        else:
            distancia_final = d_lineal
            tipo = "Línea recta"

        lista_evaluada.append({"codigo": cod, "lat": lat, "lon": lon, "distancia": round(distancia_final, 1), "tipo": tipo})

    lista_evaluada.sort(key=lambda x: x["distancia"])
    return {"encontrado": True, "principal": lista_evaluada[0], "siguientes": lista_evaluada[1:4]}

# ==========================================
# 4. RESPUESTAS E INTERFAZ TELEGRAM
# ==========================================

def responder_consulta_individual(chat_id, lat, lon, reply_to_message_id):
    res = consultar_cobertura_detallada(lat, lon)
    if res and res.get("error"):
        bot.send_message(chat_id, "⚠️ <b>Atención:</b> La base de datos de red está vacía.", parse_mode="HTML")
        return

    geo = obtener_geodireccion(lat, lon)

    if not res["encontrado"]:
        tarjeta = (
            f"🔴 <b>FUERA DE RED — registrar para expansión</b>\n\n"
            f"📊 <b>Medición</b>  |  <b>Distancia</b>\n"
            f"──────────────────────\n"
            f"📏 Al tendido de fibra  →  Sin datos cercanos\n"
            f"──────────────────────\n"
            f"(límite: {LIMITE_COBERTURA} m)\n\n"
            f"📍 <b>Punto:</b> <code>{lat:.6f}, {lon:.6f}</code>\n\n"
            f"🗺️ <b>Ubicación:</b> {geo['provincia']}, {geo['canton']}, {geo['barrio']}\n"
            f"• Vía: {geo['direccion']}"
        )
        bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_to_message_id=reply_to_message_id)
        return

    p = res["principal"]
    d = p["distancia"]

    if d <= UMBRAL_COBERTURA_DIRECTA:
        header, icon = "🟢 <b>CON COBERTURA — Instalación Directa</b>", "🟢"
    elif d <= UMBRAL_AL_BORDE:
        header, icon = "🟠 <b>AL BORDE — Requiere Estudio</b>", "🟠"
    else:
        header, icon = "🔴 <b>FUERA DE RED — Registrar para expansión</b>", "🔴"

    s_txt = f"🔌 <b>Nodo más cercano:</b>\n• <code>{p['codigo']}</code>  →  <b>{int(d)} m</b> {icon}\n"
    if res["siguientes"]:
        s_txt = f"🔌 <b>Al nodo más cercano:</b>\n• <code>{p['codigo']}</code>  →  <b>{int(d)} m</b> {icon}\n\n<b>Siguientes:</b>\n"
        for s in res["siguientes"]:
            s_txt += f"• <code>{s['codigo']}</code>  →  {int(s['distancia'])} m\n"

    link = f"https://www.google.com/maps/dir/?api=1&origin={lat},{lon}&destination={p['lat']},{p['lon']}"
    tarjeta = (
        f"{header}\n\n"
        f"📊 <b>Medición</b>  |  <b>Distancia</b>\n"
        f"──────────────────────\n"
        f"📏 Al tendido de fibra  →  <b>{int(d)} m</b> {icon}\n\n"
        f"{s_txt}"
        f"──────────────────────\n"
        f"(límite: {LIMITE_COBERTURA} m)\n\n"
        f"📍 <b>Punto:</b> <code>{lat:.6f}, {lon:.6f}</code>\n\n"
        f"🗺️ <b>Ubicación territorial:</b>\n"
        f"• Provincia: {geo['provincia']}\n"
        f"• Cantón: {geo['canton']}\n"
        f"• Barrio: {geo['barrio']}\n"
        f"• Vía: {geo['direccion']}"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗺 Ver ruta al NAP en Google Maps", url=link))
    bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_markup=markup, reply_to_message_id=reply_to_message_id)

def responder_multiconsulta(chat_id, lista_coords, reply_to_message_id):
    bot.send_message(chat_id, f"⏳ Evaluando lote de {len(lista_coords)} ubicaciones. Calculando rutas...", reply_to_message_id=reply_to_message_id)
    
    mensaje = "📑 <b>RESUMEN DE COBERTURA (Lote)</b>\n\n"
    
    for i, (lat, lon) in enumerate(lista_coords, 1):
        res = consultar_cobertura_detallada(lat, lon)
        if not res or res.get("error"):
            mensaje += f"{i}️⃣ <code>{lat:.5f}, {lon:.5f}</code>\n├ Estado: ⚠️ Base de datos vacía\n\n"
            continue
            
        if not res["encontrado"]:
            mensaje += f"{i}️⃣ <code>{lat:.5f}, {lon:.5f}</code>\n├ Estado: 🔴 FUERA DE RED (Sin datos cercanos)\n└ NAP: N/A\n\n"
        else:
            p = res["principal"]
            d = p["distancia"]
            if d <= UMBRAL_COBERTURA_DIRECTA: estado = f"🟢 CON COBERT ({int(d)}m)"
            elif d <= UMBRAL_AL_BORDE: estado = f"🟠 AL BORDE ({int(d)}m)"
            else: estado = f"🔴 FUERA DE RED ({int(d)}m)"
            
            mensaje += f"{i}️⃣ <code>{lat:.5f}, {lon:.5f}</code>\n├ Estado: {estado}\n└ NAP: <code>{p['codigo']}</code>\n\n"
            
    mensaje += f"──────────────────────\n✅ Procesadas: {len(lista_coords)} ubicaciones."
    bot.send_message(chat_id, mensaje, parse_mode="HTML", reply_to_message_id=reply_to_message_id)


# ==========================================
# 5. MANEJADORES DE TELEGRAM
# ==========================================

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.reply_to(message, "📡 <b>Sistema de Validación (Oficina 4.0)</b>\nEnvía coordenadas individuales o múltiples a la vez.", parse_mode="HTML")

@bot.message_handler(commands=['id'])
def cmd_id(message):
    bot.reply_to(message, f"Tu ID de Telegram es: <code>{message.chat.id}</code>\n\n<i>(Copia este número y ponlo en la variable ADMINS del código para poder actualizar la red).</i>", parse_mode="HTML")

@bot.message_handler(content_types=['document'])
def recibir_archivo_red(message):
    """Permite al administrador subir un nuevo KMZ o ZIP directamente por el chat."""
    if message.chat.id not in ADMINS:
        bot.reply_to(message, "⛔ No tienes permisos administrativos para modificar la red.")
        return
        
    file_name = message.document.file_name
    if file_name.lower().endswith(('.zip', '.kmz')):
        msg = bot.reply_to(message, "📥 Descargando archivo y reconstruyendo la base de datos... Esto tomará un momento.")
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            
            with open(file_name, 'wb') as new_file:
                new_file.write(downloaded_file)
                
            postes_nuevos = forzar_reconstruccion_bd(file_name)
            bot.edit_message_text(f"✅ <b>¡Red actualizada con éxito!</b>\nSe insertaron {postes_nuevos} nodos/postes en la base de datos.", chat_id=message.chat.id, message_id=msg.message_id, parse_mode="HTML")
        except Exception as e:
            bot.edit_message_text(f"❌ <b>Error al actualizar:</b>\n{e}", chat_id=message.chat.id, message_id=msg.message_id, parse_mode="HTML")
    else:
        bot.reply_to(message, "⚠️ Solo acepto archivos .zip o .kmz para actualizar la red.")

@bot.message_handler(content_types=['location'])
def recibir_ubicacion(message):
    responder_consulta_individual(message.chat.id, message.location.latitude, message.location.longitude, message.message_id)

@bot.message_handler(func=lambda m: True)
def recibir_texto(message):
    texto = message.text.strip()
    
    # Extraer formato decimal
    patron_decimal = r'(-?\d{1,2}\.\d+)[,\s]+(-?\d{2,3}\.\d+)'
    matches_decimal = re.findall(patron_decimal, texto)
    
    coords = []
    if matches_decimal:
        for lat_str, lon_str in matches_decimal:
            coords.append((float(lat_str), float(lon_str)))
            
    if len(coords) == 1:
        responder_consulta_individual(message.chat.id, coords[0][0], coords[0][1], message.message_id)
    elif len(coords) > 1:
        if len(coords) > 15:
            bot.reply_to(message, "⚠️ Has enviado demasiadas coordenadas juntas. Por favor, evalúa un máximo de 15 a la vez.")
        else:
            responder_multiconsulta(message.chat.id, coords, message.message_id)
    else:
        # Fallback para DMS (Un solo punto)
        patron_dms = r'(\d+)[°\s]+(\d+)[\'\s]+([\d\.]+)"?\s*([NSns])[,;\s]*(\d+)[°\s]+(\d+)[\'\s]+([\d\.]+)"?\s*([WEweOo])'
        match_dms = re.search(patron_dms, texto)
        if match_dms:
            lat = dms_a_decimal(match_dms.group(1), match_dms.group(2), match_dms.group(3), match_dms.group(4))
            lon = dms_a_decimal(match_dms.group(5), match_dms.group(6), match_dms.group(7), match_dms.group(8))
            responder_consulta_individual(message.chat.id, lat, lon, message.message_id)
        else:
            bot.reply_to(message, "🤔 No pude reconocer coordenadas en ese mensaje.", parse_mode="HTML")

if __name__ == "__main__":
    inicializar_bd()
    logger.info("🚀 Validador 4.0 iniciado (Caché + Lotes + Upload)")
    bot.infinity_polling(skip_pending=True)
