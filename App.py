import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    JobQueue,
)
from datetime import datetime, timedelta
import pytz
import logging

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройки Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Безопасная загрузка credentials
creds_json = os.getenv("GOOGLE_CREDENTIALS")
if not creds_json:
    raise ValueError("GOOGLE_CREDENTIALS environment variable not set")

creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
client = gspread.authorize(creds)
sheets = {
    "B-11": client.open("GSOM-PLANNER").worksheet("B-11"),
    "B-12": client.open("GSOM-PLANNER").worksheet("B-12"),
    "Users": client.open("GSOM-PLANNER").worksheet("Users")
}


ALLOWED_USERS = {
    1042880639: "B-11",  # Mariia   1062616885   1042880639
    797969195: "B-12"    # Poka chto Ya    1062616885   797969195
}

# Стейты
EDITING_TASK = 1
WAITING_FOR_INPUT = 2

# Языки
LANGUAGES = {
    "ru": "Русский",
    "en": "English"
}

# Часовой пояс Москвы
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Настройки напоминаний
REMINDER_TIME = "18:31"  # Фиксированное время отправки
REMINDER_DAYS_BEFORE = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0]  # За сколько дней напоминать
REMINDER_CHECK_INTERVAL = 5  # Проверять каждые 5 минут (для тестирования)

def convert_to_datetime(time_str, date_str):
    current_year = datetime.now().year
    try:
        if time_str.lower() in ["by schedule", "по расписанию"]:
            time_str = "23:59"
            
        time_parts = time_str.split('-')
        start_time = time_parts[0]
        date_with_year = f"{current_year}-{date_str}"
        dt = datetime.strptime(f"{start_time}-{date_with_year}", '%H:%M-%Y-%d.%m')
        return MOSCOW_TZ.localize(dt)
    except ValueError as e:
        logger.error(f"Ошибка преобразования времени: {e}")
        return None

