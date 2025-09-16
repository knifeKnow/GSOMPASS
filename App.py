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
import time

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
REMINDER_TIME = "01:26"
REMINDER_DAYS_BEFORE = list(range(10, -1, -1))
REMINDER_CHECK_INTERVAL = 60
MAX_RETRIES = 3
RETRY_DELAY = 5

# Стейты
EDITING_TASK, WAITING_FOR_INPUT, WAITING_FOR_FEEDBACK = range(1, 4)

# Языки
LANGUAGES = {"ru": "Русский", "en": "English"}

# Доступные группы
ALLOWED_GROUPS = ["B-11", "B-12"]

# Разрешенные пользователи
ALLOWED_USERS = {
    1062616885: "B-11",   #  1062616885   1042880639
    797969195: "B-12"     #  1062616885   797969195
}

class GoogleSheetsHelper:
    def __init__(self):
        self.client = None
        self.sheets = {}
        self.initialize()

    def initialize(self):
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json:
            raise ValueError("GOOGLE_CREDENTIALS environment variable not set")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), SCOPE)
        self.client = gspread.authorize(creds)
        self.load_sheets()

    def load_sheets(self):
        try:
            spreadsheet = self.client.open("GSOM-PLANNER")
            self.sheets = {
                "B-11": spreadsheet.worksheet("B-11"),
                "B-12": spreadsheet.worksheet("B-12"),
                "Users": spreadsheet.worksheet("Users")
            }
        except Exception as e:
            logger.error(f"Error loading sheets: {e}")
            raise

    def get_sheet_data(self, sheet_name):
        """Получить данные листа БЕЗ кэширования"""
        retries = 0
        while retries < MAX_RETRIES:
            try:
                sheet = self.sheets[sheet_name]
                data = sheet.get_all_values()
                return data
            except gspread.exceptions.APIError as e:
                if "429" in str(e):
                    retries += 1
                    logger.warning(f"Rate limit exceeded (429), retry {retries}/{MAX_RETRIES}")
                    time.sleep(RETRY_DELAY * retries)
                else:
                    logger.error(f"Error accessing Google Sheet {sheet_name}: {e}")
                    raise
            except Exception as e:
                logger.error(f"Unexpected error with sheet {sheet_name}: {e}")
                raise

        raise Exception("Max retries exceeded for Google Sheets API")

    def update_sheet(self, sheet_name, data):
        """Обновить данные в листе"""
        retries = 0
        while retries < MAX_RETRIES:
            try:
                sheet = self.sheets[sheet_name]
                if isinstance(data, list) and isinstance(data[0], list):
                    sheet.append_row(data[0] if len(data) == 1 else data)
                else:
                    sheet.append_row(data)
                return True
            except gspread.exceptions.APIError as e:
                if "429" in str(e):
                    retries += 1
                    logger.warning(f"Rate limit exceeded (429), retry {retries}/{MAX_RETRIES}")
                    time.sleep(RETRY_DELAY * retries)
                else:
                    logger.error(f"Error updating Google Sheet {sheet_name}: {e}")
                    raise
            except Exception as e:
                logger.error(f"Unexpected error updating sheet {sheet_name}: {e}")
                raise

        raise Exception("Max retries exceeded for Google Sheets API")

# Инициализация помощника Google Sheets
try:
    gsh = GoogleSheetsHelper()
except Exception as e:
    logger.critical(f"Failed to initialize Google Sheets Helper: {e}")
    raise

# Вспомогательные функции
def convert_to_datetime(time_str, date_str):
    """Конвертировать строку времени и даты в datetime объект с правильным годом"""
    try:
        # НЕ преобразуем "By schedule" и "По расписанию" в "23:59"
        # Оставляем всё как есть в базе данных
        
        time_parts = time_str.split('-')
        start_time = time_parts[0]
        
        # Парсим дату без года (день.месяц)
        day, month = map(int, date_str.split('.'))
        
        # Определяем правильный год
        current_date = datetime.now(MOSCOW_TZ)
        year = current_date.year
        
        # Если дата уже прошла в этом году, значит это на следующий год
        proposed_date = datetime(year, month, day)
        if proposed_date.date() < current_date.date():
            year += 1
            
        dt = datetime(year, month, day)
        
        # Добавляем время (только если это валидное время)
        if ':' in start_time and start_time not in ["By schedule", "По расписанию"]:
            hours, minutes = map(int, start_time.split(':'))
            dt = dt.replace(hour=hours, minute=minutes)
        else:
            # Для "By schedule", "По расписанию" и некорректного времени ставим конец дня
            dt = dt.replace(hour=23, minute=59)
            
        return MOSCOW_TZ.localize(dt)
    except ValueError as e:
        logger.error(f"Ошибка преобразования времени: {e}")
        return None

def get_user_data(user_id):
    """Получить данные пользователя из таблицы"""
    try:
        users = gsh.get_sheet_data("Users")
        user_row = next((row for row in users if len(row) > 0 and str(user_id) == row[0]), None)
        if user_row:
            return {
                "group": user_row[1] if len(user_row) > 1 and user_row[1] in ALLOWED_GROUPS else None,
                "reminders_enabled": len(user_row) > 2 and user_row[2].lower() == 'true',
                "language": user_row[3] if len(user_row) > 3 and user_row[3] in LANGUAGES else "ru",
                "feedback": user_row[4] if len(user_row) > 4 else ""
            }
    except Exception as e:
        logger.error(f"Error getting user data: {e}")
    return {"group": None, "reminders_enabled": True, "language": "ru", "feedback": ""}

