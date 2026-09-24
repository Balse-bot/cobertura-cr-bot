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

# Calibración exacta según sistema corporativo
UMBRAL_COBERTURA_DIRECTA = 70   # Metros para "Con cobertura"
UMBRAL_AL_BORDE = 500           # Metros para "Al borde" (Requiere estudio)

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)

def dms_a_decimal(grados, minutos, segundos, direccion):
    """Convierte Grados, Minutos y Segundos a formato Decimal."""
    decimal = float(grados) + (float(minutos) / 60) + (float(segundos) / 3600)
    if direccion.upper() in ['S', 'W', 'O']:  # Sur u Oeste/West son negativos
        decimal *= -1
    return decimal

def obtener_geodireccion(lat, lon):
    """Consulta OpenStreetMap para obtener división territorial y dirección."""
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}&addressdetails=1"
    headers = {"User-Agent": "CoberturaCR_TelegramBot/2.1"}
    try:
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            addr = r.json().get("address", {})
            
            provincia = addr.get("state") or addr.get("region") or "N/D"
            canton = addr.get("county") or addr.get("municipality") or addr.get("city") or "N/D"
            distrito = addr.get("city_district") or addr.get("suburb") or addr.get("town") or addr.get("village") or "N/D"
            
            barrio = (
                addr.get("neighbourhood") or 
                addr.get("residential") or 
                addr.get("quarter") or 
                addr.get("hamlet") or 
                "No especificado en mapa"
            )
            
            calle = addr.get("road") or addr.get("pedestrian") or addr.get("path") or ""
            numero = addr.get("house_number") or ""
            direccion = f"{calle} {numero}".strip() if calle else "Vía pública / Sin denominación"
            
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
    """Crea la base de datos indexada en SQLite a partir del archivo KML/KMZ."""
    if os.path.exists(DB_POSTES):
        print("✅ Base de datos SQLite detectada y lista.")
        return

    archivos = [f for f in os.listdir('.') if f.endswith('.zip') or f.endswith('.kmz')]
    if not archivos:
        print("⚠️ No se encontró ningún archivo .zip o .kmz en el directorio.")
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
            print("⚠️ No se encontró ningún archivo KML dentro del comprimido.")
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
    
    delta = 0.0065
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
                "encontrado": True
            }

    return mejor_poste

