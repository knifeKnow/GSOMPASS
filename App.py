import os
import json
import gspread
import re
import logging
import time
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
import random

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== КОНСТАНТЫ ====================
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
REMINDER_TIME = "09:00"
REMINDER_CHECK_INTERVAL = 60
MAX_RETRIES = 3
RETRY_DELAY = 5

# Стейты для ConversationHandler
EDITING_TASK, WAITING_FOR_INPUT, WAITING_FOR_FEEDBACK = range(3, 6)
WAITING_FOR_CURATOR_ID, WAITING_FOR_GROUP_NAME = range(6, 8)

# Языки
LANGUAGES = {"ru": "Русский", "en": "English"}

# Доступные группы (теперь будут создаваться автоматически)
ALLOWED_GROUPS = []

# Суперадмины (только твой user_id)
SUPER_ADMINS = [1062616885]  # Замени на свой user_id

# ==================== КЛАСС ДЛЯ РАБОТЫ С GOOGLE SHEETS ====================
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
            worksheets = spreadsheet.worksheets()
            self.sheets = {ws.title: ws for ws in worksheets}
        except Exception as e:
            logger.error(f"Error loading sheets: {e}")
            raise

    def get_sheet_data(self, sheet_name):
        """Получить данные листа БЕЗ кэширования"""
        retries = 0
        while retries < MAX_RETRIES:
            try:
                if sheet_name not in self.sheets:
                    return []
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

    def create_worksheet(self, group_name):
        """Создать новый лист для группы"""
        try:
            spreadsheet = self.client.open("GSOM-PLANNER")
            
            # Проверяем, существует ли уже лист
            if group_name in self.sheets:
                return self.sheets[group_name]
            
            # Создаем новый лист
            worksheet = spreadsheet.add_worksheet(title=group_name, rows="100", cols="20")
            
            # Добавляем заголовки
            headers = ["Subject", "Task Type", "Format", "Max Points", "Date", "Time", "Group", "Book Type", "Details"]
            worksheet.append_row(headers)
            
            # Обновляем кэш
            self.sheets[group_name] = worksheet
            
            logger.info(f"Created new worksheet: {group_name}")
            return worksheet
            
        except Exception as e:
            logger.error(f"Error creating worksheet {group_name}: {e}")
            raise

    def archive_worksheet(self, group_name):
        """Архивировать лист (переименовать)"""
        try:
            if group_name not in self.sheets:
                return False
                
            spreadsheet = self.client.open("GSOM-PLANNER")
            worksheet = self.sheets[group_name]
            
            # Создаем архивное название
            archive_name = f"{group_name}_Archive_{datetime.now().strftime('%Y_%m')}"
            worksheet.update_title(archive_name)
            
            # Обновляем кэш
            del self.sheets[group_name]
            self.sheets[archive_name] = worksheet
            
            logger.info(f"Archived worksheet: {group_name} -> {archive_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error archiving worksheet {group_name}: {e}")
            return False

# Инициализация помощника Google Sheets
try:
    gsh = GoogleSheetsHelper()
except Exception as e:
    logger.critical(f"Failed to initialize Google Sheets Helper: {e}")
    raise

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def convert_to_datetime(time_str, date_str):
    """Конвертировать строку времени и даты в datetime объект"""
    try:
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
        
        # Добавляем время
        if ':' in start_time and start_time not in ["By schedule", "По расписанию"]:
            hours, minutes = map(int, start_time.split(':'))
            dt = dt.replace(hour=hours, minute=minutes)
        else:
            # Для "By schedule", "По расписанию" ставим конец дня
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
                "group": user_row[1] if len(user_row) > 1 and user_row[1] != "" else None,
                "reminders_enabled": len(user_row) > 2 and user_row[2].lower() == 'true',
                "language": user_row[3] if len(user_row) > 3 and user_row[3] in LANGUAGES else "ru",
                "feedback": user_row[4] if len(user_row) > 4 else "",
                "is_curator": len(user_row) > 5 and user_row[5].lower() == 'true'
            }
    except Exception as e:
        logger.error(f"Error getting user data: {e}")
    return {"group": None, "reminders_enabled": True, "language": "ru", "feedback": "", "is_curator": False}

