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

# Calibración exacta
UMBRAL_COBERTURA_DIRECTA = 70   
UMBRAL_AL_BORDE = 500           

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)

def dms_a_decimal(grados, minutos, segundos, direccion):
    decimal = float(grados) + (float(minutos) / 60) + (float(segundos) / 3600)
    if direccion.upper() in ['S', 'W', 'O']:  
        decimal *= -1
    return decimal

def obtener_geodireccion(lat, lon):
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}&addressdetails=1"
    headers = {"User-Agent": "CoberturaCR_TelegramBot/3.1"}
    try:
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            addr = r.json().get("address", {})
            
            barrio = (
                addr.get("neighbourhood") or 
                addr.get("residential") or 
                addr.get("quarter") or 
                "No especificado en mapa"
            )
            
            calle = addr.get("road") or addr.get("pedestrian") or ""
            numero = addr.get("house_number") or ""
            direccion = f"{calle} {numero}".strip() if calle else "Vía pública / Sin denominación"
            
            return {
                "provincia": addr.get("state") or addr.get("region") or "N/D",
                "canton": addr.get("county") or addr.get("municipality") or "N/D",
                "distrito": addr.get("city_district") or addr.get("suburb") or "N/D",
                "barrio": barrio,
                "direccion": direccion
            }
    except Exception:
        pass
    
    return {
        "provincia": "N/D", "canton": "N/D", "distrito": "N/D", "barrio": "N/D", "direccion": "N/D"
    }

