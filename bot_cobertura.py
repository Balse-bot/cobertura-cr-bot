import os
import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from math import radians, sin, cos, sqrt, atan2
import telebot
from telebot import types

TOKEN = "8621312939:AAGurzTO0_zfKSXYoHNVFwQnSWDWQOjoKTc"
DB_POSTES = "posteria_optimizada.db"
RADIO_MAXIMO_METROS = 100  # Límite técnico comercial para cable drop

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)

def inicializar_desde_zip():
    """Detecta el archivo .zip o .kmz, extrae el KML y crea la base de datos SQLite."""
    if os.path.exists(DB_POSTES):
        print("✅ Base de datos SQLite detectada y lista.")
        return

    # Buscar cualquier archivo .zip o .kmz subido al repositorio
    archivos = [f for f in os.listdir('.') if f.endswith('.zip') or f.endswith('.kmz')]
    if not archivos:
        print("⚠️ No se encontró ningún archivo .zip o .kmz en el directorio.")
        return

    zip_path = archivos[0]
    print(f"📦 Procesando {zip_path} y generando índice espacial...")

    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS postes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT,
            lat REAL,
            lon REAL,
            provincia TEXT,
            canton TEXT,
            distrito TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_coords ON postes(lat, lon)")

    with zipfile.ZipFile(zip_path, 'r') as z:
        kml_candidatos = [n for n in z.namelist() if n.lower().endswith('.kml')]
        if not kml_candidatos:
            print("⚠️ No se encontró ningún archivo .kml dentro del ZIP.")
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
                            
                            prov, cant, dist = "N/D", "N/D", "N/D"
                            for sd in elem.findall('.//{*}SimpleData'):
                                c_nom = sd.attrib.get('name', '').lower()
                                val = sd.text or ""
                                if 'prov' in c_nom: prov = val
                                elif 'cant' in c_nom: cant = val
                                elif 'dist' in c_nom: dist = val

                            batch.append((codigo, lat, lon, prov, cant, dist))
                            total += 1

                            if len(batch) >= 5000:
                                c.executemany("INSERT INTO postes (codigo, lat, lon, provincia, canton, distrito) VALUES (?, ?, ?, ?, ?, ?)", batch)
                                conn.commit()
                                batch = []
                                print(f"📍 {total} postes indexados...")
                    elem.clear()

            if batch:
                c.executemany("INSERT INTO postes (codigo, lat, lon, provincia, canton, distrito) VALUES (?, ?, ?, ?, ?, ?)", batch)
                conn.commit()

    conn.close()
    print(f"✅ Inicialización completa: {total} postes guardados en SQLite.")

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
    
    # Rango inicial de búsqueda (~250m)
    delta = 0.0025
    c.execute("""
        SELECT codigo, lat, lon, provincia, canton, distrito
        FROM postes
        WHERE lat BETWEEN ? AND ?
          AND lon BETWEEN ? AND ?
    """, (lat_user - delta, lat_user + delta, lon_user - delta, lon_user + delta))
    candidatos = c.fetchall()
    
    # Si no hay postes cercanos, ampliar radio (~1.5 km)
    if not candidatos:
        delta = 0.015
        c.execute("""
            SELECT codigo, lat, lon, provincia, canton, distrito
            FROM postes
            WHERE lat BETWEEN ? AND ?
              AND lon BETWEEN ? AND ?
        """, (lat_user - delta, lat_user + delta, lon_user - delta, lon_user + delta))
        candidatos = c.fetchall()

    conn.close()

    if not candidatos:
        return {"encontrado": False}

    mejor_poste = None
    dist_min = float('inf')

    for cod, lat, lon, prov, cant, dist in candidatos:
        d = haversine_metros(lat_user, lon_user, lat, lon)
        if d < dist_min:
            dist_min = d
            mejor_poste = {
                "codigo": cod,
                "lat": lat,
                "lon": lon,
                "provincia": prov,
                "canton": cant,
                "distrito": dist,
                "distancia": round(d, 1),
                "cobertura": d <= RADIO_MAXIMO_METROS,
                "encontrado": True
            }

    return mejor_poste

def responder_consulta(chat_id, lat, lon):
    res = consultar_cobertura(lat, lon)
    if not res:
        bot.send_message(chat_id, "⚠️ El sistema está indexando la red por primera vez. Intenta en 15 segundos.")
        return

    if not res["encontrado"]:
        bot.send_message(
            chat_id,
            f"🔴 <b>FUERA DE COBERTURA TOTAL</b>\n\nNo se detectaron postes en un radio de 1.5 km de (<code>{lat:.5f}, {lon:.5f}</code>).",
            parse_mode="HTML"
        )
        return

    dist = res["distancia"]
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={lat},{lon}&destination={res['lat']},{res['lon']}"

    if res["cobertura"]:
        titulo = "🟢 <b>FACTIBLE - CON COBERTURA</b>"
        obs = f"Acometida drop dentro de norma (≤ {RADIO_MAXIMO_METROS}m)."
    else:
        titulo = "🔴 <b>NO FACTIBLE - FUERA DE RANGO</b>"
        obs = f"Supera el límite permitido de {RADIO_MAXIMO_METROS}m."

    tarjeta = (
        f"{titulo}\n\n"
        f"📍 <b>División Territorial:</b>\n"
        f"• <b>Provincia:</b> {res['provincia']}\n"
        f"• <b>Cantón:</b> {res['canton']}\n"
        f"• <b>Distrito:</b> {res['distrito']}\n\n"
        f"⚡ <b>Datos de Red:</b>\n"
        f"• <b>Poste / NAP:</b> <code>{res['codigo']}</code>\n"
        f"• <b>Distancia al cliente:</b> <b>{dist} metros</b>\n\n"
        f"📝 <b>Observación:</b> {obs}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗺 Ver ruta en Google Maps", url=link_maps))
    bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_markup=markup)

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.reply_to(
        message,
        f"👋 ¡Hola, {message.from_user.first_name}!\n\n"
        "<b>Validador de Cobertura Costa Rica</b>\n\n"
        "Para consultar factibilidad:\n"
        "1. Toca el clip 📎 y envía tu <b>Ubicación GPS</b>.\n"
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
        bot.reply_to(message, "⚠️ Envía una ubicación GPS o coordenadas numéricas (ejemplo: <code>9.9355, -84.0782</code>).", parse_mode="HTML")

if __name__ == "__main__":
    inicializar_desde_zip()
    print("🚀 Validador listo y escuchando en Koyeb...")
    bot.infinity_polling(skip_pending=True)