def main_menu_keyboard(user_lang="ru"):
    keyboard = [
        [InlineKeyboardButton("📋 Посмотреть задания" if user_lang == "ru" else "📋 View tasks", callback_data="get_data")],
        [
            InlineKeyboardButton("➕ Добавить задание" if user_lang == "ru" else "➕ Add task", callback_data="add_task"),
            InlineKeyboardButton("🗑️ Удалить задание" if user_lang == "ru" else "🗑️ Delete task", callback_data="delete_task")
        ],
        [InlineKeyboardButton("👥 Выбор группы" if user_lang == "ru" else "👥 Select group", callback_data="select_group")],
        [InlineKeyboardButton("⚙️ Функционал" if user_lang == "ru" else "⚙️ Features", callback_data="help")],
        [InlineKeyboardButton("↩️ Назад в меню" if user_lang == "ru" else "↩️ Back to menu", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    try:
        users = sheets["Users"].get_all_values()
        user_exists = any(str(user_id) == row[0] for row in users[1:] if len(row) > 0)
    except Exception as e:
        logger.error(f"Ошибка при проверке пользователя: {e}")
        user_exists = False
    
    if not user_exists:
        try:
            sheets["Users"].append_row([user_id, "", True, "ru"])
        except Exception as e:
            logger.error(f"Ошибка при добавлении пользователя: {e}")
    
    user_lang = get_user_language(user_id)
    
    await update.message.reply_text(
        "👋 Привет! Добро пожаловать в *GSOMPASS бот*.\n\nВыберите действие ниже:" if user_lang == "ru" else "👋 Hi! Welcome to *GSOMPASS bot*.\n\nChoose an action below:",
        reply_markup=main_menu_keyboard(user_lang),
        parse_mode='Markdown'
    )

def get_user_language(user_id):
    try:
        users = sheets["Users"].get_all_values()
        user_row = next((row for row in users if len(row) > 0 and str(user_id) == row[0]), None)
        if user_row and len(user_row) > 3:
            return user_row[3] if user_row[3] in LANGUAGES else "ru"
    except Exception as e:
        logger.error(f"Error getting user language: {e}")
    return "ru"

def get_user_reminders_enabled(user_id):
    try:
        users = sheets["Users"].get_all_values()
        user_row = next((row for row in users if len(row) > 0 and str(user_id) == row[0]), None)
        if user_row and len(user_row) > 2:
            return user_row[2].lower() == 'true'
    except Exception as e:
        logger.error(f"Error getting user reminders status: {e}")
    return True

async def callback_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_lang = get_user_language(query.from_user.id)
    
    await query.edit_message_text(
        "👋 Вы вернулись в главное меню. Выберите действие:" if user_lang == "ru" else "👋 You're back to the main menu. Choose an action:",
        reply_markup=main_menu_keyboard(user_lang)
    )

async def callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_lang = get_user_language(query.from_user.id)
    
    keyboard = [
        [InlineKeyboardButton("🔔 Настройки напоминаний" if user_lang == "ru" else "🔔 Reminder settings", callback_data="reminder_settings")],
        [InlineKeyboardButton("🌐 Изменить язык" if user_lang == "ru" else "🌐 Change language", callback_data="language_settings")],
        [InlineKeyboardButton("↩️ Назад в меню" if user_lang == "ru" else "↩️ Back to menu", callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        "📌 Возможности бота:\n\n"
        "• 📋 Посмотреть задания своей группы\n"
        "• ➕ Добавить задание (для кураторов)\n"
        "• 🗑️ Удалить задание (для кураторов)\n"
        "• 🗓️ Данные берутся из Google Таблицы\n"
        "• 🔔 Напоминания о заданиях (в 09:00 по МСК)\n"
        "• 👥 Выбор/изменение группы\n"
        "• 🔒 Доступ к изменению только у доверенных пользователей" if user_lang == "ru" else 
        "📌 Bot features:\n\n"
        "• 📋 View tasks for your group\n"
        "• ➕ Add task (for curators)\n"
        "• 🗑️ Delete task (for curators)\n"
        "• 🗓️ Data is taken from Google Sheets\n"
        "• 🔔 Task reminders (at 09:00 MSK)\n"
        "• 👥 Select/change group\n"
        "• 🔒 Only trusted users can make changes",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_tasks_for_group(query, group, show_delete_buttons=False):
    sheet = sheets[group]
    try:
        all_values = sheet.get_all_values()
        data = all_values[1:] if len(all_values) > 1 else []
        
        user_lang = get_user_language(query.from_user.id)
        response = f"📌 Задания для группы {group}:\n" if user_lang == "ru" else f"📌 Tasks for group {group}:\n"
        count = 0
        tasks = []

        for idx, row in enumerate(data, start=2):
            if len(row) >= 7 and row[6] == group:
                try:
                    deadline = convert_to_datetime(row[5], row[4])
                    if deadline:
                        tasks.append((deadline, row, idx))
                except Exception as e:
                    logger.error(f"Ошибка при обработке задания: {e}")
                    continue

        tasks.sort(key=lambda x: x[0])

        keyboard = []
        for deadline, row, row_idx in tasks:
            if deadline > datetime.now(MOSCOW_TZ):
                count += 1
                time_display = "By schedule" if row[5] in ["23:59", "By schedule", "По расписанию"] else row[5]
                response += (
                    f"\n🔹 *{row[0]}* — {row[1]} "
                    f"({row[2]})\n"
                    f"🗓 Дата: {row[4]} | Время: {time_display} | Баллы: {row[3]}\n" if user_lang == "ru" else 
                    f"\n🔹 *{row[0]}* — {row[1]} "
                    f"({row[2]})\n"
                    f"🗓 Date: {row[4]} | Time: {time_display} | Points: {row[3]}\n"
                )
                
                if show_delete_buttons:
                    keyboard.append([InlineKeyboardButton(
                        f"🗑️ Удалить: {row[0]} ({row[4]})" if user_lang == "ru" else f"🗑️ Delete: {row[0]} ({row[4]})",
                        callback_data=f"delete_{group}_{row_idx}"
                    )])

        if count == 0:
            response = "ℹ️ Пока нет заданий для вашей группы." if user_lang == "ru" else "ℹ️ No tasks for your group yet."

        if show_delete_buttons:
            keyboard.append([InlineKeyboardButton("↩️ Назад" if user_lang == "ru" else "↩️ Back", callback_data="back_to_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
        else:
            reply_markup = main_menu_keyboard(user_lang)

        await query.edit_message_text(response, parse_mode='Markdown', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка при получении заданий: {e}")
        user_lang = get_user_language(query.from_user.id)
        await query.edit_message_text(
            f"⛔ Ошибка при получении заданий: {str(e)}" if user_lang == "ru" else f"⛔ Error getting tasks: {str(e)}",
            reply_markup=main_menu_keyboard(user_lang))

async def callback_get_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        users = sheets["Users"].get_all_values()
        user_row = next((row for row in users if len(row) > 0 and str(user_id) == row[0]), None)
        group = user_row[1] if user_row and len(user_row) > 1 and user_row[1] in sheets else None
    except Exception as e:
        logger.error(f"Ошибка при получении группы пользователя: {e}")
        group = None

    if not group and user_id in ALLOWED_USERS:
        group = ALLOWED_USERS[user_id]
        try:
            if user_row:
                sheets["Users"].update_cell(users.index(user_row) + 1, 2, group)
            else:
                sheets["Users"].append_row([user_id, group, False, "ru"])
        except Exception as e:
            logger.error(f"Ошибка при обновлении группы пользователя: {e}")

    if group:
        await show_tasks_for_group(query, group)
    else:
        await callback_select_group(update, context)

async def callback_select_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    
    user_lang = get_user_language(query.from_user.id if query else update.effective_user.id)
    
    group_keyboard = [
        [InlineKeyboardButton("B-11", callback_data="set_group_B-11"),
         InlineKeyboardButton("B-12", callback_data="set_group_B-12")],
        [InlineKeyboardButton("↩️ Назад в меню" if user_lang == "ru" else "↩️ Back to menu", callback_data="back_to_menu")]
    ]
    
    text = "👥 Выберите вашу группу:" if user_lang == "ru" else "👥 Select your group:"
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(group_keyboard))
    else:
        await context.bot.send_message(
            update.effective_chat.id,
            text,
            reply_markup=InlineKeyboardMarkup(group_keyboard)
        )

async def set_user_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    group = query.data.replace("set_group_", "")
    
    try:
        users = sheets["Users"].get_all_values()
        user_row = next((i for i, row in enumerate(users) if len(row) > 0 and str(user_id) == row[0]), None)
        
        if user_row is None:
            sheets["Users"].append_row([user_id, group, False, "ru"])
        else:
            sheets["Users"].update_cell(user_row + 1, 2, group)
        
        user_lang = get_user_language(user_id)
        await query.edit_message_text(
            f"✅ Ваша группа установлена: {group}" if user_lang == "ru" else f"✅ Your group is set: {group}",
            reply_markup=main_menu_keyboard(user_lang))
        
        if user_row is not None and len(users[user_row]) > 2 and users[user_row][2].lower() == 'true':
            await schedule_reminders_for_user(context.application.job_queue, user_id)
            
    except Exception as e:
        logger.error(f"Ошибка при установке группы: {e}")
        user_lang = get_user_language(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при установке группы." if user_lang == "ru" else "⛔ An error occurred while setting the group.",
            reply_markup=main_menu_keyboard(user_lang))

def generate_edit_task_keyboard(user_lang="ru"):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✍️ Предмет" if user_lang == "ru" else "✍️ Subject", callback_data="edit_subject"),
            InlineKeyboardButton("📘 Тип задания" if user_lang == "ru" else "📘 Task type", callback_data="edit_task_type")
        ],
        [
            InlineKeyboardButton("💯 Баллы" if user_lang == "ru" else "💯 Points", callback_data="edit_max_points"),
            InlineKeyboardButton("🗓️ Дата" if user_lang == "ru" else "🗓️ Date", callback_data="edit_date")
        ],
        [
            InlineKeyboardButton("⏰ Время" if user_lang == "ru" else "⏰ Time", callback_data="edit_time"),
            InlineKeyboardButton("📍 Формат" if user_lang == "ru" else "📍 Format", callback_data="edit_format")
        ],
        [
            InlineKeyboardButton("✅ Сохранить" if user_lang == "ru" else "✅ Save", callback_data="save_task"),
            InlineKeyboardButton("❌ Отменить" if user_lang == "ru" else "❌ Cancel", callback_data="cancel_task")
        ]
    ])

def generate_subject_keyboard(user_lang="ru"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Maths", callback_data="Maths"),
         InlineKeyboardButton("Management", callback_data="Management")],
        [InlineKeyboardButton("DigTools", callback_data="DigTools"),
         InlineKeyboardButton("FinAcc", callback_data="FinAcc")],
        [InlineKeyboardButton("Microeconomics", callback_data="Microeconomics"),
         InlineKeyboardButton("Другое" if user_lang == "ru" else "Other", callback_data="other_subject")],
        [InlineKeyboardButton("↩️ Назад к редактированию" if user_lang == "ru" else "↩️ Back to editing", callback_data="back_to_editing")]
    ])

def generate_task_type_keyboard(user_lang="ru"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Test", callback_data="Test"),
         InlineKeyboardButton("HW", callback_data="HW")],
        [InlineKeyboardButton("MidTerm", callback_data="MidTerm"),
         InlineKeyboardButton("FinalTest", callback_data="FinalTest")],
        [InlineKeyboardButton("Другое" if user_lang == "ru" else "Other", callback_data="other_task_type")],
        [InlineKeyboardButton("↩️ Назад к редактированию" if user_lang == "ru" else "↩️ Back to editing", callback_data="back_to_editing")]
    ])

def generate_points_keyboard(user_lang="ru"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5", callback_data="points_5"),
         InlineKeyboardButton("10", callback_data="points_10")],
        [InlineKeyboardButton("15", callback_data="points_15"),
         InlineKeyboardButton("20", callback_data="points_20")],
        [InlineKeyboardButton("Другое" if user_lang == "ru" else "Other", callback_data="other_max_points")],
        [InlineKeyboardButton("↩️ Назад к редактированию" if user_lang == "ru" else "↩️ Back to editing", callback_data="back_to_editing")]
    ])

