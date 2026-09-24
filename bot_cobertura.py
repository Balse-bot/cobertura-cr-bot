import os
import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from math import radians, sin, cos, sqrt, atan2
import requests
import telebot
from telebot import types

TOKEN = "8621312939:AAGurzTO0_zfKSXYoHNVFwQnSWDWQOjoKTc"
DB_POSTES = "posteria_optimizada.db"
RADIO_MAXIMO_METROS = 400  # Límite comercial de factibilidad

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)

def obtener_geodireccion(lat, lon):
    """Obtiene provincia, cantón, distrito, barrio y dirección en tiempo real."""
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}&addressdetails=1"
    headers = {"User-Agent": "CoberturaCR_TelegramBot/1.0"}
    try:
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            addr = r.json().get("address", {})
            
            provincia = addr.get("state") or addr.get("region") or "N/D"
            canton = addr.get("county") or addr.get("municipality") or addr.get("city") or "N/D"
            distrito = addr.get("city_district") or addr.get("suburb") or addr.get("town") or addr.get("village") or "N/D"
            
            # Identificar barrio, residencial, condominio o vecindario
            barrio = (
                addr.get("neighbourhood") or 
                addr.get("residential") or 
                addr.get("quarter") or 
                addr.get("hamlet") or 
                "No especificado en mapa"
            )
            
            # Calle / Dirección de referencia
            calle = addr.get("road") or addr.get("pedestrian") or addr.get("path") or ""
            numero = addr.get("house_number") or ""
            direccion = f"{calle} {numero}".strip() if calle else "Calle sin nombre / Vía pública"
            
            return {
                "provincia": provincia,
                "canton": canton,
                "distrito": distrito,
                "barrio": barrio,
                "direccion": direccion
            }
    except Exception:
        pass
    
    return {
        "provincia": "No disponible",
        "canton": "No disponible",
        "distrito": "No disponible",
        "barrio": "No disponible",
        "direccion": "No disponible"
    }

def inicializar_desde_zip():
    """Extrae el KML desde el ZIP e indexa la red de postes."""
    if os.path.exists(DB_POSTES):
        print("✅ Base de datos SQLite detectada y lista.")
        return

    archivos = [f for f in os.listdir('.') if f.endswith('.zip') or f.endswith('.kmz')]
    if not archivos:
        print("⚠️ No se encontró ningún archivo .zip o .kmz.")
        return

    zip_path = archivos[0]
    print(f"📦 Procesando {zip_path}...")

    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS postes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT,
            lat REAL,
            lon REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_coords ON postes(lat, lon)")

    with zipfile.ZipFile(zip_path, 'r') as z:
        kml_candidatos = [n for n in z.namelist() if n.lower().endswith('.kml')]
        if not kml_candidatos:
            print("⚠️ No se encontró ningún KML en el ZIP.")
            conn.close()
            return
        
        kml_nombre = kml_candidatos[0]
        with z.open(kml_nombre) as kml_file:
            context = ET.iterparse(kml_file, events=('end',))
            batch = []
            total = 0

            for event, elem in context:
                tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if tag == 'Placemark':
                    coords_tag = elem.find('.//{*}coordinates')
                    if coords_tag is not None and coords_tag.text:
                        partes = coords_tag.text.strip().split()
                        if partes:
                            lon, lat, *_ = partes[0].split(',')
                            lat, lon = float(lat), float(lon)
                            
                            name_tag = elem.find('{*}name')
                            codigo = name_tag.text.strip() if (name_tag is not None and name_tag.text) else "S/C"

                            batch.append((codigo, lat, lon))
                            total += 1

                            if len(batch) >= 5000:
                                c.executemany("INSERT INTO postes (codigo, lat, lon) VALUES (?, ?, ?)", batch)
                                conn.commit()
                                batch = []
                                print(f"📍 {total} postes indexados...")
                    elem.clear()

            if batch:
                c.executemany("INSERT INTO postes (codigo, lat, lon) VALUES (?, ?, ?)", batch)
                conn.commit()

    conn.close()
    print(f"✅ Proceso terminado: {total} elementos registrados.")

