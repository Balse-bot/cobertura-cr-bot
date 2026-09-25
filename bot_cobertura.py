import os
import io
import re
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

# Calibración exacta
UMBRAL_COBERTURA_DIRECTA = 70
UMBRAL_AL_BORDE = 500
LIMITE_COBERTURA = 400  # Límite visual de referencia

FACTOR_MAX_RUTA_VS_LINEAL = 4
CANDIDATOS_A_EVALUAR = 10
METROS_POR_GRADO = 111320

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("cobertura_bot")

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)


def dms_a_decimal(grados, minutos, segundos, direccion):
    decimal = float(grados) + (float(minutos) / 60) + (float(segundos) / 3600)
    if direccion.upper() in ['S', 'W', 'O']:
        decimal *= -1
    return decimal


def obtener_geodireccion(lat, lon):
    url = (
        f"https://nominatim.openstreetmap.org/reverse?format=jsonv2"
        f"&lat={lat}&lon={lon}&zoom=18&addressdetails=1"
    )
    headers = {"User-Agent": "CoberturaCR_TelegramBot/3.5"}
    try:
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            addr = r.json().get("address", {})

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
                "provincia": addr.get("state") or addr.get("region") or "N/D",
                "canton": addr.get("county") or addr.get("municipality") or addr.get("city") or "N/D",
                "distrito": addr.get("city_district") or addr.get("suburb") or addr.get("town") or addr.get("village") or "N/D",
                "barrio": barrio,
                "direccion": direccion
            }
    except Exception as e:
        logger.warning("Fallo geocodificación inversa: %s", e)

    return {
        "provincia": "N/D", "canton": "N/D", "distrito": "N/D", "barrio": "N/D", "direccion": "N/D"
    }


def _extraer_kml(z):
    kml_candidatos = [n for n in z.namelist() if n.lower().endswith('.kml')]
    if kml_candidatos:
        return z.open(kml_candidatos[0])

    kmz_candidatos = [n for n in z.namelist() if n.lower().endswith('.kmz')]
    if kmz_candidatos:
        kmz_bytes = z.read(kmz_candidatos[0])
        kmz_zip = zipfile.ZipFile(io.BytesIO(kmz_bytes))
        kml_internos = [n for n in kmz_zip.namelist() if n.lower().endswith('.kml')]
        if kml_internos:
            return kmz_zip.open(kml_internos[0])
    return None


