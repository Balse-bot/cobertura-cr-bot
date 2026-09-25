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

# Factor de tolerancia: si la ruta por calle es N veces más larga que la
# línea recta, se asume que OSM tiene datos incompletos en la zona y se
# usa la línea recta en su lugar.
FACTOR_MAX_RUTA_VS_LINEAL = 4

# Cantidad de candidatos cercanos que se evalúan por ruta real
CANDIDATOS_A_EVALUAR = 10

# Grados de latitud equivalentes a un metro (aprox., válido en Costa Rica)
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
    headers = {"User-Agent": "CoberturaCR_TelegramBot/3.3"}
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
        else:
            logger.warning("Nominatim respondió %s para (%s, %s)", r.status_code, lat, lon)
    except Exception as e:
        logger.warning("Fallo geocodificación inversa (%s, %s): %s", lat, lon, e)

    return {
        "provincia": "N/D", "canton": "N/D", "distrito": "N/D", "barrio": "N/D", "direccion": "N/D"
    }


def _extraer_kml(z):
    kml_candidatos = [n for n in z.namelist() if n.lower().endswith('.kml')]
    if kml_candidatos:
        logger.info("KML encontrado directamente en el zip: %s", kml_candidatos[0])
        return z.open(kml_candidatos[0])

    kmz_candidatos = [n for n in z.namelist() if n.lower().endswith('.kmz')]
    if kmz_candidatos:
        logger.info("No hay .kml directo; probando .kmz anidado: %s", kmz_candidatos[0])
        kmz_bytes = z.read(kmz_candidatos[0])
        kmz_zip = zipfile.ZipFile(io.BytesIO(kmz_bytes))
        kml_internos = [n for n in kmz_zip.namelist() if n.lower().endswith('.kml')]
        if kml_internos:
            logger.info("KML encontrado dentro del kmz anidado: %s", kml_internos[0])
            return kmz_zip.open(kml_internos[0])

    return None


def inicializar_desde_zip():
    if os.path.exists(DB_POSTES):
        logger.info("Base de datos SQLite lista (%s).", DB_POSTES)
        return

    archivos = [f for f in os.listdir('.') if f.endswith('.zip') or f.endswith('.kmz')]
    if not archivos:
        logger.warning("No hay archivo .zip o .kmz en el directorio de trabajo.")
        return

    zip_path = archivos[0]
    logger.info("Inicializando base de datos desde: %s", zip_path)
    conn = sqlite3.connect(DB_POSTES)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS postes (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, lat REAL, lon REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_coords ON postes(lat, lon)")

    total_insertados = 0
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            kml_file = _extraer_kml(z)
            if kml_file is None:
                logger.error("No se encontró ningún .kml en %s.", zip_path)
                conn.close()
                return

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
                                except (ValueError, IndexError) as e:
                                    logger.warning("Placemark con coordenadas inválidas omitido: %s", e)
                        elem.clear()
                if batch:
                    c.executemany("INSERT INTO postes (codigo, lat, lon) VALUES (?, ?, ?)", batch)
                    conn.commit()
                    total_insertados += len(batch)
    except Exception as e:
        logger.exception("Error inicializando la base de datos: %s", e)
    finally:
        conn.close()

    logger.info("Postes insertados: %d", total_insertados)


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
    headers = {"User-Agent": "CoberturaCR_TelegramBot/3.3"}
    try:
        r = requests.get(url, headers=headers, timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "Ok":
                return data["routes"][0]["distance"]
    except Exception as e:
        logger.info("OSRM no respondió para (%s,%s)->(%s,%s): %s", lat1, lon1, lat2, lon2, e)
    return None


def consultar_cobertura(lat_user, lon_user):
    if not os.path.exists(DB_POSTES):
        return None

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

    mejor_poste = None
    dist_min = float('inf')

    for cod, lat, lon, d_lineal in mejores_n:
        d_calle = obtener_distancia_calle(lat_user, lon_user, lat, lon)

        if d_calle is not None:
            if d_lineal > 0 and d_calle > d_lineal * FACTOR_MAX_RUTA_VS_LINEAL:
                distancia_final = d_lineal
                tipo_calculo = "Línea recta (ruta por calle no confiable)"
            else:
                distancia_final = d_calle
                tipo_calculo = "Ruta por calles (Vía Pública)"
        else:
            distancia_final = d_lineal
            tipo_calculo = "Línea recta (Referencia)"

        if distancia_final < dist_min:
            dist_min = distancia_final
            mejor_poste = {
                "codigo": cod,
                "lat": lat,
                "lon": lon,
                "distancia": round(dist_min, 1),
                "tipo_calculo": tipo_calculo,
                "encontrado": True
            }

    return mejor_poste


def responder_consulta(chat_id, lat, lon, reply_to_message_id=None):
    res = consultar_cobertura(lat, lon)
    if not res:
        bot.send_message(chat_id, "⚠️ El validador se está iniciando. Por favor reintenta en breve.", reply_to_message_id=reply_to_message_id)
        return

    geo = obtener_geodireccion(lat, lon)

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
        f"• <b>Barrio / Residencial:</b> {geo['barrio']}\n"
        f"• <b>Vía / Calle:</b> {geo['direccion']}\n"
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
    logger.info("🚀 Validador de Oficina iniciado...")
    bot.infinity_polling(skip_pending=True)