def generate_time_keyboard(user_lang="ru"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("10:00", callback_data="time_10:00"),
         InlineKeyboardButton("11:45", callback_data="time_11:45")],
        [InlineKeyboardButton("14:15", callback_data="time_14:15"),
         InlineKeyboardButton("16:00", callback_data="time_16:00")],
        [InlineKeyboardButton("17:45", callback_data="time_17:45"),
         InlineKeyboardButton("19:30", callback_data="time_19:30")],
        [InlineKeyboardButton("23:59", callback_data="time_23:59"),
         InlineKeyboardButton("By schedule" if user_lang == "en" else "По расписанию", callback_data="time_schedule")],
        [InlineKeyboardButton("↩️ Назад к редактированию" if user_lang == "ru" else "↩️ Back to editing", callback_data="back_to_editing")]
    ])

def generate_date_buttons(user_lang="ru"):
    today = datetime.now(MOSCOW_TZ)
    buttons = []
    row_buttons = []
    
    for i in range(28):
        date = today + timedelta(days=i+1)
        date_str = date.strftime("%d.%m")
        day_name = date.strftime("%a")
        
        btn_text = f"{date_str} ({day_name})"
        row_buttons.append(InlineKeyboardButton(btn_text, callback_data=date_str))
        
        if len(row_buttons) == 4 or i == 27:
            buttons.append(row_buttons)
            row_buttons = []
    
    buttons.append([InlineKeyboardButton("✏️ Ввести свою дату" if user_lang == "ru" else "✏️ Enter custom date", callback_data="custom_date")])
    buttons.append([InlineKeyboardButton("↩️ Назад к редактированию" if user_lang == "ru" else "↩️ Back to editing", callback_data="back_to_editing")])
    
    return InlineKeyboardMarkup(buttons)