def update_user_data(user_id, field, value):
    """Обновить данные пользователя"""
    try:
        users = gsh.get_sheet_data("Users")
        user_row_idx = next((i for i, row in enumerate(users) if len(row) > 0 and str(user_id) == row[0]), None)
        
        if user_row_idx is not None:
            col_idx = {
                "group": 2, 
                "reminders_enabled": 3, 
                "language": 4, 
                "feedback": 5,
                "is_curator": 6
            }.get(field, 2)
            
            # Обновляем ячейку
            gsh.sheets["Users"].update_cell(user_row_idx + 1, col_idx, str(value))
            return True
    except Exception as e:
        logger.error(f"Error updating user data: {e}")
    return False

def add_new_user(user_id):
    """Добавить нового пользователя в таблицу"""
    try:
        # Проверяем, есть ли уже пользователь
        users = gsh.get_sheet_data("Users")
        if any(str(user_id) == row[0] for row in users if len(row) > 0):
            return True
            
        # Добавляем нового пользователя
        new_user = [str(user_id), "", "TRUE", "ru", "", "FALSE"]
        gsh.update_sheet("Users", new_user)
        return True
    except Exception as e:
        logger.error(f"Error adding new user: {e}")
        return False

def get_all_curators():
    """Получить список всех кураторов"""
    try:
        users = gsh.get_sheet_data("Users")
        curators = []
        for row in users[1:]:  # Пропускаем заголовок
            if len(row) > 5 and row[5].lower() == 'true':
                curators.append({
                    'user_id': row[0],
                    'group': row[1] if len(row) > 1 else '',
                    'language': row[3] if len(row) > 3 else 'ru'
                })
        return curators
    except Exception as e:
        logger.error(f"Error getting curators: {e}")
        return []

# ==================== КЛАВИАТУРЫ ====================
def main_menu_keyboard(user_lang="ru", is_curator=False):
    """Клавиатура главного меню"""
    buttons = [
        ["📚 Посмотреть задания" if user_lang == "ru" else "📚 View tasks", "get_data"],
        ["⚡ Добавить задание" if user_lang == "ru" else "⚡ Add task", "add_task"],
        ["💣 Удалить задание" if user_lang == "ru" else "💣 Delete task", "delete_task"],
        ["🏫 Выбор группы" if user_lang == "ru" else "🏫 Select group", "select_group"],
        ["⚙️ Функционал" if user_lang == "ru" else "⚙️ Features", "help"],
    ]
    
    # Показываем кнопки добавления/удаления только кураторам
    if not is_curator:
        buttons = [buttons[0], buttons[3], buttons[4]]
    
    keyboard = []
    for i, btn in enumerate(buttons):
        keyboard.append([InlineKeyboardButton(btn[0], callback_data=btn[1])])
    
    return InlineKeyboardMarkup(keyboard)

