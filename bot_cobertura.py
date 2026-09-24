import re
import requests
import telebot
from telebot import types

TOKEN = "8621312939:AAGurzTO0_zfKSXYoHNVFwQnSWDWQOjoKTc"
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)

def dms_a_decimal(grados, minutos, segundos, direccion):
    """Convierte Grados, Minutos y Segundos a formato Decimal."""
    decimal = float(grados) + (float(minutos) / 60) + (float(segundos) / 3600)
    if direccion.upper() in ['S', 'W', 'O']:  
        decimal *= -1
    return decimal

def obtener_geodireccion(lat, lon):
    """Consulta OpenStreetMap para obtener división territorial (opcional pero útil)."""
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}&addressdetails=1"
    headers = {"User-Agent": "CoberturaCR_TelegramBot/3.0"}
    try:
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            addr = r.json().get("address", {})
            return {
                "provincia": addr.get("state") or addr.get("region") or "N/D",
                "canton": addr.get("county") or addr.get("municipality") or "N/D",
                "distrito": addr.get("city_district") or addr.get("suburb") or "N/D",
                "barrio": addr.get("neighbourhood") or addr.get("residential") or "No especificado en mapa"
            }
    except Exception:
        pass
    return {"provincia": "N/D", "canton": "N/D", "distrito": "N/D", "barrio": "N/D"}

def consultar_api_oficial(lat, lon):
    """Consulta directamente los servidores de American Data."""
    url = "https://vendors.data.cr/api/v1/coverage"
    
    # Asumimos este formato estándar. Si da error, ajustaremos viendo la pestaña "Payload"
    payload = {
        "lat": lat,
        "lng": lon
    }
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # "Authorization": "Bearer TU_TOKEN_AQUI" # Descomentar si el servidor rechaza la conexión
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"Error del servidor: {r.status_code} - {r.text}")
            return None
    except Exception as e:
        print(f"Error de conexión: {e}")
        return None

def responder_consulta(chat_id, lat, lon, reply_to_message_id=None):
    res_api = consultar_api_oficial(lat, lon)
    
    if not res_api:
        bot.send_message(
            chat_id, 
            "⚠️ <b>Error de conexión con el servidor corporativo.</b>\nSi el problema persiste, revisa los permisos (Token) de la API.", 
            parse_mode="HTML", 
            reply_to_message_id=reply_to_message_id
        )
        return

    geo = obtener_geodireccion(lat, lon)
    link_maps = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

    # Extraer datos exactos de la respuesta del servidor
    veredicto = res_api.get("verdict", "uncovered")
    mensaje_oficial = res_api.get("message", "Sin información")
    distancia = res_api.get("distance_m", 0)
    condominio = res_api.get("condominium")
    
    # Homologar el diseño de la tarjeta
    if veredicto == "covered":
        badge = "🟢 <b>CON COBERTURA</b>"
    elif veredicto == "border" or (veredicto != "uncovered" and distancia <= 500):
        badge = "🟠 <b>AL BORDE</b>"
    else:
        badge = "🔴 <b>NO APLICA</b>"

    # Preparar el texto del condominio si existe
    alerta_condominio = f"\n🏢 <b>Condominio:</b> {condominio}" if condominio else ""

    tarjeta = (
        f"{badge}\n\n"
        f"📏 <b>Distancia a la red:</b> <b>{round(distancia, 1)} metros</b>"
        f"{alerta_condominio}\n\n"
        f"📍 <b>Ubicación Territorial:</b>\n"
        f"• <b>Provincia:</b> {geo['provincia']}\n"
        f"• <b>Cantón:</b> {geo['canton']}\n"
        f"• <b>Distrito:</b> {geo['distrito']}\n"
        f"• <b>Barrio:</b> {geo['barrio']}\n"
        f"• <b>Coordenadas:</b> <code>{lat:.6f}, {lon:.6f}</code>\n\n"
        f"ℹ️ <b>Diagnóstico del Sistema:</b> {mensaje_oficial}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗺 Ver punto en Google Maps", url=link_maps))
    bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_markup=markup, reply_to_message_id=reply_to_message_id)

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.reply_to(
        message,
        f"👋 ¡Hola, {message.from_user.first_name}!\n\n"
        "<b>Validador Oficial de Cobertura</b> (Conectado a la red central).\n\n"
        "Envía una <b>ubicación GPS</b> o escribe coordenadas (ej: <code>9.953, -84.150</code>).",
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
        if message.chat.type == "private":
            bot.reply_to(message, "⚠️ Formato no reconocido. Envía una ubicación GPS o coordenadas válidas.")

if __name__ == "__main__":
    print("🚀 Validador V3 (API Corporativa) iniciado...")
    bot.infinity_polling(skip_pending=True)