def generate_format_keyboard(user_lang="ru"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Online", callback_data="Online"),
         InlineKeyboardButton("Offline - MD", callback_data="Offline - MD")],
        [InlineKeyboardButton("↩️ Назад к редактированию" if user_lang == "ru" else "↩️ Back to editing", callback_data="back_to_editing")]
    ])

async def format_task_message(context):
    task_data = context.user_data.get("task_data", {})
    user_lang = get_user_language(context._user_id) if hasattr(context, '_user_id') else "ru"
    
    message = "📝 Редактирование задания:\n\n" if user_lang == "ru" else "📝 Editing task:\n\n"
    message += f"🔹 <b>Предмет:</b> {task_data.get('subject', 'не выбрано' if user_lang == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Тип задания:</b> {task_data.get('task_type', 'не выбрано' if user_lang == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Макс. баллы:</b> {task_data.get('max_points', 'не выбрано' if user_lang == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Дата:</b> {task_data.get('date', 'не выбрана' if user_lang == 'ru' else 'not selected')}\n"
    
    time_display = task_data.get('time', 'не выбрано' if user_lang == 'ru' else 'not selected')
    if time_display == "23:59":
        time_display = "By schedule" if user_lang == "en" else "По расписанию"
    elif time_display == "time_schedule":
        time_display = "By schedule" if user_lang == "en" else "По расписанию"
    message += f"🔹 <b>Время:</b> {time_display}\n"
    
    message += f"🔹 <b>Формат:</b> {task_data.get('format', 'не выбран' if user_lang == 'ru' else 'not selected')}\n\n"
    message += "Выберите параметр для изменения или сохраните задание:" if user_lang == "ru" else "Select a parameter to change or save the task:"
    return message

async def callback_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_lang = get_user_language(user_id)

    if user_id not in ALLOWED_USERS:
        await query.edit_message_text(
            "⛔ У вас нет доступа к добавлению заданий." if user_lang == "ru" else "⛔ You don't have access to add tasks.",
            reply_markup=main_menu_keyboard(user_lang))
        return ConversationHandler.END

    context.user_data["task_data"] = {
        "group": ALLOWED_USERS[user_id],
        "subject": "не выбрано" if user_lang == "ru" else "not selected",
        "task_type": "не выбрано" if user_lang == "ru" else "not selected",
        "max_points": "не выбрано" if user_lang == "ru" else "not selected",
        "date": "не выбрана" if user_lang == "ru" else "not selected",
        "time": "не выбрано" if user_lang == "ru" else "not selected",
        "format": "не выбран" if user_lang == "ru" else "not selected"
    }

    message = await format_task_message(context)
    await query.edit_message_text(
        message,
        reply_markup=generate_edit_task_keyboard(user_lang),
        parse_mode='HTML'
    )
    return EDITING_TASK