def responder_consulta(chat_id, lat, lon, reply_to_message_id=None):
    res = consultar_cobertura(lat, lon)
    if not res:
        bot.send_message(chat_id, "⚠️ El validador se está iniciando. Por favor reintenta en breve.", reply_to_message_id=reply_to_message_id)
        return

    if not res["encontrado"]:
        bot.send_message(
            chat_id,
            f"🔴 <b>NO APLICA - SIN RED CERCANA</b>\n\nNo se localizó infraestructura cercana a las coordenadas (<code>{lat:.6f}, {lon:.6f}</code>).",
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id
        )
        return

    dist = res["distancia"]
    geo = obtener_geodireccion(lat, lon)
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={lat},{lon}&destination={res['lat']},{res['lon']}"

    # Advertencia si cae fuera del GAM (San José, Heredia, Alajuela, Cartago)
    gam_provincias = ["san josé", "heredia", "alajuela", "cartago"]
    nota_gam = ""
    if geo['provincia'].lower() not in gam_provincias and geo['provincia'] != "N/D":
        nota_gam = "\n⚠️ <i>Atención: Zona fuera del GAM central.</i>\n"

    # Clasificación homologada
    if dist <= UMBRAL_COBERTURA_DIRECTA:
        badge = "🟢 <b>CON COBERTURA</b>"
        obs = "Hay red en ese punto. Factible para instalación directa."
    elif dist <= UMBRAL_AL_BORDE:
        badge = "🟠 <b>AL BORDE</b>"
        obs = f"Está a {int(dist)} metros de la red. Hace falta el estudio para confirmarlo."
    else:
        badge = "🔴 <b>NO APLICA</b>"
        obs = f"Supera la distancia técnica permitida de {UMBRAL_AL_BORDE} metros."

    tarjeta = (
        f"{badge}\n\n"
        f"📏 <b>Distancia a la red:</b> <b>{dist} metros</b>\n"
        f"🏷 <b>Poste / NAP más cercano:</b> <code>{res['codigo']}</code>\n\n"
        f"📍 <b>Ubicación Territorial:</b>\n"
        f"• <b>Provincia:</b> {geo['provincia']}\n"
        f"• <b>Cantón:</b> {geo['canton']}\n"
        f"• <b>Distrito:</b> {geo['distrito']}\n"
        f"• <b>Barrio / Residencial:</b> {geo['barrio']}\n"
        f"• <b>Vía / Calle:</b> {geo['direccion']}\n"
        f"• <b>Punto consultado:</b> <code>{lat:.6f}, {lon:.6f}</code>\n\n"
        f"ℹ️ <b>Diagnóstico:</b> {obs}{nota_gam}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗺 Ver ruta al poste", url=link_maps))
    bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_markup=markup, reply_to_message_id=reply_to_message_id)

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.reply_to(
        message,
        f"👋 ¡Hola, {message.from_user.first_name}!\n\n"
        "<b>Sistema de Validación de Cobertura Costa Rica</b>\n\n"
        "• <b>0 a 70 m:</b> 🟢 Con cobertura\n"
        "• <b>71 a 500 m:</b> 🟠 Al borde (Requiere estudio)\n"
        "• <b>> 500 m:</b> 🔴 No aplica\n\n"
        "Puedes enviar tu <b>ubicación GPS</b>, o escribir coordenadas en Decimal o Grados:\n"
        "📍 <code>9.953482, -84.150603</code>\n"
        "📍 <code>9°50'11.5\"N 83°52'41.5\"W</code>",
        parse_mode="HTML"
    )

@bot.message_handler(content_types=['location'])
def recibir_ubicacion(message):
    responder_consulta(message.chat.id, message.location.latitude, message.location.longitude, message.message_id)

@bot.message_handler(func=lambda m: True)
def recibir_texto(message):
    texto = message.text.strip()
    
    # 1. Buscar formato GMS / DMS (ej: 9°50'11.5"N 83°52'41.5"W)
    patron_dms = r'(\d+)[°\s]+(\d+)[\'\s]+([\d\.]+)"?\s*([NSns])[,;\s]*(\d+)[°\s]+(\d+)[\'\s]+([\d\.]+)"?\s*([WEweOo])'
    match_dms = re.search(patron_dms, texto)
    
    # 2. Buscar formato Decimal normal (ej: 9.953482, -84.150603)
    patron_decimal = r'(-?\d{1,2}\.\d+)[,\s]+(-?\d{2,3}\.\d+)'
    match_decimal = re.search(patron_decimal, texto)

    if match_dms:
        lat = dms_a_decimal(match_dms.group(1), match_dms.group(2), match_dms.group(3), match_dms.group(4))
        lon = dms_a_decimal(match_dms.group(5), match_dms.group(6), match_dms.group(7), match_dms.group(8))
        responder_consulta(message.chat.id, lat, lon, message.message_id)
        
    elif match_decimal:
        lat = float(match_decimal.group(1))
        lon = float(match_decimal.group(2))
        responder_consulta(message.chat.id, lat, lon, message.message_id)
        
    else:
        if message.chat.type == "private":
            bot.reply_to(
                message,
                "⚠️ Formato no reconocido. Envía una ubicación GPS, decimales o grados (ej: <code>9°50'11.5\"N 83°52'41.5\"W</code>).",
                parse_mode="HTML"
            )

if __name__ == "__main__":
    inicializar_desde_zip()
    print("🚀 Validador oficial iniciado (Soporte GMS y Decimal activado)...")
    bot.infinity_polling(skip_pending=True)
