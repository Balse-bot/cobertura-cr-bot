import re
import requests
import telebot
from telebot import types

# Token de tu bot en Telegram
TOKEN = "8621312939:AAGurzTO0_zfKSXYoHNVFwQnSWDWQOjoKTc"
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)

def dms_a_decimal(grados, minutos, segundos, direccion):
    decimal = float(grados) + (float(minutos) / 60) + (float(segundos) / 3600)
    if direccion.upper() in ['S', 'W', 'O']:  
        decimal *= -1
    return decimal

def consultar_api_oficial(lat, lon):
    url = "https://vendors.data.cr/api/v1/coverage"
    
    payload = {
        "latitude": lat,
        "longitude": lon
    }
    
    # AQUÍ PEGARÁS EL TOKEN NUEVO (reemplaza TU_TOKEN_AQUI)
    token_fresco = "TU_TOKEN_AQUI"
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token_fresco}"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"Error {r.status_code}: {r.text}")
            return None
    except Exception as e:
        print(f"Error de conexión: {e}")
        return None

def responder_consulta(chat_id, lat, lon, reply_to_message_id=None):
    res_api = consultar_api_oficial(lat, lon)
    
    if not res_api:
        bot.send_message(
            chat_id, 
            "⚠️ <b>Error de conexión o Token expirado (401).</b>\nEl servidor corporativo rechazó la consulta.", 
            parse_mode="HTML", 
            reply_to_message_id=reply_to_message_id
        )
        return

    veredicto = res_api.get("verdict", "uncovered")
    mensaje = res_api.get("message", "Sin información")
    distancia = res_api.get("distance_m", 0)
    condominio = res_api.get("condominium")
    
    if veredicto == "covered":
        badge = "🟢 <b>CON COBERTURA</b>"
    elif veredicto == "border" or (veredicto != "uncovered" and distancia <= 500):
        badge = "🟠 <b>AL BORDE</b>"
    else:
        badge = "🔴 <b>NO APLICA</b>"

    alerta_condominio = f"\n🏢 <b>Condominio:</b> {condominio}" if condominio else ""
    link_maps = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

    tarjeta = (
        f"{badge}\n\n"
        f"📏 <b>Distancia a la red oficial:</b> <b>{round(distancia, 1)} metros</b>"
        f"{alerta_condominio}\n\n"
        f"📍 <b>Coordenadas:</b> <code>{lat:.6f}, {lon:.6f}</code>\n\n"
        f"ℹ️ <b>Respuesta del Servidor:</b> {mensaje}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗺 Ver en Google Maps", url=link_maps))
    bot.send_message(chat_id, tarjeta, parse_mode="HTML", reply_markup=markup, reply_to_message_id=reply_to_message_id)

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.reply_to(message, "📡 <b>Validador API Activo</b>. Envía una ubicación.", parse_mode="HTML")

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
    print("🚀 Bot iniciado en modo API...")
    bot.infinity_polling(skip_pending=True)