async def edit_task_parameter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_lang = get_user_language(query.from_user.id)
    
    if query.data == "edit_subject":
        await query.edit_message_text(
            "✍️ Выберите предмет:" if user_lang == "ru" else "✍️ Select subject:",
            reply_markup=generate_subject_keyboard(user_lang)
        )
    elif query.data == "edit_task_type":
        await query.edit_message_text(
            "📘 Выберите тип задания:" if user_lang == "ru" else "📘 Select task type:",
            reply_markup=generate_task_type_keyboard(user_lang)
        )
    elif query.data == "edit_max_points":
        await query.edit_message_text(
            "💯 Выберите максимальное количество баллов:" if user_lang == "ru" else "💯 Select maximum points:",
            reply_markup=generate_points_keyboard(user_lang)
        )
    elif query.data == "edit_date":
        await query.edit_message_text(
            "🗓️ Выберите дату:" if user_lang == "ru" else "🗓️ Select date:",
            reply_markup=generate_date_buttons(user_lang)
        )
    elif query.data == "edit_time":
        await query.edit_message_text(
            "⏰ Выберите время:" if user_lang == "ru" else "⏰ Select time:",
            reply_markup=generate_time_keyboard(user_lang)
        )
    elif query.data == "edit_format":
        await query.edit_message_text(
            "📍 Выберите формат:" if user_lang == "ru" else "📍 Select format:",
            reply_markup=generate_format_keyboard(user_lang)
        )
    elif query.data == "back_to_editing":
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_lang),
            parse_mode='HTML'
        )
    elif query.data.startswith(("Maths", "Management", "DigTools", "FinAcc", "Microeconomics")):
        context.user_data["task_data"]["subject"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_lang),
            parse_mode='HTML'
        )
    elif query.data.startswith(("Test", "HW", "MidTerm", "FinalTest")):
        context.user_data["task_data"]["task_type"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_lang),
            parse_mode='HTML'
        )
    elif query.data.startswith("points_"):
        points_value = query.data[7:]
        context.user_data["task_data"]["max_points"] = points_value
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_lang),
            parse_mode='HTML'
        )
    elif len(query.data.split('.')) == 2 and query.data.count('.') == 1:
        context.user_data["task_data"]["date"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_lang),
            parse_mode='HTML'
        )
    elif query.data.startswith("time_"):
        time_value = query.data[5:]
        if time_value == "schedule":
            time_value = "23:59"
        context.user_data["task_data"]["time"] = time_value
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_lang),
            parse_mode='HTML'
        )
    elif query.data in ["Online", "Offline - MD"]:
        context.user_data["task_data"]["format"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_lang),
            parse_mode='HTML'
        )
    elif query.data == "other_subject":
        await query.edit_message_text("✍️ Введите название предмета:" if user_lang == "ru" else "✍️ Enter subject name:")
        context.user_data["waiting_for"] = "subject"
        return WAITING_FOR_INPUT
    elif query.data == "other_task_type":
        await query.edit_message_text("📘 Введите тип задания:" if user_lang == "ru" else "📘 Enter task type:")
        context.user_data["waiting_for"] = "task_type"
        return WAITING_FOR_INPUT
    elif query.data == "other_max_points":
        await query.edit_message_text("💯 Введите количество баллов:" if user_lang == "ru" else "💯 Enter points:")
        context.user_data["waiting_for"] = "max_points"
        return WAITING_FOR_INPUT
    elif query.data == "custom_date":
        await query.edit_message_text("🗓️ Введите дату в формате ДД.ММ (например, 15.12):" if user_lang == "ru" else "🗓️ Enter date in DD.MM format (e.g., 15.12):")
        context.user_data["waiting_for"] = "date"
        return WAITING_FOR_INPUT
    elif query.data == "save_task":
        task_data = context.user_data.get("task_data", {})
        if (task_data["subject"] == ("не выбрано" if user_lang == "ru" else "not selected") or 
            task_data["task_type"] == ("не выбрано" if user_lang == "ru" else "not selected") or 
            task_data["max_points"] == ("не выбрано" if user_lang == "ru" else "not selected") or 
            task_data["date"] == ("не выбрана" if user_lang == "ru" else "not selected") or 
            task_data["time"] == ("не выбрано" if user_lang == "ru" else "not selected") or 
            task_data["format"] == ("не выбран" if user_lang == "ru" else "not selected")):
            
            await query.answer(
                "⚠️ Заполните все поля перед сохранением!" if user_lang == "ru" else "⚠️ Fill all fields before saving!",
                show_alert=True)
            return EDITING_TASK
        
        group = task_data["group"]
        sheet = sheets[group]
        
        try:
            row_data = [
                task_data["subject"],
                task_data["task_type"],
                task_data["format"],
                task_data["max_points"],
                task_data["date"],
                task_data["time"],
                group
            ]
            
            sheet.append_row(row_data)
            context.user_data.clear()
            
            # Обновляем напоминания для всех пользователей группы
            await refresh_reminders_for_group(context.application.job_queue, group)
            
            await query.edit_message_text(
                "✅ Задание успешно добавлено!" if user_lang == "ru" else "✅ Task added successfully!",
                reply_markup=main_menu_keyboard(user_lang))
        except Exception as e:
            logger.error(f"Ошибка при сохранении задания: {e}")
            await query.edit_message_text(
                f"⛔ Произошла ошибка при сохранении: {str(e)}" if user_lang == "ru" else f"⛔ Error saving: {str(e)}",
                reply_markup=main_menu_keyboard(user_lang))
        return ConversationHandler.END
    elif query.data == "cancel_task":
        context.user_data.clear()
        await query.edit_message_text(
            "🚫 Добавление задания отменено." if user_lang == "ru" else "🚫 Task addition canceled.",
            reply_markup=main_menu_keyboard(user_lang))
        return ConversationHandler.END
    
    return EDITING_TASK

async def handle_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    waiting_for = context.user_data.get("waiting_for")
    user_lang = get_user_language(update.effective_user.id)
    
    if waiting_for == "subject":
        context.user_data["task_data"]["subject"] = user_input
    elif waiting_for == "task_type":
        context.user_data["task_data"]["task_type"] = user_input
    elif waiting_for == "max_points":
        context.user_data["task_data"]["max_points"] = user_input
    elif waiting_for == "date":
        try:
            day, month = user_input.split('.')
            if len(day) == 2 and len(month) == 2 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                context.user_data["task_data"]["date"] = user_input
            else:
                await update.message.reply_text(
                    "⚠️ Неверный формат даты. Введите дату в формате ДД.ММ (например, 15.12)" if user_lang == "ru" else 
                    "⚠️ Wrong date format. Enter date in DD.MM format (e.g., 15.12)")
                return WAITING_FOR_INPUT
        except:
            await update.message.reply_text(
                "⚠️ Неверный формат даты. Введите дату в формате ДД.ММ (например, 15.12)" if user_lang == "ru" else 
                "⚠️ Wrong date format. Enter date in DD.MM format (e.g., 15.12)")
            return WAITING_FOR_INPUT
    
    del context.user_data["waiting_for"]
    
    message = await format_task_message(context)
    await update.message.reply_text(
        message,
        reply_markup=generate_edit_task_keyboard(user_lang),
        parse_mode='HTML'
    )
    return EDITING_TASK

async def callback_delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_lang = get_user_language(user_id)

    if user_id not in ALLOWED_USERS:
        await query.edit_message_text(
            "⛔ У вас нет доступа к удалению заданий." if user_lang == "ru" else "⛔ You don't have access to delete tasks.",
            reply_markup=main_menu_keyboard(user_lang))
        return ConversationHandler.END

    group = ALLOWED_USERS[user_id]
    await show_tasks_for_group(query, group, show_delete_buttons=True)
    return EDITING_TASK