def admin_keyboard(user_lang="ru"):
    """Клавиатура админ-панели"""
    keyboard = [
        [InlineKeyboardButton("👥 Назначить куратора" if user_lang == "ru" else "👥 Make curator", callback_data="admin_make_curator")],
        [InlineKeyboardButton("📋 Список кураторов" if user_lang == "ru" else "📋 Curators list", callback_data="admin_list_curators")],
        [InlineKeyboardButton("📊 Статистика" if user_lang == "ru" else "📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("🎓 Новый семестр" if user_lang == "ru" else "🎓 New semester", callback_data="admin_new_semester")],
        [InlineKeyboardButton("↩️ Назад" if user_lang == "ru" else "↩️ Back", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

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

# Генераторы клавиатур для редактирования задания (остаются без изменений)
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

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    
    # Добавляем пользователя в систему если его нет
    if not add_new_user(user_id):
        await update.message.reply_text("❌ Ошибка при регистрации. Попробуйте позже.")
        return
    
    user_data = get_user_data(user_id)
    
    welcome_text = (
        "👋 Привет! Добро пожаловать в *GSOMPASS бот*.\n\n"
        "Выберите действие ниже:" 
        if user_data["language"] == "ru" else 
        "👋 Hi! Welcome to *GSOMPASS bot*.\n\n"
        "Choose an action below:"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]),
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
        reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"])
    )

async def callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать справку"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(user_id)
    
    keyboard = [
        [InlineKeyboardButton("🔔 Настройки напоминаний" if user_data["language"] == "ru" else "🔔 Reminder settings", 
                            callback_data="reminder_settings")],
        [InlineKeyboardButton("🌐 Изменить язык" if user_data["language"] == "ru" else "🌐 Change language", 
                            callback_data="language_settings")],
        [InlineKeyboardButton("📝 Оставить фидбэк" if user_data["language"] == "ru" else "📝 Leave feedback", 
                            callback_data="leave_feedback")],
    ]
    
    # Добавляем кнопку админ-панели только для суперадминов
    if user_id in SUPER_ADMINS:
        keyboard.append([InlineKeyboardButton("👑 Админ-панель" if user_data["language"] == "ru" else "👑 Admin panel", 
                                           callback_data="admin_panel")])
    
    keyboard.append([InlineKeyboardButton("↩️ Назад в меню" if user_data["language"] == "ru" else "↩️ Back to menu", 
                                       callback_data="back_to_menu")])
    
    help_text = (
        "📌 Возможности бота:\n\n"
        "• 📋 Посмотреть задания своей группы\n"
        "• ➕ Добавить задание (для кураторов)\n"
        "• 🗑️ Удалить задание (для кураторов)\n"
        "• 🗓️ Данные берутся из Google Таблицы\n"
        "• 🔔 Напоминания о заданиях\n"
        "• 👥 Выбор/изменение группы\n"
        "• 📝 Отправить отзыв разработчику\n"
        "• 🔒 Доступ к изменению только у кураторов" 
        if user_data["language"] == "ru" else 
        "📌 Bot features:\n\n"
        "• 📋 View tasks for your group\n"
        "• ➕ Add task (for curators)\n"
        "• 🗑️ Delete task (for curators)\n"
        "• 🗓️ Data is taken from Google Sheets\n"
        "• 🔔 Task reminders\n"
        "• 👥 Select/change group\n"
        "• 📝 Send feedback to developer\n"
        "• 🔒 Only curators can make changes"
    )
    
    await query.edit_message_text(
        help_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def callback_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-панель для суперадмина"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return
        
    user_data = get_user_data(user_id)
    
    await query.edit_message_text(
        "👑 *АДМИН-ПАНЕЛЬ*\n\n"
        "Выберите действие:" 
        if user_data["language"] == "ru" else 
        "👑 *ADMIN PANEL*\n\n"
        "Choose an action:",
        reply_markup=admin_keyboard(user_data["language"]),
        parse_mode='Markdown'
    )

# ==================== СИСТЕМА КУРАТОРОВ ====================
async def admin_make_curator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать процесс назначения куратора"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return
        
    user_data = get_user_data(user_id)
    
    await query.edit_message_text(
        "👥 *Назначение куратора*\n\n"
        "Введите user_id пользователя (только цифры):\n\n"
        "Как получить user_id:\n"
        "1. Попросите пользователя написать /start боту\n"
        "2. Скопируйте цифры из его профиля Telegram\n"
        "3. Отправьте мне эти цифры" 
        if user_data["language"] == "ru" else 
        "👥 *Make Curator*\n\n"
        "Enter user_id (numbers only):\n\n"
        "How to get user_id:\n"
        "1. Ask user to type /start to the bot\n"
        "2. Copy numbers from their Telegram profile\n"
        "3. Send me these numbers",
        parse_mode='Markdown'
    )
    
    return WAITING_FOR_CURATOR_ID

async def handle_curator_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного user_id куратора"""
    user_id = update.effective_user.id
    
    if user_id not in SUPER_ADMINS:
        await update.message.reply_text("❌ Доступ запрещен")
        return ConversationHandler.END
        
    user_input = update.message.text.strip()
    
    try:
        curator_id = int(user_input)
        
        # Проверяем что пользователь есть в системе
        users = gsh.get_sheet_data("Users")
        user_exists = any(str(curator_id) == row[0] for row in users if len(row) > 0)
        
        if not user_exists:
            await update.message.reply_text(
                "❌ Пользователь не найден в системе.\n"
                "Попросите его сначала написать /start боту."
            )
            return ConversationHandler.END
        
        # Назначаем куратором
        success = update_user_data(curator_id, "is_curator", True)
        
        if success:
            await update.message.reply_text(
                f"✅ Пользователь {curator_id} теперь куратор!\n\n"
                "Бот автоматически запросит у него название группы."
            )
            
            # Отправляем уведомление новому куратору
            try:
                await context.bot.send_message(
                    curator_id,
                    "🎉 *ВЫ НАЗНАЧЕНЫ КУРАТОРОМ!*\n\n"
                    "Пожалуйста, введите название вашей группы:\n"
                    "• Например: B-13, M-22, A-24\n"
                    "• Только латинские буквы и цифры\n"
                    "• Формат: Буква-Цифры (B-13)",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Error notifying curator {curator_id}: {e}")
                await update.message.reply_text(
                    f"✅ Куратор назначен, но не удалось отправить уведомление.\n"
                    f"Попросите его ввести название группы через бота."
                )
        else:
            await update.message.reply_text("❌ Ошибка при назначении куратора")
            
    except ValueError:
        await update.message.reply_text("❌ user_id должен состоять только из цифр")
    
    return ConversationHandler.END

async def handle_group_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода названия группы от куратора"""
    user_id = update.effective_user.id
    group_name = update.message.text.strip().upper()  # Приводим к верхнему регистру
    
    user_data = get_user_data(user_id)
    if not user_data.get("is_curator", False):
        await update.message.reply_text("❌ У вас нет прав куратора")
        return
    
    # Валидация названия группы
    if not re.match(r'^[A-Z]-\d{2,3}$', group_name):
        await update.message.reply_text(
            "❌ Неверный формат группы!\n\n"
            "Используйте формат: *Буква-Цифры*\n"
            "Пример: B-13, M-21, A-105\n\n"
            "Пожалуйста, введите еще раз:",
            parse_mode='Markdown'
        )
        return
    
    # Архивируем старый лист если он есть
    old_group = user_data.get("group")
    if old_group and old_group in gsh.sheets:
        gsh.archive_worksheet(old_group)
    
    # Создаем новый лист
    try:
        gsh.create_worksheet(group_name)
        
        # Устанавливаем группу куратору
        update_user_data(user_id, "group", group_name)
        
        await update.message.reply_text(
            f"✅ *Группа {group_name} установлена!*\n\n"
            f"Лист '{group_name}' создан в таблице.\n"
            f"Старые данные архивированы.\n\n"
            "Теперь вам доступны:\n"
            "• 📝 Добавление заданий\n"
            "• 🗑️ Удаление заданий\n"
            "• 👥 Просмотр заданий вашей группы",
            parse_mode='Markdown',
            reply_markup=main_menu_keyboard(user_data["language"], True)
        )
        
    except Exception as e:
        logger.error(f"Error creating worksheet: {e}")
        await update.message.reply_text(
            "❌ Ошибка при создании листа. Попробуйте другое название группы."
        )

async def admin_list_curators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список всех кураторов"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return
        
    user_data = get_user_data(user_id)
    curators = get_all_curators()
    
    if not curators:
        await query.edit_message_text("📋 Список кураторов пуст")
        return
    
    response = "📋 *СПИСОК КУРАТОРОВ:*\n\n" if user_data["language"] == "ru" else "📋 *CURATORS LIST:*\n\n"
    
    for curator in curators:
        status = f"Группа: {curator['group']}" if curator['group'] else "Группа не установлена"
        response += f"• ID: {curator['user_id']} | {status}\n"
    
    await query.edit_message_text(response, parse_mode='Markdown')

async def admin_new_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск нового семестра"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return
        
    user_data = get_user_data(user_id)
    
    # Подтверждение
    confirm_keyboard = [
        [InlineKeyboardButton("✅ Да, начать новый семестр" if user_data["language"] == "ru" else "✅ Yes, start new semester", callback_data="confirm_new_semester")],
        [InlineKeyboardButton("❌ Отмена" if user_data["language"] == "ru" else "❌ Cancel", callback_data="admin_panel")]
    ]
    
    await query.edit_message_text(
        "🎓 *НОВЫЙ СЕМЕСТР*\n\n"
        "Это действие:\n"
        "• Архивирует все текущие листы групп\n"
        "• Сбросит группы у всех кураторов\n"
        "• Попросит кураторов ввести новые названия групп\n"
        "• Создаст новые чистые листы\n\n"
        "Продолжить?" 
        if user_data["language"] == "ru" else 
        "🎓 *NEW SEMESTER*\n\n"
        "This action will:\n"
        "• Archive all current group sheets\n"
        "• Reset groups for all curators\n"
        "• Ask curators to enter new group names\n"
        "• Create new clean sheets\n\n"
        "Continue?",
        reply_markup=InlineKeyboardMarkup(confirm_keyboard),
        parse_mode='Markdown'
    )

async def confirm_new_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение начала нового семестра"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return
        
    user_data = get_user_data(user_id)
    
    try:
        # Архивируем все активные листы групп
        curators = get_all_curators()
        archived_count = 0
        notified_count = 0
        
        for curator in curators:
            if curator['group'] and curator['group'] in gsh.sheets:
                if gsh.archive_worksheet(curator['group']):
                    archived_count += 1
                # Сбрасываем группу у куратора
                update_user_data(int(curator['user_id']), "group", "")
        
        # Уведомляем всех кураторов
        for curator in curators:
            try:
                await context.bot.send_message(
                    int(curator['user_id']),
                    "🎓 *НОВЫЙ СЕМЕСТР!*\n\n"
                    "Данные прошлого семестра архивированы.\n"
                    "Пожалуйста, введите новое название вашей группы:\n"
                    "• Например: B-23, M-22, A-24\n"
                    "• Формат: Буква-Цифры (B-13)\n\n"
                    "Просто введите название группы в чат:",
                    parse_mode='Markdown'
                )
                notified_count += 1
            except Exception as e:
                logger.error(f"Error notifying curator {curator['user_id']}: {e}")
        
        await query.edit_message_text(
            f"✅ *Новый семестр запущен!*\n\n"
            f"• Архивировано листов: {archived_count}\n"
            f"• Уведомлено кураторов: {notified_count}/{len(curators)}\n\n"
            "Все кураторы получили запрос на ввод новых названий групп.",
            parse_mode='Markdown',
            reply_markup=admin_keyboard(user_data["language"])
        )
        
    except Exception as e:
        logger.error(f"Error starting new semester: {e}")
        await query.edit_message_text(
            "❌ Ошибка при запуске нового семестра",
            reply_markup=admin_keyboard(user_data["language"])
        )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return
        
    user_data = get_user_data(user_id)
    
    try:
        users = gsh.get_sheet_data("Users")
        total_users = len(users) - 1  # minus header
        curators = get_all_curators()
        active_curators = sum(1 for c in curators if c['group'])
        
        response = (
            f"📊 *СТАТИСТИКА БОТА*\n\n"
            f"• Всего пользователей: {total_users}\n"
            f"• Кураторов: {len(curators)}\n"
            f"• Активных кураторов (с группой): {active_curators}\n"
            f"• Всего листов: {len(gsh.sheets)}\n\n"
            f"*Группы с заданиями:*\n"
        )
        
        # Считаем задания по группам
        group_stats = {}
        for sheet_name in gsh.sheets:
            if not sheet_name.endswith('Archive') and sheet_name != 'Users':
                data = gsh.get_sheet_data(sheet_name)
                task_count = len(data) - 1  # minus header
                group_stats[sheet_name] = task_count
        
        for group, count in group_stats.items():
            response += f"• {group}: {count} заданий\n"
            
        if not group_stats:
            response += "Пока нет активных групп с заданиями"
        
        await query.edit_message_text(response, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await query.edit_message_text("❌ Ошибка при получении статистики")

# ==================== СИСТЕМА ЗАДАНИЙ ====================
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
                    # Пропускаем пустые строки
                    if not row[0] or not row[4]:
                        continue
                        
                    # Проверяем что дата актуальная
                    day, month = map(int, row[4].split('.'))
                    current_date = datetime.now(MOSCOW_TZ)
                    
                    # Если дата уже прошла в этом году, пропускаем
                    proposed_date = datetime(current_date.year, month, day)
                    if proposed_date.date() < current_date.date():
                        continue
                    
                    # Проверяем дедлайн
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
                
                time_display = row[5]
                book_icon = "📖" if len(row) > 7 and row[7] == "open-book" else "📕"
                
                details = ""
                if len(row) > 8 and row[8] and row[8].strip() and row[8] not in ["не выбраны", "not selected"]:
                    details = f" | {row[8]}\n"
                
                response += (
                    f"📚 *{row[0]}* — {row[1]} {book_icon} | {row[2]}\n"
                    f"📅 {row[4]} | 🕒 {time_display} | *{row[3]}* баллов курса\n" 
                    f"{details}\n"
                    if user_data["language"] == "ru" else
                    f"📚 *{row[0]}* — {row[1]} {book_icon} ({row[2]})\n"                   
                    f"📅 {row[4]} | 🕒 {time_display} | *{row[3]}* course points\n"
                    f"{details}\n"
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
            reply_markup = main_menu_keyboard(user_data["language"], user_data["is_curator"])

        await query.edit_message_text(response, parse_mode='Markdown', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка при получении заданий: {e}")
        user_data = get_user_data(query.from_user.id)
        await query.edit_message_text(
            f"⛔ Ошибка при получении заданий: {str(e)}" 
            if user_data["language"] == "ru" else 
            f"⛔ Error getting tasks: {str(e)}",
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
        
async def callback_get_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить данные о заданиях"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(user_id)

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
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
        
        if user_data["reminders_enabled"]:
            await schedule_reminders_for_user(context.application.job_queue, user_id)
    else:
        user_data = get_user_data(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при установке группы." 
            if user_data["language"] == "ru" else 
            "⛔ An error occurred while setting the group.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))

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
    """Добавить задание"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(user_id)

    # Проверяем права куратора
    if not user_data.get("is_curator", False):
        await query.edit_message_text(
            "⛔ У вас нет доступа к добавлению заданий." if user_data["language"] == "ru" else "⛔ You don't have access to add tasks.",
            reply_markup=main_menu_keyboard(user_data["language"], False))
        return ConversationHandler.END

    # Проверяем что куратор установил группу
    if not user_data.get("group"):
        await query.edit_message_text(
            "📝 Сначала введите название вашей группы:\n"
            "Например: B-13, M-21"
        )
        return ConversationHandler.END

    context.user_data["task_data"] = {
        "group": user_data["group"],
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
                reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
        except Exception as e:
            logger.error(f"Ошибка при сохранении задания: {e}")
            await query.edit_message_text(
                f"⛔ Произошла ошибка при сохранении: {str(e)}" if user_data["language"] == "ru" else f"⛔ Error saving: {str(e)}",
                reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
        return ConversationHandler.END
    elif query.data == "cancel_task":
        context.user_data.clear()
        await query.edit_message_text(
            "🚫 Добавление задания отменено." if user_data["language"] == "ru" else "🚫 Task addition canceled.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
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
                    "⚠️ Неверный формат даты. Введите дату в формате ДД.ММ (например, 15.12)" if user_data["language"] == "ru" else 
                    "⚠️ Wrong date format. Enter date in DD.MM format (e.g., 15.12)")
                return WAITING_FOR_INPUT
        except:
            await update.message.reply_text(
                "⚠️ Неверный формат даты. Введите дату в формате ДД.ММ (например, 15.12)" if user_data["language"] == "ru" else 
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
    """Удалить задание"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(user_id)

    # Проверяем права куратора
    if not user_data.get("is_curator", False):
        await query.edit_message_text(
            "⛔ У вас нет доступа к удалению заданий." if user_data["language"] == "ru" else "⛔ You don't have access to delete tasks.",
            reply_markup=main_menu_keyboard(user_data["language"], False))
        return ConversationHandler.END

    # Проверяем что куратор установил группу
    if not user_data.get("group"):
        await query.edit_message_text(
            "📝 Сначала введите название вашей группы:\n"
            "Например: B-13, M-21"
        )
        return ConversationHandler.END

    await show_tasks_for_group(query, user_data["group"], show_delete_buttons=True)
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
                    reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
                
                # Обновляем напоминания для всех пользователей группы
                await refresh_reminders_for_group(context.application.job_queue, group)
            else:
                await query.edit_message_text(
                    "⛔ Задание уже было удалено" if user_data["language"] == "ru" else "⛔ Task was already deleted",
                    reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
        except Exception as e:
            logger.error(f"Ошибка при удалении задания: {e}")
            await query.edit_message_text(
                f"⛔ Ошибка при удалении: {str(e)}" if user_data["language"] == "ru" else f"⛔ Error deleting: {str(e)}",
                reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
    
    return ConversationHandler.END

# ==================== СИСТЕМА НАПОМИНАНИЙ ====================
async def callback_reminder_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    try:
        keyboard = [
            [InlineKeyboardButton(
                "🔔 Напоминания: Вкл" if user_data["reminders_enabled"] else "🔔 Напоминания: Выкл",
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
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))

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
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
    except Exception as e:
        logger.error(f"Ошибка в toggle_reminders: {e}")
        await query.edit_message_text(
            "⛔ Произошла ошибка при изменении настроек." if user_data["language"] == "ru" else "⛔ Error changing settings.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))

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
                    # Пропускаем пустые строки
                    if not row[0] or not row[4]:
                        continue
                        
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
            
            # Планирование на REMINDER_TIME по МСК
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
            logger.info(f"Scheduled reminders for user {user_id} at {REMINDER_TIME}")

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
                f"{details}"  # Детали только если есть
                if user_data["language"] == "ru" else
                f"{book_icon} *{task['subject']}* — {task['task_type']} ({task['format']})\n"                   
                f"📅 {task['date']} | 🕒 {time_display} | *{task['max_points']}* course points\n"
                f"{details}"  # Детали только если есть
            )
    
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode='Markdown'
        )
        logger.info(f"Sent daily reminder to user {user_id}")
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
        logger.info(f"Refreshed reminders for group {group}")
    except Exception as e:
        logger.error(f"Ошибка в refresh_reminders_for_group: {e}")

async def check_reminders_now(context: ContextTypes.DEFAULT_TYPE):
    """Проверить и отправить напоминания прямо сейчас"""
    try:
        users = gsh.get_sheet_data("Users")
        for row in users[1:]:
            if len(row) > 2 and row[2].lower() == 'true':
                user_id = int(row[0])
                await schedule_reminders_for_user(context.application.job_queue, user_id)
        logger.info("Checked reminders for all users")
    except Exception as e:
        logger.error(f"Ошибка в check_reminders_now: {e}")

# ==================== СИСТЕМА ЯЗЫКА ====================
async def callback_language_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="set_lang_ru")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en")],
        [InlineKeyboardButton("↩️ Назад" if user_data["language"] == "ru" else "↩️ Back", callback_data="back_to_menu")]
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
                reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
    except Exception as e:
        logger.error(f"Ошибка при изменении языка: {e}")
        user_data = get_user_data(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при изменении языка." if user_data["language"] == "ru" else "⛔ Error changing language.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))

# ==================== СИСТЕМА ОБРАТНОЙ СВЯЗИ ====================
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
                reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
        else:
            await update.message.reply_text(
                "⛔ Не удалось сохранить отзыв. Попробуйте позже." if user_data["language"] == "ru" else 
                "⛔ Failed to save feedback. Please try again later.",
                reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
    except Exception as e:
        logger.error(f"Ошибка при сохранении фидбэка: {e}")
        await update.message.reply_text(
            "⛔ Произошла ошибка при сохранении отзыва." if user_data["language"] == "ru" else 
            "⛔ An error occurred while saving feedback.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
    
    return ConversationHandler.END

async def cancel_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_user_data(query.from_user.id)
    
    await query.edit_message_text(
        "🚫 Отправка отзыва отменена." if user_data["language"] == "ru" else "🚫 Feedback submission canceled.",
        reply_markup=main_menu_keyboard(user_data["language"], user_data["is_curator"]))
    return ConversationHandler.END

# ==================== ОСНОВНАЯ ФУНКЦИЯ ====================
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable not set")
        return
    
    application = Application.builder().token(token).build()

    # Основные обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_get_data, pattern="get_data"))
    application.add_handler(CallbackQueryHandler(callback_help, pattern="help"))
    application.add_handler(CallbackQueryHandler(callback_back_to_menu, pattern="back_to_menu"))
    application.add_handler(CallbackQueryHandler(callback_select_group, pattern="select_group"))
    application.add_handler(CallbackQueryHandler(set_user_group, pattern="^set_group_B-11$|^set_group_B-12$"))
    application.add_handler(CallbackQueryHandler(callback_admin_panel, pattern="admin_panel"))

    # Обработчики настроек
    application.add_handler(CallbackQueryHandler(callback_reminder_settings, pattern="reminder_settings"))
    application.add_handler(CallbackQueryHandler(toggle_reminders, pattern="toggle_reminders"))
    application.add_handler(CallbackQueryHandler(callback_language_settings, pattern="language_settings"))
    application.add_handler(CallbackQueryHandler(set_user_language, pattern="^set_lang_ru$|^set_lang_en$"))

    # Обработчики админ-панели
    application.add_handler(CallbackQueryHandler(admin_make_curator, pattern="admin_make_curator"))
    application.add_handler(CallbackQueryHandler(admin_list_curators, pattern="admin_list_curators"))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern="admin_stats"))
    application.add_handler(CallbackQueryHandler(admin_new_semester, pattern="admin_new_semester"))
    application.add_handler(CallbackQueryHandler(confirm_new_semester, pattern="confirm_new_semester"))

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

    # Обработчик для назначения кураторов
    curator_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_make_curator, pattern="admin_make_curator")],
        states={
            WAITING_FOR_CURATOR_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_curator_id)],
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
    )

    # Обработчик для ввода группы
    group_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_input)

    application.add_handler(add_task_handler)
    application.add_handler(delete_task_handler)
    application.add_handler(feedback_handler)
    application.add_handler(curator_handler)
    application.add_handler(group_handler)
    
    # Настраиваем периодическую проверку напоминаний
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(
            check_reminders_now,
            interval=timedelta(minutes=REMINDER_CHECK_INTERVAL),
            first=10
        )
    
    logger.info("Bot started successfully!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
