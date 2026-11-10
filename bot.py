import os
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.request import HTTPXRequest
from dotenv import load_dotenv

from routing import (
    find_optimal_route, 
    get_stops_nearby,
    get_stop_title,
    get_buses_with_destinations,
    get_route_full_details,
    search_routes_by_prefix
)
from map_renderer import (
    render_stops_map,
    render_full_route_map,
    render_route_map
)
from redis_service import (
    get_cached_route_file_id,
    set_cached_route_file_id,
    push_task
)

# Загружаем переменные окружения
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN_PROD")

# Логирование
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка команды /start."""
    user = update.effective_user
    push_task("start_command", {"user_id": user.id, "username": user.username})
    await update.message.reply_html(
        "Привет! Я помогу тебе найти маршрут в Нови-Саде. 🚌\n\n"
        "📍 <b>Как найти путь:</b>\n"
        "1. Отправь своё местоположение (кнопка 📎 -> Локация).\n"
        "2. Нажми «Это старт 🚩».\n"
        "3. Отправь местоположение финиша.\n\n"
        "🚏 <b>Как посмотреть остановки рядом:</b>\n"
        "Просто отправь локацию и нажми «Показать остановки 🚏».\n\n"
        "🗺 <b>Как посмотреть карту автобуса:</b>\n"
        "Напиши номер (например, <code>9A</code> or <code>11</code>).\n\n"
        "Используй /help для подробного руководства."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Полный мануал к боту."""
    help_text = (
        "📖 <b>Руководство по использованию бота:</b>\n\n"
        "1️⃣ <b>Поиск маршрута (А -> Б):</b>\n"
        "• Отправьте локацию точки отправления.\n"
        "• Нажмите кнопку 🚩 <b>Это старт</b>.\n"
        "• Отправьте локацию точки прибытия.\n"
        "• Бот предложит до 3-х вариантов (прямые или с 1 пересадкой) и покажет карту первого.\n"
        "• Для других вариантов используйте кнопки под картой.\n\n"
        "2️⃣ <b>Просмотр остановок рядом:</b>\n"
        "• Отправьте свою локацию.\n"
        "• Нажмите 🚏 <b>Показать остановки</b>.\n"
        "• Вы получите карту с ближайшими остановками (оранжевые — крупные узлы).\n"
        "• Нажав на кнопку с названием остановки, вы увидите список всех проходящих автобусов и их направления.\n\n"
        "3️⃣ <b>Карты автобусов:</b>\n"
        "• Просто введите номер маршрута (например, <code>7A</code>).\n"
        "• Если вы введете только цифру (например, <code>9</code>), бот предложит список всех литер (9A, 9B и т.д.).\n"
        "• На карте маршрута: 🔴 — конечные, 🟠 — крупные пересадки, 🔵 — обычные остановки.\n\n"
        "4️⃣ <b>Команды:</b>\n"
        "/start - Начать работу\n"
        "/help - Инструкция\n"
        "/about - О проекте"
    )
    await update.message.reply_html(help_text)

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Информация о проекте."""
    about_text = (
        "🚌 <b>RealRoutes Novi Sad</b>\n\n"
        "Данные: <b>JGSP Novi Sad</b>\n"
        "Разработчик: <b>Pavel Katurov</b>\n"
        "Интеллект: <b>Gemini</b>\n\n"
        "Проект создан для удобного планирования поездок на общественном транспорте в Нови-Саде."
    )
    await update.message.reply_html(about_text)

async def process_route_request(update: Update, context: ContextTypes.DEFAULT_TYPE, route_num: str, user_id: int):
    """Общая логика обработки запроса маршрута с использованием Redis file_id кеша."""
    logger.info(f"User {user_id}: Requested route map for {route_num}")
    push_task("route_request", {"user_id": user_id, "route_num": route_num})
    
    if update.message:
        reply_method = update.message.reply_text
        reply_photo_method = update.message.reply_photo
    else:
        reply_method = update.callback_query.message.reply_text
        reply_photo_method = update.callback_query.message.reply_photo

    # 1. Проверяем точное совпадение деталей маршрута
    route_info = await asyncio.to_thread(get_route_full_details, route_num, exact=True)
    
    if not route_info:
        matches = await asyncio.to_thread(search_routes_by_prefix, route_num)
        
        if not matches:
            await reply_method(f"Маршрут '{route_num}' не найден. Попробуй другой, например: 3, 9A, 11B.")
            return
            
        if len(matches) == 1:
            route_info = await asyncio.to_thread(get_route_full_details, matches[0]['num'], exact=True)
        else:
            response_text = f"🔍 <b>Маршруты, начинающиеся на «{route_num}»:</b>\n"
            keyboard = []
            row = []
            
            for m in matches:
                response_text += f"\n• <code>{m['num']}</code>\n   <pre>{m['title']}</pre>\n"
                row.append(InlineKeyboardButton(m['num'], callback_data=f"route:{m['num']}"))
                if len(row) == 4:
                    keyboard.append(row)
                    row = []
            
            if row:
                keyboard.append(row)
                
            await reply_method(
                response_text, 
                reply_markup=InlineKeyboardMarkup(keyboard), 
                parse_mode='HTML'
            )
            return

    if not route_info:
        await reply_method(f"Не удалось получить данные маршрута '{route_num}'. Возможно, у него отсутствует линия на карте.")
        return

    num = route_info['num']
    caption = f"🚌 <b>Маршрут {num}</b>\n{route_info['title']}\n\nНа карте: 🔴 — конечные, 🟠 — пересадки, 🔵 — остановки."

    # 2. Проверяем Redis кеш медиафайлов (file_id)
    cached_file_id = get_cached_route_file_id(num)
    if cached_file_id:
        logger.info(f"Отправка карты маршрута {num} из Telegram file_id кеша (Redis)")
        try:
            await reply_photo_method(cached_file_id, caption=caption, parse_mode='HTML')
            return
        except Exception as e:
            logger.warning(f"Ошибка отправки через file_id кеш (возможно file_id устарел), генерируем заново: {e}")

    # 3. Если в кеше нет — генерируем карту в оперативной памяти (BytesIO)
    msg = await reply_method(f"Рисую карту маршрута {num}... 🎨")
    photo_buf = await asyncio.to_thread(render_full_route_map, route_info)
    
    if photo_buf:
        try:
            sent_msg = await reply_photo_method(photo_buf, caption=caption, parse_mode='HTML')
            if sent_msg and sent_msg.photo:
                new_file_id = sent_msg.photo[-1].file_id
                set_cached_route_file_id(num, new_file_id)
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            await reply_method("Произошла ошибка при отправке карты.")
    else:
        await reply_method("Не удалось сгенерировать карту.")
    
    await msg.delete()

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка текстовых сообщений (номера маршрутов)."""
    text = update.message.text.strip().upper()
    user_id = update.effective_user.id
    await process_route_request(update, context, text, user_id)

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка входящего местоположения."""
    user_id = update.effective_user.id
    lat, lon = update.message.location.latitude, update.message.location.longitude

    if "start_point" not in context.user_data:
        context.user_data["temp_location"] = (lon, lat)
        
        keyboard = [
            [InlineKeyboardButton("Показать остановки 🚏", callback_data="show_stops")],
            [InlineKeyboardButton("Это старт 🚩", callback_data="set_start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📍 Координаты получены. Что сделать?",
            reply_markup=reply_markup
        )
    else:
        start_lon, start_lat = context.user_data.pop("start_point")
        end_lon, end_lat = lon, lat
        logger.info(f"User {user_id}: Searching route from ({start_lat}, {start_lon}) to ({end_lat}, {end_lon})")
        push_task("search_route_ab", {"user_id": user_id, "start": (start_lat, start_lon), "end": (end_lat, end_lon)})
        
        msg = await update.message.reply_text("Ищу лучшие маршруты... 🚌")
        
        routes = await asyncio.to_thread(find_optimal_route, start_lon, start_lat, end_lon, end_lat)
        logger.info(f"User {user_id}: Total routes found: {len(routes)}")
        
        if not routes:
            await msg.edit_text("К сожалению, я не нашел подходящих маршрутов между этими точками.")
            return

        context.user_data["last_routes"] = routes

        response = "<b>Найденные варианты проезда:</b>\n\n"
        keyboard = []
        
        for i, r in enumerate(routes, 1):
            if r['type'] == 'direct':
                response += f"{i}. 🚌 <b>{r['route']}</b> ({r['title']})\n   🏁 {r['start_stop']} → {r['end_stop']} ({r['stops_count']} ост.)\n"
            else:
                response += f"{i}. 🔄 <b>{r['route1']} → {r['route2']}</b>\n   🚌 {r['route1']} ({r['start_stop']}) до <i>{r['transfer_stop']}</i>.\n"
                if r.get('walk_dist', 0) > 10:
                    response += f"   🚶 Пройти {r['walk_dist']:.0f}м до <i>{r['transfer2_stop']}</i>.\n"
                response += f"   🚌 {r['route2']} до <i>{r['end_stop']}</i> ({r['stops_count']} ост.)\n"
            
            response += "------------------------------\n"
            
            if i > 1:
                keyboard.append([InlineKeyboardButton(f"Показать карту варианта №{i} 🗺️", callback_data=f"show_route_map:{i-1}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        photo_buf = await asyncio.to_thread(render_route_map, routes[0])
        if photo_buf:
            try:
                await update.message.reply_photo(
                    photo_buf, 
                    caption=response, 
                    parse_mode='HTML', 
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Ошибка отправки фото: {e}")
                await update.message.reply_html(response, reply_markup=reply_markup)
        else:
            await update.message.reply_html(response, reply_markup=reply_markup)
        
        await msg.delete()

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатий на Inline-кнопки."""
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data

    await query.answer()

    if data == "show_stops":
        if "temp_location" not in context.user_data:
            await query.edit_message_text("Координаты устарели. Пришли локацию еще раз.")
            return
            
        lon, lat = context.user_data["temp_location"]
        stops = await asyncio.to_thread(get_stops_nearby, lon, lat)
        
        if not stops:
            await query.edit_message_text("Рядом остановок не найдено.")
            return
            
        photo_buf = await asyncio.to_thread(render_stops_map, stops, lon, lat)
        
        keyboard = []
        unique_stops = {}
        for s in stops:
            if s['title'] not in unique_stops:
                unique_stops[s['title']] = s['id']
        
        for title, s_id in unique_stops.items():
            keyboard.append([InlineKeyboardButton(title, callback_data=f"stop:{s_id}")])
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if photo_buf:
            await query.message.reply_photo(
                photo_buf, 
                caption="Ближайшие остановки:", 
                reply_markup=reply_markup
            )
        else:
            await query.edit_message_text("Ближайшие остановки:", reply_markup=reply_markup)

    elif data == "set_start":
        if "temp_location" not in context.user_data:
            await query.edit_message_text("Координаты устарели. Пришли локацию еще раз.")
            return
            
        context.user_data["start_point"] = context.user_data.pop("temp_location")
        await query.edit_message_text("Точка СТАРТА принята. Теперь отправь точку ФИНИША (локацией).")

    elif data.startswith("stop:"):
        stop_id = int(data.split(":")[1])
        stop_title = await asyncio.to_thread(get_stop_title, stop_id)
        buses_info = await asyncio.to_thread(get_buses_with_destinations, stop_id)
        
        if not buses_info:
            await query.message.reply_text(f"📍 <b>{stop_title}</b>\nНа этой остановке нет маршрутов.", parse_mode='HTML')
            return

        response_text = f"📍 <b>Остановка: {stop_title}</b>\n\n"
        response_text += "🚌 <b>Список маршрутов:</b>\n"
        
        all_nums = [r['num'] for r in buses_info]
        for bus in buses_info:
            response_text += f"\n• <code>{bus['num']}</code> 🏁 {bus['destination']}\n"
            if bus['route_title']:
                response_text += f"   <pre>{bus['route_title']}</pre>\n"
        
        response_text += "\n👇 <b>Выберите карту:</b>"

        buttons_buses = []
        if len(all_nums) <= 7:
            buttons_buses = all_nums
        else:
            import re
            for b in all_nums:
                match = re.search(r'^\d+([AB])?$', b)
                if match:
                    buttons_buses.append(b)
            
            if not buttons_buses:
                buttons_buses = all_nums

        keyboard = []
        row = []
        for b in buttons_buses:
            row.append(InlineKeyboardButton(b, callback_data=f"route:{b}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        await query.message.reply_text(
            response_text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )

    elif data.startswith("route:"):
        route_num = data.split(":")[1]
        await process_route_request(update, context, route_num, user_id)

    elif data.startswith("show_route_map:"):
        idx = int(data.split(":")[1])
        routes = context.user_data.get("last_routes")
        
        if not routes or idx >= len(routes):
            await query.message.reply_text("Данные о маршрутах устарели. Попробуйте найти путь заново.")
            return
            
        route_data = routes[idx]
        msg = await query.message.reply_text(f"Рисую карту варианта №{idx+1}... 🎨")
        
        photo_buf = await asyncio.to_thread(render_route_map, route_data)
        if photo_buf:
            try:
                caption = f"🗺️ <b>Карта варианта №{idx+1}</b>\n"
                if route_data['type'] == 'direct':
                    caption += f"🚌 Автобус {route_data['route']} ({route_data['title']})"
                else:
                    caption += f"🔄 {route_data['route1']} → {route_data['route2']}"
                    
                await query.message.reply_photo(photo_buf, caption=caption, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка отправки фото: {e}")
        else:
            await query.message.reply_text("Не удалось сгенерировать карту.")
        
        await msg.delete()

def main() -> None:
    """Запуск бота."""
    # Настройка увеличенных таймаутов для больших изображений карт
    request = HTTPXRequest(
        read_timeout=30.0,
        write_timeout=30.0,
        connect_timeout=10.0,
        pool_timeout=5.0,
        media_write_timeout=60.0
    )
    
    application = Application.builder().token(TOKEN).request(request).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling()

if __name__ == "__main__":
    main()