def inicializar_desde_zip():
    if os.path.exists(DB_POSTES):
        print("✅ Base de datos SQLite lista.")
        return

    archivos = [f for f in os.listdir('.') if f.endswith('.zip') or f.endswith('.kmz')]
    if not archivos:
        print("⚠️ No hay archivo .zip o .kmz.")
        return

    zip_path = archivos[0]
    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS postes (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, lat REAL, lon REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_coords ON postes(lat, lon)")

    with zipfile.ZipFile(zip_path, 'r') as z:
        kml_candidatos = [n for n in z.namelist() if n.lower().endswith('.kml')]
        if kml_candidatos:
            with z.open(kml_candidatos[0]) as kml_file:
                context = ET.iterparse(kml_file, events=('end',))
                batch = []
                for event, elem in context:
                    tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                    if tag == 'Placemark':
                        coords = elem.find('.//{*}coordinates')
                        if coords is not None and coords.text:
                            partes = coords.text.strip().split()
                            if partes:
                                lon, lat, *_ = partes[0].split(',')
                                name_tag = elem.find('{*}name')
                                cod = name_tag.text.strip() if (name_tag is not None and name_tag.text) else "S/C"
                                batch.append((cod, float(lat), float(lon)))
                                if len(batch) >= 5000:
                                    c.executemany("INSERT INTO postes (codigo, lat, lon) VALUES (?, ?, ?)", batch)
                                    conn.commit()
                                    batch = []
                        elem.clear()
                if batch:
                    c.executemany("INSERT INTO postes (codigo, lat, lon) VALUES (?, ?, ?)", batch)
                    conn.commit()
    conn.close()

def haversine_metros(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    delta_phi = radians(lat2 - lat1)
    delta_lambda = radians(lon2 - lon1)
    a = sin(delta_phi / 2)**2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def obtener_distancia_calle(lat1, lon1, lat2, lon2):
    url = f"http://router.project-osrm.org/route/v1/foot/{lon1},{lat1};{lon2},{lat2}?overview=false"
    headers = {"User-Agent": "CoberturaCR_TelegramBot/3.1"}
    try:
        r = requests.get(url, headers=headers, timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "Ok":
                return data["routes"][0]["distance"]
    except Exception:
        pass
    return None

def consultar_cobertura(lat_user, lon_user):
    if not os.path.exists(DB_POSTES):
        return None

    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    
    delta = 0.015
    c.execute("SELECT codigo, lat, lon FROM postes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
              (lat_user - delta, lat_user + delta, lon_user - delta, lon_user + delta))
    candidatos = c.fetchall()
    conn.close()

    if not candidatos:
        return {"encontrado": False}

    candidatos_dist = []
    for cod, lat, lon in candidatos:
        d_lineal = haversine_metros(lat_user, lon_user, lat, lon)
        candidatos_dist.append((cod, lat, lon, d_lineal))
    
    candidatos_dist.sort(key=lambda x: x[3])
    mejores_5 = candidatos_dist[:5]

    mejor_poste = None
    dist_min = float('inf')

    for cod, lat, lon, d_lineal in mejores_5:
        d_calle = obtener_distancia_calle(lat_user, lon_user, lat, lon)
        distancia_final = d_calle if d_calle is not None else d_lineal
        
        if distancia_final < dist_min:
            dist_min = distancia_final
            mejor_poste = {
                "codigo": cod,
                "lat": lat,
                "lon": lon,
                "distancia": round(dist_min, 1),
                "tipo_calculo": "Ruta por calles (Vía Pública)" if d_calle is not None else "Línea recta (Referencia)",
                "encontrado": True
            }

    return mejor_poste

def responder_consulta(chat_id, lat, lon, reply_to_message_id=None):
    res = consultar_cobertura(lat, lon)
    if not res:
        bot.send_message(chat_id, "⚠️ El validador se está iniciando. Por favor reintenta en breve.", reply_to_message_id=reply_to_message_id)
        return

    geo = obtener_geodireccion(lat, lon)
    
    # Aprendizaje del sistema corporativo: Alerta de Condominio basada en nomenclatura
    barrio_lower = geo['barrio'].lower()
    es_condominio = any(palabra in barrio_lower for palabra in ["condominio", "residencial", "urbanización", "condo"])
    alerta_condominio = f"\n🏢 <b>Condominio cercano:</b> {geo['barrio']} (Verificar acceso)" if es_condominio else ""

    if not res["encontrado"]:
        mensaje_rechazo = (
            "🔴 <b>NO APLICA (Uncovered)</b>\n\n"
            f"📍 <b>Coordenadas:</b> <code>{lat:.6f}, {lon:.6f}</code>\n"
            "ℹ️ <b>Diagnóstico:</b> No hay red conocida en ese punto. La venta seguirá como huella."
        )
        bot.send_message(chat_id, mensaje_rechazo, parse_mode="HTML", reply_to_message_id=reply_to_message_id)
        return

    dist = res["distancia"]
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={lat},{lon}&destination={res['lat']},{res['lon']}"
    tipo_ruta = res.get("tipo_calculo", "Línea recta")

    # Aprendizaje del sistema corporativo: Diagnósticos exactos
    if dist <= UMBRAL_COBERTURA_DIRECTA:
        badge = "🟢 <b>CON COBERTURA</b>"
        obs = "Hay red en ese punto. Factible para instalación directa."
    elif dist <= UMBRAL_AL_BORDE:
        badge = "🟠 <b>AL BORDE</b>"
        obs = f"Está a {int(dist)} metros de la red. Hace falta el estudio para confirmarlo."
    else:
        badge = "🔴 <b>NO APLICA (Uncovered)</b>"
        obs = "No hay red conocida en ese punto. La venta seguirá como huella."

    tarjeta = (
        f"{badge}\n\n"
        f"📏 <b>Distancia a la red:</b> <b>{dist} metros</b>\n"
        f"🛣 <b>Medición:</b> {tipo_ruta}\n"
        f"🏷 <b>NAP más cercano:</b> <code>{res['codigo']}</code>"
        f"{alerta_condominio}\n\n"
        f"📍 <b>Ubicación:</b>\n"
        f"• <b>Provincia:</b> {geo['provincia']}\n"
        f"• <b>Cantón:</b> {geo['canton']}\n"
        f"• <b>Distrito:</b> {geo['distrito']}\n"
        f"• <b>Barrio:</b> {geo['barrio']}\n"
        f"• <b>Coordenadas:</b> <code>{lat:.6f}, {lon:.6f}</code>\n\n"
        f"ℹ️ <b>Diagnóstico:</b> {obs}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗺 Ver ruta al NAP en Google Maps", url=link_maps))
    bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_markup=markup, reply_to_message_id=reply_to_message_id)

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.reply_to(
        message,
        "📡 <b>Sistema de Validación de Cobertura (Modo Field)</b>\n\n"
        "Envía una ubicación GPS o coordenadas para validar factibilidad.",
        parse_mode="HTML"
    )

@bot.message_handler(content_types=['location'])
def recibir_ubicacion(message):
    responder_consulta(message.chat.id, message.location.latitude, message.location.longitude, message.message_id)

@bot.message_handler(func=lambda m: True)
def recibir_texto(message):
    texto = message.text.strip()
    
    patron_dms = r'(\d+)[°\s]+(\d+)[\'\s]+([\d\.]+)"?\s*([NSns])[,;\s]*(\d+)[°\s]+(\d+)[\'\s]+([\d\.]+)"?\s*([WEweOo])'
    match_dms = re.search(patron_dms, texto)
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

if __name__ == "__main__":
    inicializar_desde_zip()
    print("🚀 Validador Modo Field iniciado...")
    bot.infinity_polling(skip_pending=True)