def update_user_data(user_id, field, value):
    """Обновить данные пользователя"""
    try:
        users = gsh.get_sheet_data("Users")
        user_row_idx = next((i for i, row in enumerate(users) if len(row) > 0 and str(user_id) == row[0]), None)
        
        if user_row_idx is not None:
            col_idx = {"group": 2, "reminders_enabled": 3, "language": 4, "feedback": 5}.get(field, 2)
            gsh.sheets["Users"].update_cell(user_row_idx + 1, col_idx, str(value))
            return True
    except Exception as e:
        logger.error(f"Error updating user data: {e}")
    return False

def main_menu_keyboard(user_lang="ru"):
    """Клавиатура главного меню"""
    buttons = [
        ["📚 Посмотреть задания" if user_lang == "ru" else "📚 View tasks", "get_data"],
        ["⚡ Добавить задание" if user_lang == "ru" else "⚡ Add task", "add_task"],
        ["💣 Удалить задание" if user_lang == "ru" else "💣 Delete task", "delete_task"],
        ["🏫 Выбор группы" if user_lang == "ru" else "🏫 Select group", "select_group"],
        ["⚙️ Функционал" if user_lang == "ru" else "⚙️ Features", "help"],
        ["🏠 Назад в меню" if user_lang == "ru" else "🏠 Back to menu", "back_to_menu"]
    ]
    
    # Группируем кнопки по 2 в ряд, кроме первой и последней
    keyboard = [[InlineKeyboardButton(btn[0], callback_data=btn[1])] if i in [0, 5] else [] for i, btn in enumerate(buttons)]
    keyboard[1] = [InlineKeyboardButton(buttons[1][0], callback_data=buttons[1][1]),
                   InlineKeyboardButton(buttons[2][0], callback_data=buttons[2][1])]
    keyboard[2] = [InlineKeyboardButton(buttons[3][0], callback_data=buttons[3][1]),
                   InlineKeyboardButton(buttons[4][0], callback_data=buttons[4][1])]
    
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    
    # Если пользователя нет в таблице, добавляем его
    if not any(str(user_id) == row[0] for row in gsh.get_sheet_data("Users")[1:] if len(row) > 0):
        try:
            gsh.update_sheet("Users", [str(user_id), "", True, "ru", ""])
        except Exception as e:
            logger.error(f"Error adding user: {e}")
    
    await update.message.reply_text(
        "👋 Привет! Добро пожаловать в *GSOMPASS бот*.\n\nВыберите действие ниже:" 
        if user_data["language"] == "ru" else 
        "👋 Hi! Welcome to *GSOMPASS bot*.\n\nChoose an action below:",
        reply_markup=main_menu_keyboard(user_data["language"]),
        parse_mode='Markdown'
    )