async def handle_task_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_lang = get_user_language(query.from_user.id)
    
    if query.data == "back_to_menu":
        await callback_back_to_menu(update, context)
        return ConversationHandler.END
    
    if query.data.startswith("delete_"):
        try:
            _, group, row_idx = query.data.split("_")
            row_idx = int(row_idx)
            sheet = sheets[group]
            
            all_values = sheet.get_all_values()
            if row_idx <= len(all_values):
                sheet.delete_rows(row_idx)
                await query.edit_message_text(
                    "✅ Задание успешно удалено!" if user_lang == "ru" else "✅ Task deleted successfully!",
                    reply_markup=main_menu_keyboard(user_lang))
                
                # Обновляем напоминания для всех пользователей группы
                await refresh_reminders_for_group(context.application.job_queue, group)
            else:
                await query.edit_message_text(
                    "⛔ Задание уже было удалено" if user_lang == "ru" else "⛔ Task was already deleted",
                    reply_markup=main_menu_keyboard(user_lang))
        except Exception as e:
            logger.error(f"Ошибка при удалении задания: {e}")
            await query.edit_message_text(
                f"⛔ Ошибка при удалении: {str(e)}" if user_lang == "ru" else f"⛔ Error deleting: {str(e)}",
                reply_markup=main_menu_keyboard(user_lang))
    
    return ConversationHandler.END