def haversine_metros(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    delta_phi = radians(lat2 - lat1)
    delta_lambda = radians(lon2 - lon1)
    a = sin(delta_phi / 2)**2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def consultar_cobertura(lat_user, lon_user):
    if not os.path.exists(DB_POSTES):
        return None

    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    
    delta = 0.0055
    c.execute("SELECT codigo, lat, lon FROM postes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
              (lat_user - delta, lat_user + delta, lon_user - delta, lon_user + delta))
    candidatos = c.fetchall()
    
    if not candidatos:
        delta = 0.02
        c.execute("SELECT codigo, lat, lon FROM postes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                  (lat_user - delta, lat_user + delta, lon_user - delta, lon_user + delta))
        candidatos = c.fetchall()

    conn.close()

    if not candidatos:
        return {"encontrado": False}

    mejor_poste = None
    dist_min = float('inf')

    for cod, lat, lon in candidatos:
        d = haversine_metros(lat_user, lon_user, lat, lon)
        if d < dist_min:
            dist_min = d
            mejor_poste = {
                "codigo": cod,
                "lat": lat,
                "lon": lon,
                "distancia": round(d, 1),
                "cobertura": d <= RADIO_MAXIMO_METROS,
                "encontrado": True
            }

    return mejor_poste

def responder_consulta(chat_id, lat, lon):
    res = consultar_cobertura(lat, lon)
    if not res:
        bot.send_message(chat_id, "⚠️ Base de datos en mantenimiento. Por favor reintenta.")
        return

    if not res["encontrado"]:
        bot.send_message(
            chat_id,
            f"❌ <b>NO APLICA - SIN COBERTURA</b>\n\nNo se localizó infraestructura cercana a las coordenadas (<code>{lat:.5f}, {lon:.5f}</code>).",
            parse_mode="HTML"
        )
        return

    # Geocodificación inversa para dirección territorial real
    geo = obtener_geodireccion(lat, lon)
    dist = res["distancia"]
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={lat},{lon}&destination={res['lat']},{res['lon']}"

    if res["cobertura"]:
        estado_header = "✅ <b>FACTIBILIDAD: APLICA</b>"
        estado_badge = "🟢 <b>CON COBERTURA COMERCIAL</b>"
        obs = f"El punto consultado está dentro del rango permitido (≤ {RADIO_MAXIMO_METROS} m)."
    else:
        estado_header = "❌ <b>FACTIBILIDAD: NO APLICA</b>"
        estado_badge = "🔴 <b>FUERA DE RANGO MÁXIMO</b>"
        obs = f"Supera la distancia máxima autorizada de {RADIO_MAXIMO_METROS} m."

    tarjeta = (
        f"{estado_header}\n"
        f"{estado_badge}\n\n"
        f"📏 <b>Distancia al poste:</b> <b>{dist} metros</b> (Límite: {RADIO_MAXIMO_METROS}m)\n"
        f"🏷 <b>Poste / NAP más cercano:</b> <code>{res['codigo']}</code>\n\n"
        f"📍 <b>Dirección y Ubicación Territorial:</b>\n"
        f"• <b>Provincia:</b> {geo['provincia']}\n"
        f"• <b>Cantón:</b> {geo['canton']}\n"
        f"• <b>Distrito:</b> {geo['distrito']}\n"
        f"• <b>Barrio / Condominio:</b> {geo['barrio']}\n"
        f"• <b>Dirección / Vía:</b> {geo['direccion']}\n"
        f"• <b>Coordenadas cliente:</b> <code>{lat:.5f}, {lon:.5f}</code>\n\n"
        f"ℹ️ <b>Diagnóstico:</b> {obs}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗺 Ver ruta al poste en Google Maps", url=link_maps))
    bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_markup=markup)

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.reply_to(
        message,
        f"👋 ¡Hola, {message.from_user.first_name}!\n\n"
        "<b>Sistema de Validación de Cobertura Costa Rica</b>\n\n"
        "• <b>Rango máximo permitido:</b> hasta 400 metros del poste.\n\n"
        "<b>¿Cómo consultar?</b>\n"
        "1. Envía la <b>Ubicación GPS</b> tocando el clip 📎.\n"
        "2. O envía coordenadas directas (ejemplo: <code>9.9355, -84.0782</code>).",
        parse_mode="HTML"
    )

@bot.message_handler(content_types=['location'])
def recibir_ubicacion(message):
    responder_consulta(message.chat.id, message.location.latitude, message.location.longitude)

@bot.message_handler(func=lambda m: True)
def recibir_texto(message):
    match = re.search(r'(-?\d{1,2}\.\d+)[,\s]+(-?\d{2,3}\.\d+)', message.text.strip())
    if match:
        responder_consulta(message.chat.id, float(match.group(1)), float(match.group(2)))
    else:
        bot.reply_to(
            message,
            "⚠️ Formato no reconocido. Envía una ubicación GPS o coordenadas numéricas (ejemplo: <code>9.9355, -84.0782</code>).",
            parse_mode="HTML"
        )

if __name__ == "__main__":
    inicializar_desde_zip()
    print("🚀 Bot iniciado y listo con geocodificación territorial activa.")
    bot.infinity_polling(skip_pending=True)