async def callback_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню"""
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    await query.edit_message_text(
        "👋 Вы вернулись в главное меню. Выберите действие:" 
        if user_data["language"] == "ru" else 
        "👋 You're back to the main menu. Choose an action:",
        reply_markup=main_menu_keyboard(user_data["language"])
    )

async def callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать справку"""
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    keyboard = [
        [InlineKeyboardButton("🔔 Настройки напоминаний" if user_data["language"] == "ru" else "🔔 Reminder settings", 
                            callback_data="reminder_settings")],
        [InlineKeyboardButton("🌐 Изменить язык" if user_data["language"] == "ru" else "🌐 Change language", 
                            callback_data="language_settings")],
        [InlineKeyboardButton("📝 Оставить фидбэк" if user_data["language"] == "ru" else "📝 Leave feedback", 
                            callback_data="leave_feedback")],
        [InlineKeyboardButton("↩️ Назад в меню" if user_data["language"] == "ru" else "↩️ Back to menu", 
                            callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        "📌 Возможности бота:\n\n"
        "• 📋 Посмотреть задания своей группы\n"
        "• ➕ Добавить задание (для кураторов)\n"
        "• 🗑️ Удалить задание (для кураторов)\n"
        "• 🗓️ Данные берутся из Google Таблицы\n"
        "• 🔔 Напоминания о заданиях\n"
        "• 👥 Выбор/изменение группы\n"
        "• 📝 Отправить отзыв разработчику\n"
        "• 🔒 Доступ к изменению только у доверенных пользователей" 
        if user_data["language"] == "ru" else 
        "📌 Bot features:\n\n"
        "• 📋 View tasks for your group\n"
        "• ➕ Add task (for curators)\n"
        "• 🗑️ Delete task (for curators)\n"
        "• 🗓️ Data is taken from Google Sheets\n"
        "• 🔔 Task reminders\n"
        "• 👥 Select/change group\n"
        "• 📝 Send feedback to developer\n"
        "• 🔒 Only trusted users can make changes",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def show_tasks_for_group(query, group, show_delete_buttons=False):
    """Показать задания для группы"""
    try:
        data = gsh.get_sheet_data(group)[1:]  # Пропускаем заголовок
        
        user_data = get_user_data(query.from_user.id)
        response = f"📌 Задания для группы {group}:\n\n" if user_data["language"] == "ru" else f"📌 Tasks for group {group}:\n\n"
        count = 0
        tasks = []

        for idx, row in enumerate(data, start=2):
            if len(row) >= 7 and row[6] == group:
                try:
                    # Сначала проверяем что дата из таблицы актуальная (не прошлогодняя)
                    day, month = map(int, row[4].split('.'))
                    current_date = datetime.now(MOSCOW_TZ)
                    
                    # Если дата уже прошла в этом году, пропускаем это задание
                    proposed_date = datetime(current_date.year, month, day)
                    if proposed_date.date() < current_date.date():
                        continue  # Пропускаем прошедшие задания
                    
                    # Теперь проверяем дедлайн с учетом времени
                    deadline = convert_to_datetime(row[5], row[4])
                    if deadline and deadline > datetime.now(MOSCOW_TZ):
                        tasks.append((deadline, row, idx))
                except Exception as e:
                    logger.error(f"Ошибка при обработке задания: {e}")
                    continue

        tasks.sort(key=lambda x: x[0])

        keyboard = []
        for deadline, row, row_idx in tasks:
            if deadline > datetime.now(MOSCOW_TZ):
                count += 1
                
                # Просто показываем время как есть
                time_display = row[5]
                
                # Добавляем иконку типа книги
                book_icon = "📖" if len(row) > 7 and row[7] == "open-book" else "📕"
                
                # Формируем строку с деталями (только если детали есть)
                details = ""
                if len(row) > 8 and row[8] and row[8].strip() and row[8] != "не выбраны" and row[8] != "not selected":
                    details = f" | {row[8]}\n"  # Добавляем перенос строки после деталей
                
                response += (
                    f"📚 *{row[0]}* — {row[1]} {book_icon} | {row[2]}\n"
                    f"📅 {row[4]} | 🕒 {time_display} | *{row[3]}* баллов курса\n" 
                    f"{details}\n"  # Детали уже содержат перенос строки
                    if user_data["language"] == "ru" else
                    f"📚 *{row[0]}* — {row[1]} {book_icon} ({row[2]})\n"                   
                    f"📅 {row[4]} | 🕒 {time_display} | *{row[3]}* course points\n"
                    f"{details}\n"  # Детали уже содержат перенос строки
                )
                
                if show_delete_buttons:
                    keyboard.append([InlineKeyboardButton(
                        f"🗑️ Удалить: {row[0]} ({row[4]})" 
                        if user_data["language"] == "ru" else 
                        f"🗑️ Delete: {row[0]} ({row[4]})",
                        callback_data=f"delete_{group}_{row_idx}"
                    )])

        if count == 0:
            response = "ℹ️ Пока нет заданий для вашей группы." if user_data["language"] == "ru" else "ℹ️ No tasks for your group yet."

        if show_delete_buttons:
            keyboard.append([InlineKeyboardButton(
                "↩️ Назад" if user_data["language"] == "ru" else "↩️ Back", 
                callback_data="back_to_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
        else:
            reply_markup = main_menu_keyboard(user_data["language"])

        await query.edit_message_text(response, parse_mode='Markdown', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка при получении заданий: {e}")
        user_data = get_user_data(query.from_user.id)
        await query.edit_message_text(
            f"⛔ Ошибка при получении заданий: {str(e)}" 
            if user_data["language"] == "ru" else 
            f"⛔ Error getting tasks: {str(e)}",
            reply_markup=main_menu_keyboard(user_data["language"]))
        
async def callback_get_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить данные о заданиях"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(user_id)

    if not user_data["group"] and user_id in ALLOWED_USERS:
        group = ALLOWED_USERS[user_id]
        if update_user_data(user_id, "group", group):
            user_data["group"] = group

    if user_data["group"]:
        await show_tasks_for_group(query, user_data["group"])
    else:
        await callback_select_group(update, context)

async def callback_select_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор группы"""
    query = update.callback_query
    if query:
        await query.answer()
    
    user_data = get_user_data(query.from_user.id if query else update.effective_user.id)
    
    group_keyboard = [
        [InlineKeyboardButton("B-11", callback_data="set_group_B-11"),
         InlineKeyboardButton("B-12", callback_data="set_group_B-12")],
        [InlineKeyboardButton(
            "↩️ Назад в меню" if user_data["language"] == "ru" else "↩️ Back to menu", 
            callback_data="back_to_menu")]
    ]
    
    text = "👥 Выберите вашу группу:" if user_data["language"] == "ru" else "👥 Select your group:"
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(group_keyboard))
    else:
        await context.bot.send_message(
            update.effective_chat.id,
            text,
            reply_markup=InlineKeyboardMarkup(group_keyboard)
        )

async def set_user_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить группу пользователя"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    group = query.data.replace("set_group_", "")
    
    if update_user_data(user_id, "group", group):
        user_data = get_user_data(user_id)
        await query.edit_message_text(
            f"✅ Ваша группа установлена: {group}" 
            if user_data["language"] == "ru" else 
            f"✅ Your group is set: {group}",
            reply_markup=main_menu_keyboard(user_data["language"]))
        
        if user_data["reminders_enabled"]:
            await schedule_reminders_for_user(context.application.job_queue, user_id)
    else:
        user_data = get_user_data(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при установке группы." 
            if user_data["language"] == "ru" else 
            "⛔ An error occurred while setting the group.",
            reply_markup=main_menu_keyboard(user_data["language"]))

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
            InlineKeyboardButton("📖", callback_data="open-book"),
            InlineKeyboardButton("📕", callback_data="closed-book"),
            InlineKeyboardButton(
                "📝 Детали (опционально)" if user_lang == "ru" else "📝 Details (optional)", 
                callback_data="edit_details"
            )
        ],
        [
            InlineKeyboardButton("✅ Сохранить" if user_lang == "ru" else "✅ Save", callback_data="save_task"),
            InlineKeyboardButton("❌ Отменить" if user_lang == "ru" else "❌ Cancel", callback_data="cancel_task")
        ]
    ])

def generate_subject_keyboard(user_lang="ru"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Entrepreneurship", callback_data="Entrepreneurship"),
         InlineKeyboardButton("Financial Analysis", callback_data="Financial Analysis")],
        [InlineKeyboardButton("International Economics", callback_data="International Economics"),
         InlineKeyboardButton("Law", callback_data="Law")],
        [InlineKeyboardButton("Marketing", callback_data="Marketing"),
         InlineKeyboardButton("Statistics", callback_data="Statistics")],
        [InlineKeyboardButton("Другое" if user_lang == "ru" else "Other", callback_data="other_subject")],
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
         InlineKeyboardButton("Offline", callback_data="Offline")],
        [InlineKeyboardButton("↩️ Назад к редактированию" if user_lang == "ru" else "↩️ Back to editing", callback_data="back_to_editing")]
    ])

def generate_details_keyboard(user_lang="ru"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Calculators allowed", callback_data="Calculators allowed")],
        [InlineKeyboardButton("Notes allowed", callback_data="Notes allowed")],
        [InlineKeyboardButton("Phones allowed", callback_data="Phones allowed")],
        [InlineKeyboardButton("Другое" if user_lang == "ru" else "Other", callback_data="other_details")],
        [InlineKeyboardButton("↩️ Назад к редактированию" if user_lang == "ru" else "↩️ Back to editing", callback_data="back_to_editing")]
    ])

async def format_task_message(context):
    task_data = context.user_data.get("task_data", {})
    user_data = get_user_data(context._user_id) if hasattr(context, '_user_id') else {"language": "ru"}
    
    message = "📝 Редактирование задания:\n\n" if user_data["language"] == "ru" else "📝 Editing task:\n\n"
    message += f"🔹 <b>Предмет:</b> {task_data.get('subject', 'не выбрано' if user_data['language'] == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Тип задания:</b> {task_data.get('task_type', 'не выбрано' if user_data['language'] == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Макс. баллы:</b> {task_data.get('max_points', 'не выбрано' if user_data['language'] == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Дата:</b> {task_data.get('date', 'не выбрана' if user_data['language'] == 'ru' else 'not selected')}\n"
    
    time_display = task_data.get('time', 'не выбрано' if user_data['language'] == 'ru' else 'not selected')
    if time_display == "23:59":
        time_display = "By schedule" if user_data['language'] == "en" else "По расписанию"
    elif time_display == "time_schedule":
        time_display = "By schedule" if user_data['language'] == "en" else "По расписанию"
    message += f"🔹 <b>Время:</b> {time_display}\n"
    
    message += f"🔹 <b>Формат:</b> {task_data.get('format', 'не выбран' if user_data['language'] == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Тип книги:</b> {task_data.get('book_type', 'не выбран' if user_data['language'] == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Детали:</b> {task_data.get('details', 'не выбраны' if user_data['language'] == 'ru' else 'not selected')}\n\n"
    message += "Выберите параметр для изменения или сохраните задание:" if user_data['language'] == "ru" else "Select a parameter to change or save the task:"
    return message

async def callback_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(user_id)

    if user_id not in ALLOWED_USERS:
        await query.edit_message_text(
            "⛔ У вас нет доступа к добавлению заданий." if user_data["language"] == "ru" else "⛔ You don't have access to add tasks.",
            reply_markup=main_menu_keyboard(user_data["language"]))
        return ConversationHandler.END

    context.user_data["task_data"] = {
        "group": ALLOWED_USERS[user_id],
        "subject": "не выбрано" if user_data["language"] == "ru" else "not selected",
        "task_type": "не выбрано" if user_data["language"] == "ru" else "not selected",
        "max_points": "не выбрано" if user_data["language"] == "ru" else "not selected",
        "date": "не выбрана" if user_data["language"] == "ru" else "not selected",
        "time": "не выбрано" if user_data["language"] == "ru" else "not selected",
        "format": "не выбран" if user_data["language"] == "ru" else "not selected",
        "book_type": "не выбран" if user_data["language"] == "ru" else "not selected",
        "details": "не выбраны" if user_data["language"] == "ru" else "not selected"
    }

    message = await format_task_message(context)
    await query.edit_message_text(
        message,
        reply_markup=generate_edit_task_keyboard(user_data["language"]),
        parse_mode='HTML'
    )
    return EDITING_TASK

async def edit_task_parameter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    if query.data == "edit_subject":
        await query.edit_message_text(
            "✍️ Выберите предмет:" if user_data["language"] == "ru" else "✍️ Select subject:",
            reply_markup=generate_subject_keyboard(user_data["language"])
        )
    elif query.data == "edit_task_type":
        await query.edit_message_text(
            "📘 Выберите тип задания:" if user_data["language"] == "ru" else "📘 Select task type:",
            reply_markup=generate_task_type_keyboard(user_data["language"])
        )
    elif query.data == "edit_max_points":
        await query.edit_message_text(
            "💯 Выберите количество баллов от курса:" if user_data["language"] == "ru" else "💯 Select course points:",
            reply_markup=generate_points_keyboard(user_data["language"])
        )
    elif query.data == "edit_date":
        await query.edit_message_text(
            "🗓️ Выберите дату:" if user_data["language"] == "ru" else "🗓️ Select date:",
            reply_markup=generate_date_buttons(user_data["language"])
        )
    elif query.data == "edit_time":
        await query.edit_message_text(
            "⏰ Выберите время:" if user_data["language"] == "ru" else "⏰ Select time:",
            reply_markup=generate_time_keyboard(user_data["language"])
        )
    elif query.data == "edit_format":
        await query.edit_message_text(
            "📍 Выберите формат:" if user_data["language"] == "ru" else "📍 Select format:",
            reply_markup=generate_format_keyboard(user_data["language"])
        )
    elif query.data == "edit_details":
        await query.edit_message_text(
            "📝 Выберите детали:" if user_data["language"] == "ru" else "📝 Select details:",
            reply_markup=generate_details_keyboard(user_data["language"])
        )
    elif query.data == "back_to_editing":
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
            parse_mode='HTML'
        )
    elif query.data in ["open-book", "closed-book"]:
        context.user_data["task_data"]["book_type"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
            parse_mode='HTML'
        )
    elif query.data in ["Calculators allowed", "Notes allowed", "Phones allowed"]:
        context.user_data["task_data"]["details"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
            parse_mode='HTML'
        )
    elif query.data == "other_details":
        await query.edit_message_text("📝 Введите детали:" if user_data["language"] == "ru" else "📝 Enter details:")
        context.user_data["waiting_for"] = "details"
        return WAITING_FOR_INPUT
        
    elif query.data.startswith(("Entrepreneurship", "Financial Analysis", "International Economics", 
                          "Law", "Marketing", "Statistics")):            
        context.user_data["task_data"]["subject"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
            parse_mode='HTML'
        )
    elif query.data.startswith(("Test", "HW", "MidTerm", "FinalTest")):
        context.user_data["task_data"]["task_type"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
            parse_mode='HTML'
        )
    elif query.data.startswith("points_"):
        points_value = query.data[7:]
        context.user_data["task_data"]["max_points"] = points_value
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
            parse_mode='HTML'
        )
    elif len(query.data.split('.')) == 2 and query.data.count('.') == 1:
        context.user_data["task_data"]["date"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
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
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
            parse_mode='HTML'
        )
    elif query.data in ["Online", "Offline"]:
        context.user_data["task_data"]["format"] = query.data
        message = await format_task_message(context)
        await query.edit_message_text(
            message,
            reply_markup=generate_edit_task_keyboard(user_data["language"]),
            parse_mode='HTML'
        )
    elif query.data == "other_subject":
        await query.edit_message_text("✍️ Введите название предмета:" if user_data["language"] == "ru" else "✍️ Enter subject name:")
        context.user_data["waiting_for"] = "subject"
        return WAITING_FOR_INPUT
    elif query.data == "other_task_type":
        await query.edit_message_text("📘 Введите тип задания:" if user_data["language"] == "ru" else "📘 Enter task type:")
        context.user_data["waiting_for"] = "task_type"
        return WAITING_FOR_INPUT
    elif query.data == "other_max_points":
        await query.edit_message_text("💯 Введите количество баллов:" if user_data["language"] == "ru" else "💯 Enter points:")
        context.user_data["waiting_for"] = "max_points"
        return WAITING_FOR_INPUT
    elif query.data == "custom_date":
        await query.edit_message_text("🗓️ Введите дату в формате ДД.ММ (например, 15.12):" if user_data["language"] == "ru" else "🗓️ Enter date in DD.MM format (e.g., 15.12):")
        context.user_data["waiting_for"] = "date"
        return WAITING_FOR_INPUT
    elif query.data == "save_task":
        task_data = context.user_data.get("task_data", {})
        if (task_data["subject"] == ("не выбрано" if user_data["language"] == "ru" else "not selected") or 
            task_data["task_type"] == ("не выбрано" if user_data["language"] == "ru" else "not selected") or 
            task_data["max_points"] == ("не выбрано" if user_data["language"] == "ru" else "not selected") or 
            task_data["date"] == ("не выбрана" if user_data["language"] == "ru" else "not selected") or 
            task_data["time"] == ("не выбрано" if user_data["language"] == "ru" else "not selected") or 
            task_data["format"] == ("не выбран" if user_data["language"] == "ru" else "not selected") or 
            task_data["book_type"] == ("не выбран" if user_data["language"] == "ru" else "not selected")):
            
            await query.answer(
                "⚠️ Заполните все обязательные поля перед сохранением!" if user_data["language"] == "ru" else "⚠️ Fill all required fields before saving!",
                show_alert=True)
            return EDITING_TASK
        
        group = task_data["group"]
        
        try:
            row_data = [
                task_data["subject"],
                task_data["task_type"],
                task_data["format"],
                task_data["max_points"],
                task_data["date"],
                task_data["time"],
                group,
                task_data["book_type"],
                task_data.get("details", "")
            ]
            
            gsh.update_sheet(group, row_data)
            context.user_data.clear()
            
            # Обновляем напоминания для всех пользователей группы
            await refresh_reminders_for_group(context.application.job_queue, group)
            
            await query.edit_message_text(
                "✅ Задание успешно добавлено!" if user_data["language"] == "ru" else "✅ Task added successfully!",
                reply_markup=main_menu_keyboard(user_data["language"]))
        except Exception as e:
            logger.error(f"Ошибка при сохранении задания: {e}")
            await query.edit_message_text(
                f"⛔ Произошла ошибка при сохранении: {str(e)}" if user_data["language"] == "ru" else f"⛔ Error saving: {str(e)}",
                reply_markup=main_menu_keyboard(user_data["language"]))
        return ConversationHandler.END
    elif query.data == "cancel_task":
        context.user_data.clear()
        await query.edit_message_text(
            "🚫 Добавление задания отменено." if user_data["language"] == "ru" else "🚫 Task addition canceled.",
            reply_markup=main_menu_keyboard(user_data["language"]))
        return ConversationHandler.END
    
    return EDITING_TASK

async def handle_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    waiting_for = context.user_data.get("waiting_for")
    user_data = get_user_data(update.effective_user.id)
    
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
                    "⚠️ Неверный формат дату. Введите дату в формате ДД.ММ (например, 15.12)" if user_data["language"] == "ru" else 
                    "⚠️ Wrong date format. Enter date in DD.MM format (e.g., 15.12)")
                return WAITING_FOR_INPUT
        except:
            await update.message.reply_text(
                "⚠️ Неверный формат дату. Введите дату в формате ДД.ММ (например, 15.12)" if user_data["language"] == "ru" else 
                "⚠️ Wrong date format. Enter date in DD.MM format (e.g., 15.12)")
            return WAITING_FOR_INPUT
    elif waiting_for == "details":
        context.user_data["task_data"]["details"] = user_input
    
    del context.user_data["waiting_for"]
    
    message = await format_task_message(context)
    await update.message.reply_text(
        message,
        reply_markup=generate_edit_task_keyboard(user_data["language"]),
        parse_mode='HTML'
    )
    return EDITING_TASK

async def callback_delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(user_id)

    if user_id not in ALLOWED_USERS:
        await query.edit_message_text(
            "⛔ У вас нет доступа к удалению заданий." if user_data["language"] == "ru" else "⛔ You don't have access to delete tasks.",
            reply_markup=main_menu_keyboard(user_data["language"]))
        return ConversationHandler.END

    group = ALLOWED_USERS[user_id]
    await show_tasks_for_group(query, group, show_delete_buttons=True)
    return EDITING_TASK

async def handle_task_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    if query.data == "back_to_menu":
        await callback_back_to_menu(update, context)
        return ConversationHandler.END
    
    if query.data.startswith("delete_"):
        try:
            _, group, row_idx = query.data.split("_")
            row_idx = int(row_idx)
            
            all_values = gsh.get_sheet_data(group)
            if row_idx <= len(all_values):
                gsh.sheets[group].delete_rows(row_idx)
                
                await query.edit_message_text(
                    "✅ Задание успешно удалено!" if user_data["language"] == "ru" else "✅ Task deleted successfully!",
                    reply_markup=main_menu_keyboard(user_data["language"]))
                
                # Обновляем напоминания для всех пользователей группы
                await refresh_reminders_for_group(context.application.job_queue, group)
            else:
                await query.edit_message_text(
                    "⛔ Задание уже было удалено" if user_data["language"] == "ru" else "⛔ Task was already deleted",
                    reply_markup=main_menu_keyboard(user_data["language"]))
        except Exception as e:
            logger.error(f"Ошибка при удалении задания: {e}")
            await query.edit_message_text(
                f"⛔ Ошибка при удалении: {str(e)}" if user_data["language"] == "ru" else f"⛔ Error deleting: {str(e)}",
                reply_markup=main_menu_keyboard(user_data["language"]))
    
    return ConversationHandler.END

async def callback_reminder_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    try:
        keyboard = [
            [InlineKeyboardButton(
                "🔔 Reminders: On" if user_data["reminders_enabled"] else "🔔 Reminders: Off",
                callback_data="toggle_reminders")],
            [InlineKeyboardButton(
                "↩️ Назад в меню" if user_data["language"] == "ru" else "↩️ Back to menu",
                callback_data="back_to_menu")]
        ]
        
        await query.edit_message_text(
            f"🔔 Настройки напоминаний:\n\n"
            f"Напоминания приходят каждый день в {REMINDER_TIME} по МСК за:\n"
            f"10, 9, 8, ..., 1 день и в день задания." if user_data["language"] == "ru" else 
            f"🔔 Reminder settings:\n\n"
            f"Reminders are sent daily at {REMINDER_TIME} MSK for:\n"
            f"10, 9, 8, ..., 1 days before and on the task day.",
            reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Ошибка в callback_reminder_settings: {e}")
        await query.edit_message_text(
            "⛔ Произошла ошибка при получении настроек." if user_data["language"] == "ru" else "⛔ Error getting settings.",
            reply_markup=main_menu_keyboard(user_data["language"]))

async def toggle_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(user_id)
    
    try:
        new_state = not user_data["reminders_enabled"]
        if update_user_data(user_id, "reminders_enabled", new_state):
            user_data["reminders_enabled"] = new_state
        
        await schedule_reminders_for_user(context.application.job_queue, user_id)
        
        await query.edit_message_text(
            f"✅ Напоминания {'включены' if new_state else 'выключены'}!" if user_data["language"] == "ru" else f"✅ Reminders {'enabled' if new_state else 'disabled'}!",
            reply_markup=main_menu_keyboard(user_data["language"]))
    except Exception as e:
        logger.error(f"Ошибка в toggle_reminders: {e}")
        await query.edit_message_text(
            "⛔ Произошла ошибка при изменении настроек." if user_data["language"] == "ru" else "⛔ Error changing settings.",
            reply_markup=main_menu_keyboard(user_data["language"]))

async def schedule_reminders_for_user(job_queue: JobQueue, user_id: int):
    """Запланировать напоминания для пользователя"""
    try:
        logger.info(f"Scheduling reminders for user {user_id}")
        
        # Удаление старых напоминаний
        for job in job_queue.jobs():
            if job.name and str(user_id) in job.name:
                job.schedule_removal()

        user_data = get_user_data(user_id)
        if not user_data["reminders_enabled"] or not user_data["group"]:
            return

        data = gsh.get_sheet_data(user_data["group"])[1:]  # Пропускаем заголовок
        now = datetime.now(MOSCOW_TZ)
        today = now.date()
        tasks_for_reminder = []
        
        for row in data:
            if len(row) >= 7 and row[6] == user_data["group"]:
                try:
                    deadline = convert_to_datetime(row[5], row[4])
                    if not deadline:
                        continue
                        
                    days_left = (deadline.date() - today).days
                    if 0 <= days_left <= 10:
                        tasks_for_reminder.append({
                            'subject': row[0],
                            'task_type': row[1],
                            'date': row[4],
                            'time': row[5],
                            'days_left': days_left,
                            'max_points': row[3],
                            'format': row[2],
                            'book_type': row[7] if len(row) > 7 else "",
                            'details': row[8] if len(row) > 8 else ""
                        })
                except Exception as e:
                    logger.error(f"Ошибка обработки строки {row}: {e}")

        if tasks_for_reminder:
            tasks_for_reminder.sort(key=lambda x: x['days_left'])
            
            # Планирование на 09:00 по МСК
            reminder_time = datetime.strptime(REMINDER_TIME, "%H:%M").time()
            next_reminder = datetime.combine(datetime.now().date(), reminder_time)
            
            if datetime.now().time() > reminder_time:
                next_reminder += timedelta(days=1)
            
            next_reminder = MOSCOW_TZ.localize(next_reminder)
            
            job_queue.run_repeating(
                send_daily_reminder_callback,
                interval=timedelta(days=1),
                first=next_reminder,
                chat_id=user_id,
                data={'tasks': tasks_for_reminder},
                name=f"daily_reminder_{user_id}"
            )

    except Exception as e:
        logger.error(f"Error in schedule_reminders_for_user: {e}")

async def send_daily_reminder_callback(context: ContextTypes.DEFAULT_TYPE):
    """Колбэк для ежедневного напоминания"""
    await send_daily_reminder(context, context.job.chat_id, context.job.data['tasks'])

async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, tasks: list):
    """Отправить ежедневное напоминание"""
    if not tasks:
        return
    
    user_data = get_user_data(user_id)
    
    # Группируем задачи по дням до дедлайна
    tasks_by_days = {}
    for task in tasks:
        if task['days_left'] not in tasks_by_days:
            tasks_by_days[task['days_left']] = []
        tasks_by_days[task['days_left']].append(task)
    
    # Сортируем дни по возрастанию
    sorted_days = sorted(tasks_by_days.keys())
    
    # Создаем сообщение
    message = "🔔 *ЕЖЕДНЕВНОЕ НАПОМИНАНИЕ*\n\n" if user_data["language"] == "ru" else "🔔 *DAILY TASKS REMINDER*\n\n"
    
    for days_left in sorted_days:
        if days_left == 0:
            day_header = "\n*СЕГОДНЯ*" if user_data["language"] == "ru" else "\n*TODAY*"
        elif days_left == 1:
            day_header = "\n*ЗАВТРА*" if user_data["language"] == "ru" else "\n*TOMORROW*"
        else:
            day_header = f"\n*ЧЕРЕЗ {days_left} ДНЕЙ*" if user_data["language"] == "ru" else f"\n*IN {days_left} DAYS*"
        
        message += f"{day_header}\n"
        
        for task in tasks_by_days[days_left]:
            # Просто показываем время как есть (без года)
            time_display = task['time']
                
            book_icon = "📖" if task.get('book_type') == "open-book" else "📕"
            
            # Формируем строку с деталями (только если детали есть и они не "не выбраны")
            details = ""
            if (task.get('details') and 
                task['details'].strip() and 
                task['details'] not in ["не выбраны", "not selected", ""]):
                details = f" | {task['details']}\n"
            
            message += (
                f"{book_icon} *{task['subject']}* — {task['task_type']} | {task['format']}\n"
                f"📅 {task['date']} | 🕒 {time_display} | *{task['max_points']}* баллов курса\n" 
                f"{details}\n"  # Детали только если есть
                if user_data["language"] == "ru" else
                f"{book_icon} *{task['subject']}* — {task['task_type']} ({task['format']})\n"                   
                f"📅 {task['date']} | 🕒 {time_display} | *{task['max_points']}* course points\n"
                f"{details}\n"  # Детали только если есть
            )
    
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
        users = gsh.get_sheet_data("Users")
        for row in users[1:]:
            if len(row) > 1 and row[1] == group and len(row) > 2 and row[2].lower() == 'true':
                user_id = int(row[0])
                await schedule_reminders_for_user(job_queue, user_id)
    except Exception as e:
        logger.error(f"Ошибка в refresh_reminders_for_group: {e}")

async def check_reminders_now(context: ContextTypes.DEFAULT_TYPE):
    """Проверить и отправить напоминания прямо сейчас"""
    try:
        users = gsh.get_sheet_data("Users")
        for row in users[1:]:
            if len(row) > 2 and row[2].lower() == 'true':
                user_id = int(row[0])
                await schedule_reminders_for_user(context.job_queue, user_id)
    except Exception as e:
        logger.error(f"Ошибка в check_reminders_now: {e}")

async def callback_language_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="set_lang_ru")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en")],
        [InlineKeyboardButton("↩️ Назад в меню" if user_data["language"] == "ru" else "↩️ Back to menu", callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        "🌐 Выберите язык:" if user_data["language"] == "ru" else "🌐 Select language:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def set_user_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("set_lang_", "")
    
    try:
        if update_user_data(user_id, "language", lang):
            user_data = get_user_data(user_id)
            await query.edit_message_text(
                "✅ Язык изменен на русский!" if user_data["language"] == "ru" else "✅ Language changed to English!",
                reply_markup=main_menu_keyboard(user_data["language"]))
    except Exception as e:
        logger.error(f"Ошибка при изменении языка: {e}")
        user_data = get_user_data(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при изменении языка." if user_data["language"] == "ru" else "⛔ Error changing language.",
            reply_markup=main_menu_keyboard(user_data["language"]))

async def callback_leave_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    await query.edit_message_text(
        "📝 Пожалуйста, напишите ваш отзыв или предложение по улучшению бота:" if user_data["language"] == "ru" else 
        "📝 Please write your feedback or suggestion for improving the bot:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить" if user_data["language"] == "ru" else "↩️ Cancel", callback_data="cancel_feedback")]])
    )
    return WAITING_FOR_FEEDBACK

async def handle_feedback_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    feedback_text = update.message.text
    user_data = get_user_data(user_id)
    
    try:
        if update_user_data(user_id, "feedback", feedback_text):
            await update.message.reply_text(
                "✅ Спасибо за ваш отзыв! Мы учтем ваши пожелания." if user_data["language"] == "ru" else 
                "✅ Thank you for your feedback! We'll take it into account.",
                reply_markup=main_menu_keyboard(user_data["language"]))
        else:
            await update.message.reply_text(
                "⛔ Не удалось сохранить отзыв. Попробуйте позже." if user_data["language"] == "ru" else 
                "⛔ Failed to save feedback. Please try again later.",
                reply_markup=main_menu_keyboard(user_data["language"]))
    except Exception as e:
        logger.error(f"Ошибка при сохранении фидбэка: {e}")
        await update.message.reply_text(
            "⛔ Произошла ошибка при сохранении отзыва." if user_data["language"] == "ru" else 
            "⛔ An error occurred while saving feedback.",
            reply_markup=main_menu_keyboard(user_data["language"]))
    
    return ConversationHandler.END

async def cancel_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    await query.edit_message_text(
        "🚫 Отправка отзыва отменена." if user_data["language"] == "ru" else "🚫 Feedback submission canceled.",
        reply_markup=main_menu_keyboard(user_data["language"]))
    return ConversationHandler.END

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    application = Application.builder().token(token).build()

    # Основные обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_get_data, pattern="get_data"))
    application.add_handler(CallbackQueryHandler(callback_help, pattern="help"))
    application.add_handler(CallbackQueryHandler(callback_back_to_menu, pattern="back_to_menu"))
    application.add_handler(CallbackQueryHandler(callback_select_group, pattern="select_group"))
    application.add_handler(CallbackQueryHandler(set_user_group, pattern="^set_group_B-11$|^set_group_B-12$"))

    # Обработчики настроек
    application.add_handler(CallbackQueryHandler(callback_reminder_settings, pattern="reminder_settings"))
    application.add_handler(CallbackQueryHandler(toggle_reminders, pattern="toggle_reminders"))
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

    # Обработчик для фидбэка
    feedback_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_leave_feedback, pattern="leave_feedback")],
        states={
            WAITING_FOR_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_feedback_input),
                CallbackQueryHandler(cancel_feedback, pattern="cancel_feedback")
            ]
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
    )

    application.add_handler(add_task_handler)
    application.add_handler(delete_task_handler)
    application.add_handler(feedback_handler)
    
    # Настраиваем периодическую проверку напоминаний
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(
            check_reminders_now,
            interval=timedelta(minutes=REMINDER_CHECK_INTERVAL),
            first=10
        )
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