# ========== IMPROVED REMINDER SYSTEM ==========
async def callback_reminder_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_lang = get_user_language(user_id)
    
    try:
        reminders_enabled = get_user_reminders_enabled(user_id)
        
        keyboard = [
            [InlineKeyboardButton(
                "🔔 Напоминания: ВКЛ" if reminders_enabled else "🔔 Напоминания: ВЫКЛ",
                callback_data="toggle_reminders")],
            [InlineKeyboardButton(
                "↩️ Назад в меню" if user_lang == "ru" else "↩️ Back to menu",
                callback_data="back_to_menu")]
        ]
        
        await query.edit_message_text(
            "🔔 Настройки напоминаний:\n\n"
            "Напоминания приходят каждый день в 09:00 по МСК на :\n"
            "10 дней вперед и в день задания" if user_lang == "ru" else 
            "🔔 Reminder settings:\n\n"
            "Reminders are sent daily at 09:00 MSK for:\n"
            "10 days before and on the task day.",
            reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Ошибка в callback_reminder_settings: {e}")
        await query.edit_message_text(
            "⛔ Произошла ошибка при получении настроек." if user_lang == "ru" else "⛔ Error getting settings.",
            reply_markup=main_menu_keyboard(user_lang))

async def toggle_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_lang = get_user_language(user_id)
    
    try:
        users = sheets["Users"].get_all_values()
        user_row = next((i for i, row in enumerate(users) if len(row) > 0 and str(user_id) == row[0]), None)
        
        if user_row is None:
            # Создаем нового пользователя с включенными напоминаниями
            sheets["Users"].append_row([user_id, "", True, "ru"])
            new_state = True
        else:
            # Переключаем состояние
            current_state = len(users[user_row]) > 2 and users[user_row][2].lower() == 'true'
            new_state = not current_state
            sheets["Users"].update_cell(user_row + 1, 3, str(new_state))
        
        # Обновляем напоминания
        await schedule_reminders_for_user(context.application.job_queue, user_id)
        
        # Возвращаем пользователю сообщение о новом состоянии
        await query.edit_message_text(
            f"✅ Напоминания {'включены' if new_state else 'выключены'}!" if user_lang == "ru" else f"✅ Reminders {'enabled' if new_state else 'disabled'}!",
            reply_markup=main_menu_keyboard(user_lang))
    except Exception as e:
        logger.error(f"Ошибка в toggle_reminders: {e}")
        await query.edit_message_text(
            "⛔ Произошла ошибка при изменении настроек." if user_lang == "ru" else "⛔ Error changing settings.",
            reply_markup=main_menu_keyboard(user_lang))

async def test_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_lang = get_user_language(user_id)
    
    try:
        # Создаем тестовое напоминание
        test_data = {
            'subject': "Test Subject",
            'task_type': "Test Task",
            'date': datetime.now(MOSCOW_TZ).strftime("%d.%m"),
            'time': "10:00",
            'days_left': 1,
            'max_points': "10",
            'format': "Online"
        }
        
        context.job_queue.run_once(
            send_reminder,
            5,  # Через 5 секунд
            chat_id=user_id,
            data=test_data,
            name=f"test_reminder_{user_id}"
        )
        
        await query.edit_message_text(
            "🔔 Тестовое напоминание будет отправлено через 5 секунд!" if user_lang == "ru" else "🔔 Test reminder will be sent in 5 seconds!",
            reply_markup=main_menu_keyboard(user_lang))
    except Exception as e:
        logger.error(f"Ошибка в test_reminder: {e}")
        await query.edit_message_text(
            "⛔ Произошла ошибка при отправке тестового напоминания." if user_lang == "ru" else "⛔ Error sending test reminder.",
            reply_markup=main_menu_keyboard(user_lang))

async def schedule_reminders_for_user(job_queue: JobQueue, user_id: int):
    """Запланировать напоминания для пользователя"""
    try:
        # Удаляем все старые напоминания для этого пользователя
        for job in job_queue.jobs():
            if job.name and str(user_id) in job.name and not job.name.startswith("test_"):
                job.schedule_removal()

        # Проверяем, включены ли напоминания у пользователя
        if not get_user_reminders_enabled(user_id):
            return

        # Получаем группу пользователя
        users = sheets["Users"].get_all_values()
        user_row = next((row for row in users if len(row) > 0 and str(user_id) == row[0]), None)
        group = user_row[1] if user_row and len(user_row) > 1 and user_row[1] in sheets else None
        if not group:
            return

        # Получаем все задания для группы
        sheet = sheets[group]
        all_values = sheet.get_all_values()
        data = all_values[1:] if len(all_values) > 1 else []
        
        now = datetime.now(MOSCOW_TZ)
        today = now.date()
        
        # Группируем задания по дням до дедлайна
        reminders_by_day = {}
        
        for row in data:
            if len(row) >= 7 and row[6] == group:
                try:
                    deadline = convert_to_datetime(row[5], row[4])
                    if not deadline or deadline.date() < today:
                        continue  # Пропускаем просроченные задания
                    
                    # Создаем напоминания для каждого дня из REMINDER_DAYS_BEFORE
                    for days_before in REMINDER_DAYS_BEFORE:
                        reminder_date = (deadline.date() - timedelta(days=days_before))
                        
                        # Проверяем, что дата напоминания еще не прошла
                        if reminder_date >= today:
                            # Устанавливаем время напоминания (09:05 по МСК)
                            reminder_time = datetime.combine(
                                reminder_date,
                                datetime.strptime(REMINDER_TIME, "%H:%M").time()
                            )
                            reminder_time = MOSCOW_TZ.localize(reminder_time)
                            
                            # Добавляем задание в соответствующую дату
                            if reminder_date not in reminders_by_day:
                                reminders_by_day[reminder_date] = []
                            
                            reminders_by_day[reminder_date].append({
                                'subject': row[0],
                                'task_type': row[1],
                                'date': row[4],
                                'time': row[5],
                                'days_left': days_before,
                                'max_points': row[3],
                                'format': row[2]
                            })
                except Exception as e:
                    logger.error(f"Ошибка при создании напоминания: {e}")
                    continue
        
        # Создаем задания для отправки группированных напоминаний
        for reminder_date, tasks in reminders_by_day.items():
            # Сортируем задания по дате дедлайна (ближайшие сначала)
            tasks.sort(key=lambda x: convert_to_datetime(x['time'], x['date']))
            
            # Устанавливаем время напоминания (09:05 по МСК)
            reminder_time = datetime.combine(
                reminder_date,
                datetime.strptime(REMINDER_TIME, "%H:%M").time()
            )
            reminder_time = MOSCOW_TZ.localize(reminder_time)
            
            # Уникальное имя для job
            job_name = f"reminder_{user_id}_{reminder_date.strftime('%Y%m%d')}"
            
            job_queue.run_once(
                send_grouped_reminders,
                reminder_time,
                chat_id=user_id,
                data={'tasks': tasks},
                name=job_name
            )
    except Exception as e:
        logger.error(f"Ошибка в schedule_reminders_for_user: {e}")

async def send_grouped_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Отправить группированные напоминания пользователю"""
    job = context.job
    tasks = job.data['tasks']
    user_id = job.chat_id
    user_lang = get_user_language(user_id)
    
    if not tasks:
        return
    
    # Группируем задачи по дням до дедлайна
    tasks_by_days = {}
    for task in tasks:
        if task['days_left'] not in tasks_by_days:
            tasks_by_days[task['days_left']] = []
        tasks_by_days[task['days_left']].append(task)
    
    # Сортируем дни по возрастанию (ближайшие сначала)
    sorted_days = sorted(tasks_by_days.keys())
    
    # Создаем основное сообщение
    message = "🔔 *UPCOMING DEADLINES*\n\n" if user_lang == "en" else "🔔 *ПРЕДСТОЯЩИЕ ДЕДЛАЙНЫ*\n\n"
    
    for days_left in sorted_days:
        # Добавляем заголовок для группы задач
        if days_left == 0:
            message += "*TODAY*\n" if user_lang == "en" else "*СЕГОДНЯ*\n"
        elif days_left == 1:
            message += "*TOMORROW*\n" if user_lang == "en" else "*ЗАВТРА*\n"
        else:
            days_text = {
                3: "3 days" if user_lang == "en" else "3 дня",
                7: "7 days" if user_lang == "en" else "7 дней",
                10: "10 days" if user_lang == "en" else "10 дней"
            }.get(days_left, f"{days_left} days" if user_lang == "en" else f"{days_left} дней")
            message += f"*IN {days_text.upper()}*\n" if user_lang == "en" else f"*ЧЕРЕЗ {days_text.upper()}*\n"
        
        # Добавляем задачи для этой группы
        for task in tasks_by_days[days_left]:
            time_display = "By schedule" if task['time'] in ["23:59", "By schedule", "По расписанию"] else task['time']
            
            message += (
                f"📌 *{task['subject']}* — {task['task_type']}\n"
                f"🗓 {task['date']} | ⏰ {time_display} | 🏷 {task['format']} | 💯 {task['max_points']}\n\n" if user_lang == "en" else
                f"📌 *{task['subject']}* — {task['task_type']}\n"
                f"🗓 {task['date']} | ⏰ {time_display} | 🏷 {task['format']} | 💯 {task['max_points']}\n\n"
            )
    
    # Добавляем общий совет в зависимости от наличия срочных задач
    if 0 in tasks_by_days:
        message += "❗ Urgent: Some tasks are due TODAY!" if user_lang == "en" else "❗ Срочно: Некоторые задания должны быть выполнены СЕГОДНЯ!"
    elif 1 in tasks_by_days:
        message += "❗ Reminder: Tasks due tomorrow!" if user_lang == "en" else "❗ Напоминание: Задания на завтра!"
    else:
        message += "❗ Plan ahead for upcoming deadlines." if user_lang == "en" else "❗ Запланируйте выполнение предстоящих заданий."
    
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке напоминания пользователю {user_id}: {e}")

async def refresh_reminders_for_group(job_queue: JobQueue, group: str):
    """Обновить напоминания для всех пользователей группы"""
    try:
        users = sheets["Users"].get_all_values()
        for row in users[1:]:  # Пропускаем заголовок
            if len(row) > 1 and row[1] == group and len(row) > 2 and row[2].lower() == 'true':
                user_id = int(row[0])
                await schedule_reminders_for_user(job_queue, user_id)
    except Exception as e:
        logger.error(f"Ошибка в refresh_reminders_for_group: {e}")

async def check_reminders_now(context: ContextTypes.DEFAULT_TYPE):
    """Проверить и отправить напоминания прямо сейчас"""
    try:
        now = datetime.now(MOSCOW_TZ)
        current_time = now.time()
        
        # Проверяем только с 00:00 до 08:55
        if current_time >= datetime.strptime("00:00", "%H:%M").time() and \
           current_time <= datetime.strptime("23:55", "%H:%M").time():
            
            users = sheets["Users"].get_all_values()
            for row in users[1:]:  # Пропускаем заголовок
                if len(row) > 2 and row[2].lower() == 'true':  # Если напоминания включены
                    user_id = int(row[0])
                    await schedule_reminders_for_user(context.job_queue, user_id)
    except Exception as e:
        logger.error(f"Ошибка в check_reminders_now: {e}")

async def callback_language_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_lang = get_user_language(user_id)
    
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="set_lang_ru")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en")],
        [InlineKeyboardButton("↩️ Назад в меню" if user_lang == "ru" else "↩️ Back to menu", callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        "🌐 Выберите язык:" if user_lang == "ru" else "🌐 Select language:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def set_user_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("set_lang_", "")
    
    try:
        users = sheets["Users"].get_all_values()
        user_row = next((i for i, row in enumerate(users) if len(row) > 0 and str(user_id) == row[0]), None)
        
        if user_row is None:
            sheets["Users"].append_row([user_id, "", False, lang])
        else:
            if len(users[user_row]) < 4:
                sheets["Users"].update_cell(user_row + 1, 4, lang)
            else:
                sheets["Users"].update_cell(user_row + 1, 4, lang)
        
        new_lang = get_user_language(user_id)
        
        await query.edit_message_text(
            "✅ Язык изменен на русский!" if new_lang == "ru" else "✅ Language changed to English!",
            reply_markup=main_menu_keyboard(new_lang))
    except Exception as e:
        logger.error(f"Ошибка при изменении языка: {e}")
        current_lang = get_user_language(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при изменении языка." if current_lang == "ru" else "⛔ Error changing language.",
            reply_markup=main_menu_keyboard(current_lang))

def main():
    # Загрузка токена бота
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Токен бота не найден! Проверьте переменную TELEGRAM_BOT_TOKEN в Render.")
        raise ValueError("TELEGRAM_BOT_TOKEN не установлен")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_get_data, pattern="get_data"))
    application.add_handler(CallbackQueryHandler(callback_help, pattern="help"))
    application.add_handler(CallbackQueryHandler(callback_back_to_menu, pattern="back_to_menu"))
    application.add_handler(CallbackQueryHandler(callback_reminder_settings, pattern="reminder_settings"))
    application.add_handler(CallbackQueryHandler(toggle_reminders, pattern="toggle_reminders"))
    application.add_handler(CallbackQueryHandler(test_reminder, pattern="test_reminder"))
    application.add_handler(CallbackQueryHandler(callback_select_group, pattern="select_group"))
    application.add_handler(CallbackQueryHandler(set_user_group, pattern="^set_group_B-11$|^set_group_B-12$"))
    application.add_handler(CallbackQueryHandler(callback_language_settings, pattern="language_settings"))
    application.add_handler(CallbackQueryHandler(set_user_language, pattern="^set_lang_ru$|^set_lang_en$"))

    # Обработчик для добавления заданий
    add_task_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_add_task, pattern="add_task")],
        states={
            EDITING_TASK: [CallbackQueryHandler(edit_task_parameter)],
            WAITING_FOR_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_input)],
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
    )

    # Обработчик для удаления заданий
    delete_task_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_delete_task, pattern="delete_task")],
        states={
            EDITING_TASK: [CallbackQueryHandler(handle_task_deletion)]
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
    )

    application.add_handler(add_task_handler)
    application.add_handler(delete_task_handler)
    
    # Настраиваем периодическую проверку напоминаний
    job_queue = application.job_queue
    if job_queue:
        # Проверка каждые 1 минутy с 00:00 до 08:55
        job_queue.run_repeating(
            check_reminders_now,
            interval=100,  # 1 минута в секундах
            first=0,
            name="frequent_reminders_check"
        )
        
        # Основная проверка в 00:05 (на всякий случай)
        job_queue.run_daily(
            check_reminders_now,
            time=datetime.strptime("00:05", "%H:%M").time(),
            days=(0, 1, 2, 3, 4, 5, 6),
            name="daily_reminders_check"
        )
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