def inicializar_desde_zip():
    archivos = [f for f in os.listdir('.') if f.endswith('.zip') or f.endswith('.kmz')]
    if not archivos:
        logger.warning("⚠️ No hay archivo .zip o .kmz en el directorio.")
        return

    zip_path = archivos[0]
    
    # Si la BD ya existe, validemos que tenga registros adentro
    if os.path.exists(DB_POSTES):
        conn = sqlite3.connect(DB_POSTES)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM postes")
        count = c.fetchone()[0]
        conn.close()
        if count > 0:
            logger.info("✅ Base de datos SQLite lista con %d postes.", count)
            return
        else:
            logger.warning("⚠️ La base de datos existe pero está vacía. Reconstruyendo...")
            os.remove(DB_POSTES)

    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS postes (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, lat REAL, lon REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_coords ON postes(lat, lon)")

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
                                            batch = []
                                    except (ValueError, IndexError):
                                        pass
                            elem.clear()
                    if batch:
                        c.executemany("INSERT INTO postes (codigo, lat, lon) VALUES (?, ?, ?)", batch)
                        conn.commit()
    except Exception as e:
        logger.exception("Error inicializando BD: %s", e)
    finally:
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
    headers = {"User-Agent": "CoberturaCR_TelegramBot/3.5"}
    try:
        r = requests.get(url, headers=headers, timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "Ok":
                return data["routes"][0]["distance"]
    except Exception:
        pass
    return None


def consultar_cobertura_detallada(lat_user, lon_user):
    if not os.path.exists(DB_POSTES):
        return {"error": "db_no_encontrada"}

    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM postes")
    count = c.fetchone()[0]
    conn.close()

    if count == 0:
        return {"error": "db_vacia"}

    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    delta = (UMBRAL_AL_BORDE * 3) / METROS_POR_GRADO
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

        lista_evaluada.append({
            "codigo": cod,
            "lat": lat,
            "lon": lon,
            "distancia": round(distancia_final, 1),
            "tipo": tipo
        })

    lista_evaluada.sort(key=lambda x: x["distancia"])
    
    return {
        "encontrado": True,
        "principal": lista_evaluada[0],
        "siguientes": lista_evaluada[1:4] if len(lista_evaluada) > 1 else []
    }


def responder_consulta(chat_id, lat, lon, reply_to_message_id=None):
    res = consultar_cobertura_detallada(lat, lon)
    
    if res and res.get("error"):
        bot.send_message(
            chat_id, 
            "⚠️ <b>Atención:</b> La base de datos de red está vacía o no se encuentra el archivo KMZ/ZIP en el servidor. Por favor avise al administrador.", 
            parse_mode="HTML", 
            reply_to_message_id=reply_to_message_id
        )
        return

    geo = obtener_geodireccion(lat, lon)

    if not res["encontrado"]:
        tarjeta = (
            f"🔴 <b>FUERA DE RED — registrar para expansión</b>\n\n"
            f"📊 <b>Medición</b>  |  <b>Distancia</b>\n"
            f"──────────────────────\n"
            f"📏 Al tendido de fibra  →  Sin datos cercanos\n\n"
            f"──────────────────────\n"
            f"(límite: {LIMITE_COBERTURA} m)\n\n"
            f"📍 <b>Punto:</b> <code>{lat:.6f}, {lon:.6f}</code>\n\n"
            f"🗺️ <b>Ubicación territorial:</b>\n"
            f"• Provincia: {geo['provincia']}\n"
            f"• Cantón: {geo['canton']}\n"
            f"• Distrito/Zona: {geo['distrito']}\n"
            f"• Barrio: {geo['barrio']}\n"
            f"• Vía: {geo['direccion']}"
        )
        bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_to_message_id=reply_to_message_id)
        return

    p_ppal = res["principal"]
    dist = p_ppal["distancia"]

    if dist <= UMBRAL_COBERTURA_DIRECTA:
        header = "🟢 <b>CON COBERTURA — Instalación Directa</b>"
        icono_dist = "🟢"
    elif dist <= UMBRAL_AL_BORDE:
        header = "🟠 <b>AL BORDE — Requiere Estudio</b>"
        icono_dist = "🟠"
    else:
        header = "🔴 <b>FUERA DE RED — Registrar para expansión</b>"
        icono_dist = "🔴"

    siguientes_txt = ""
    if res["siguientes"]:
        siguientes_txt = "🔌 <b>Al nodo más cercano:</b>\n"
        siguientes_txt += f"• <code>{p_ppal['codigo']}</code>  →  <b>{int(dist)} m</b> {icono_dist}\n\n"
        siguientes_txt += "<b>Siguientes:</b>\n"
        for s in res["siguientes"]:
            siguientes_txt += f"• <code>{s['codigo']}</code>  →  {int(s['distancia'])} m\n"
    else:
        siguientes_txt = f"🔌 <b>Nodo más cercano:</b>\n• <code>{p_ppal['codigo']}</code>  →  <b>{int(dist)} m</b> {icono_dist}\n"

    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={lat},{lon}&destination={p_ppal['lat']},{p_ppal['lon']}"

    tarjeta = (
        f"{header}\n\n"
        f"📊 <b>Medición</b>  |  <b>Distancia</b>\n"
        f"──────────────────────\n"
        f"📏 Al tendido de fibra  →  <b>{int(dist)} m</b> {icono_dist}\n\n"
        f"{siguientes_txt}"
        f"──────────────────────\n"
        f"(límite: {LIMITE_COBERTURA} m)\n\n"
        f"📍 <b>Punto:</b> <code>{lat:.6f}, {lon:.6f}</code>\n\n"
        f"🗺️ <b>Ubicación territorial:</b>\n"
        f"• Provincia: {geo['provincia']}\n"
        f"• Cantón: {geo['canton']}\n"
        f"• Distrito/Zona: {geo['distrito']}\n"
        f"• Barrio: {geo['barrio']}\n"
        f"• Vía: {geo['direccion']}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗺 Ver ruta al NAP en Google Maps", url=link_maps))
    bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_markup=markup, reply_to_message_id=reply_to_message_id)


@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.reply_to(
        message,
        "📡 <b>Sistema de Validación de Cobertura (Oficina)</b>\n\n"
        "Envía coordenadas o pines para validar factibilidad de red.",
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
    else:
        bot.reply_to(
            message,
            "🤔 No pude reconocer coordenadas en ese mensaje.\n"
            "Envía coordenadas en formato decimal (ej: <code>9.928069, -84.090725</code>) o DMS.",
            parse_mode="HTML"
        )


if __name__ == "__main__":
    inicializar_desde_zip()
    logger.info("🚀 Validador de Oficina blindado iniciado...")
    bot.infinity_polling(skip_pending=True)
