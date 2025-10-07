import os
import json
import gspread
import re
import logging
import time
import asyncio
from typing import Dict, List, Any, Optional
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
COURSES = {
    "1": "1 курс",
    "2": "2 курс", 
    "3": "3 курс",
    "4": "4 курс"
}

# Стейты для ConversationHandler
(
    EDITING_TASK, WAITING_FOR_INPUT, WAITING_FOR_FEEDBACK,
    WAITING_FOR_USERNAME, CONFIRM_CURATOR,
    NEW_CURATOR_COURSE, NEW_CURATOR_GROUP,
    CHOOSING_COURSE_FOR_GROUP,
    PROFESSOR_SELECT_COURSE, PROFESSOR_SELECT_GROUP
) = range(10)

# Языки
LANGUAGES = {"ru": "Русский", "en": "English"}

# Суперадмины (только твой user_id)
SUPER_ADMINS = [1062616885]  # Замени на свой user_id

# ==================== КЭШ ====================
SHEETS_CACHE = {}
USERS_CACHE = {}
CACHE_TTL = 300  # 5 минут

def get_cached_sheet_data(sheet_name: str) -> List[List[str]]:
    """Получить данные листа из кэша или из Google Sheets."""
    global SHEETS_CACHE
    current_time = time.time()

    if (sheet_name in SHEETS_CACHE and
            current_time - SHEETS_CACHE[sheet_name]['timestamp'] < CACHE_TTL):
        return SHEETS_CACHE[sheet_name]['data']

    try:
        data = gsh.get_sheet_data(sheet_name)
        SHEETS_CACHE[sheet_name] = {
            'data': data,
            'timestamp': current_time
        }
        return data
    except Exception as e:
        logger.error(f"Error getting sheet data for cache: {e}")
        return []

def invalidate_sheet_cache(sheet_name: str = None):
    """Сбросить кэш для конкретного листа или всего кэша."""
    global SHEETS_CACHE
    if sheet_name:
        SHEETS_CACHE.pop(sheet_name, None)
        logger.info(f"Cache invalidated for sheet: {sheet_name}")
    else:
        SHEETS_CACHE = {}
        logger.info("Full sheets cache invalidated")

def get_cached_user_data(user_id: int) -> Dict[str, Any]:
    """Получить данные пользователя из кэша или из Google Sheets."""
    global USERS_CACHE
    user_id_str = str(user_id)

    if user_id_str in USERS_CACHE:
        return USERS_CACHE[user_id_str]

    user_data = get_user_data(user_id)
    USERS_CACHE[user_id_str] = user_data
    return user_data

def update_cached_user_data(user_id: int, field: str, value: Any):
    """Обновить данные пользователя в кэше и в Google Sheets."""
    global USERS_CACHE
    user_id_str = str(user_id)

    success = update_user_data(user_id, field, value)

    if success:
        if user_id_str in USERS_CACHE:
            USERS_CACHE[user_id_str][field] = value
        else:
            USERS_CACHE[user_id_str] = get_user_data(user_id)

    return success

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
            logger.info(f"Loaded sheets: {list(self.sheets.keys())}")
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
                
                invalidate_sheet_cache(sheet_name)
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

            if group_name in self.sheets:
                return self.sheets[group_name]

            worksheet = spreadsheet.add_worksheet(title=group_name, rows="100", cols="20")

            headers = ["Subject", "Task Type", "Format", "Max Points", "Date", "Time", "Group", "Book Type", "Details"]
            worksheet.append_row(headers)

            self.sheets[group_name] = worksheet
            invalidate_sheet_cache()

            logger.info(f"Created new worksheet: {group_name}")
            return worksheet

        except Exception as e:
            logger.error(f"Error creating worksheet {group_name}: {e}")
            raise

    def delete_worksheet(self, group_name):
        """Удалить лист группы"""
        try:
            if group_name not in self.sheets:
                return False

            spreadsheet = self.client.open("GSOM-PLANNER")
            worksheet = self.sheets[group_name]
            
            spreadsheet.del_worksheet(worksheet)
            
            del self.sheets[group_name]
            invalidate_sheet_cache()
            
            logger.info(f"Deleted worksheet: {group_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting worksheet {group_name}: {e}")
            return False

    def archive_worksheet(self, group_name):
        """Архивировать лист (переименовать)"""
        try:
            if group_name not in self.sheets:
                return False

            spreadsheet = self.client.open("GSOM-PLANNER")
            worksheet = self.sheets[group_name]

            archive_name = f"{group_name}_Archive_{datetime.now().strftime('%Y_%m')}"
            worksheet.update_title(archive_name)

            del self.sheets[group_name]
            self.sheets[archive_name] = worksheet
            invalidate_sheet_cache()

            logger.info(f"Archived worksheet: {group_name} -> {archive_name}")
            return True

        except Exception as e:
            logger.error(f"Error archiving worksheet {group_name}: {e}")
            return False

    def delete_row(self, sheet_name, row_index):
        """Удалить строку из листа"""
        try:
            sheet = self.sheets[sheet_name]
            sheet.delete_rows(row_index)
            invalidate_sheet_cache(sheet_name)
            return True
        except Exception as e:
            logger.error(f"Error deleting row from {sheet_name}: {e}")
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

        day, month = map(int, date_str.split('.'))

        current_date = datetime.now(MOSCOW_TZ)
        year = current_date.year

        proposed_date = datetime(year, month, day)
        if proposed_date.date() < current_date.date():
            year += 1

        dt = datetime(year, month, day)

        if ':' in start_time and start_time not in ["schedule", "По расписанию"]:
            hours, minutes = map(int, start_time.split(':'))
            dt = dt.replace(hour=hours, minute=minutes)
        else:
            dt = dt.replace(hour=23, minute=59)

        return MOSCOW_TZ.localize(dt)
    except ValueError as e:
        logger.error(f"Ошибка преобразования времени: {e}")
        return None

def get_user_data(user_id):
    """Получить данные пользователя из таблицы"""
    try:
        users = get_cached_sheet_data("Users")
        user_row = next((row for row in users if row and len(row) > 0 and str(user_id) == row[0]), None)
        if user_row:
            return {
                "group": user_row[1] if len(user_row) > 1 and user_row[1] != "" else None,
                "reminders_enabled": len(user_row) > 2 and user_row[2].lower() == 'true',
                "language": user_row[3] if len(user_row) > 3 and user_row[3] in LANGUAGES else "ru",
                "feedback": user_row[4] if len(user_row) > 4 else "",
                "is_curator": len(user_row) > 5 and user_row[5].lower() == 'true',
                "course": user_row[6] if len(user_row) > 6 else None,
                "is_professor": len(user_row) > 7 and user_row[7].lower() == 'true'
            }
    except Exception as e:
        logger.error(f"Error getting user data: {e}")
    return {"group": None, "reminders_enabled": True, "language": "ru", "feedback": "", "is_curator": False, "course": None, "is_professor": False}

def update_user_data(user_id, field, value):
    """Обновить данные пользователя"""
    try:
        users = gsh.get_sheet_data("Users")
        user_row_idx = next((i for i, row in enumerate(users) if len(row) > 0 and str(user_id) == row[0]), None)

        if user_row_idx is not None:
            col_idx = {
                "group": 1,
                "reminders_enabled": 2,
                "language": 3,
                "feedback": 4,
                "is_curator": 5,
                "course": 6,
                "is_professor": 7
            }.get(field, 1)

            gsh.sheets["Users"].update_cell(user_row_idx + 1, col_idx + 1, str(value))
            
            global USERS_CACHE
            user_id_str = str(user_id)
            if user_id_str in USERS_CACHE:
                USERS_CACHE[user_id_str][field] = value
                
            return True
    except Exception as e:
        logger.error(f"Error updating user data: {e}")
    return False

def add_new_user(user_id):
    """Добавить нового пользователя в таблицу"""
    try:
        users = gsh.get_sheet_data("Users")
        if any(str(user_id) == row[0] for row in users if len(row) > 0):
            return True

        new_user = [str(user_id), "", "TRUE", "ru", "", "FALSE", "", "FALSE"]
        gsh.update_sheet("Users", new_user)
        
        global USERS_CACHE
        USERS_CACHE.pop(str(user_id), None)
        
        return True
    except Exception as e:
        logger.error(f"Error adding new user: {e}")
        return False

def get_all_curators():
    """Получить список всех кураторов"""
    try:
        users = get_cached_sheet_data("Users")
        curators = []
        for row in users[1:]:
            if len(row) > 5 and row[5].lower() == 'true':
                curators.append({
                    'user_id': row[0],
                    'group': row[1] if len(row) > 1 else '',
                    'language': row[3] if len(row) > 3 else 'ru',
                    'course': row[6] if len(row) > 6 else '',
                    'is_professor': len(row) > 7 and row[7].lower() == 'true'
                })
        return curators
    except Exception as e:
        logger.error(f"Error getting curators: {e}")
        return []

def get_all_professors():
    """Получить список всех профессоров"""
    try:
        users = get_cached_sheet_data("Users")
        professors = []
        for row in users[1:]:
            if len(row) > 7 and row[7].lower() == 'true':
                professors.append({
                    'user_id': row[0],
                    'group': row[1] if len(row) > 1 else '',
                    'language': row[3] if len(row) > 3 else 'ru',
                    'course': row[6] if len(row) > 6 else '',
                    'is_curator': len(row) > 5 and row[5].lower() == 'true'
                })
        return professors
    except Exception as e:
        logger.error(f"Error getting professors: {e}")
        return []

def get_all_groups():
    """Получить список всех групп из всех листов"""
    try:
        all_sheets = list(gsh.sheets.keys())
        groups = [
            sheet for sheet in all_sheets 
            if not (sheet == "Users" or sheet.endswith('Archive') or '_' in sheet)
        ]
        return groups
    except Exception as e:
        logger.error(f"Error getting groups: {e}")
        return []

def get_groups_by_course(course):
    """Получить группы по курсу"""
    try:
        all_groups = get_all_groups()
        course_groups = []
        
        for group in all_groups:
            if group.startswith(f"{course}/"):
                course_groups.append(group)
        
        return course_groups
    except Exception as e:
        logger.error(f"Error getting groups for course {course}: {e}")
        return []

def can_edit_tasks(user_data):
    """Проверяет, может ли пользователь редактировать задания"""
    return user_data.get("is_curator", False) or user_data.get("is_professor", False) or user_data.get("user_id") in SUPER_ADMINS

def format_time_display(time_str, user_lang="ru"):
    """Форматирование отображения времени"""
    if time_str == "schedule":
        return "По расписанию" if user_lang == "ru" else "By schedule"
    return time_str

# ==================== КЛАВИАТУРЫ ====================
def main_menu_keyboard(user_lang="ru", user_data=None):
    """Клавиатура главного меню"""
    if user_data is None:
        user_data = {}
    
    is_curator = user_data.get("is_curator", False)
    is_professor = user_data.get("is_professor", False)
    can_edit = can_edit_tasks(user_data)
    
    if can_edit:
        keyboard = [
            [InlineKeyboardButton(
                "📚 Посмотреть задания" if user_lang == "ru" else "📚 View tasks", 
                callback_data="get_data")],
            [
                InlineKeyboardButton(
                    "⚡ Добавить задание" if user_lang == "ru" else "⚡ Add task", 
                    callback_data="add_task"),
                InlineKeyboardButton(
                    "💣 Удалить задание" if user_lang == "ru" else "💣 Delete task", 
                    callback_data="delete_task")
            ],
            [
                InlineKeyboardButton(
                    "⚙️ Функционал" if user_lang == "ru" else "⚙️ Features", 
                    callback_data="help")
            ]
        ]
        
        # Для профессоров добавляем кнопку выбора группы
        if is_professor and not is_curator:
            keyboard.insert(1, [InlineKeyboardButton(
                "🏫 Выбор группы" if user_lang == "ru" else "🏫 Select group", 
                callback_data="select_group")])
    else:
        keyboard = [
            [InlineKeyboardButton(
                "📚 Посмотреть задания" if user_lang == "ru" else "📚 View tasks", 
                callback_data="get_data")],
            [
                InlineKeyboardButton(
                    "🏫 Выбор группы" if user_lang == "ru" else "🏫 Select group", 
                    callback_data="select_group"),
                InlineKeyboardButton(
                    "⚙️ Функционал" if user_lang == "ru" else "⚙️ Features", 
                    callback_data="help")
            ]
        ]

    return InlineKeyboardMarkup(keyboard)

def help_keyboard(user_lang="ru", user_id=None):
    """Клавиатура для раздела помощи/функционала"""
    keyboard = [
        [InlineKeyboardButton(
            "🔔 Настройки напоминаний" if user_lang == "ru" else "🔔 Reminder settings", 
            callback_data="reminder_settings")],
        [InlineKeyboardButton(
            "🌐 Изменить язык" if user_lang == "ru" else "🌐 Change language", 
            callback_data="language_settings")],
        [InlineKeyboardButton(
            "📝 Оставить фидбэк" if user_lang == "ru" else "📝 Leave feedback", 
            callback_data="leave_feedback")],
    ]

    user_data = get_cached_user_data(user_id) if user_id else {"is_curator": False, "is_professor": False}
    if not user_data.get("is_curator", False) and not user_data.get("is_professor", False):
        keyboard.append([InlineKeyboardButton(
            "📢 Стать куратором" if user_lang == "ru" else "📢 Become curator", 
            callback_data="become_curator")])

    if user_id in SUPER_ADMINS:
        keyboard.append([InlineKeyboardButton(
            "👑 Админ-панель" if user_lang == "ru" else "👑 Admin panel", 
            callback_data="admin_panel")])

    keyboard.append([InlineKeyboardButton(
        "↩️ Назад в меню" if user_lang == "ru" else "↩️ Back to menu", 
        callback_data="back_to_menu")])

    return InlineKeyboardMarkup(keyboard)

def admin_keyboard(user_lang="ru"):
    """Клавиатура админ-панели"""
    keyboard = [
        [InlineKeyboardButton("👥 Назначить куратора" if user_lang == "ru" else "👥 Make curator", callback_data="admin_make_curator")],
        [InlineKeyboardButton("👨‍🏫 Назначить профессора" if user_lang == "ru" else "👨‍🏫 Make professor", callback_data="admin_make_professor")],
        [InlineKeyboardButton("📋 Список кураторов" if user_lang == "ru" else "📋 Curators list", callback_data="admin_list_curators")],
        [InlineKeyboardButton("👨‍🏫 Список профессоров" if user_lang == "ru" else "👨‍🏫 Professors list", callback_data="admin_list_professors")],
        [InlineKeyboardButton("📊 Статистика" if user_lang == "ru" else "📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("🎓 Новый учебный год" if user_lang == "ru" else "🎓 New academic year", callback_data="admin_new_year")],
        [InlineKeyboardButton("↩️ Назад" if user_lang == "ru" else "↩️ Back", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def admin_back_keyboard(user_lang="ru"):
    """Клавиатура с кнопкой назад для админ-разделов"""
    keyboard = [
        [InlineKeyboardButton("↩️ Назад" if user_lang == "ru" else "↩️ Back", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def generate_courses_keyboard(user_lang="ru", back_pattern="back_to_menu"):
    """Клавиатура для выбора курса"""
    keyboard = [
        [InlineKeyboardButton("1 курс" if user_lang == "ru" else "1st year", callback_data="course_1")],
        [InlineKeyboardButton("2 курс" if user_lang == "ru" else "2nd year", callback_data="course_2")],
        [InlineKeyboardButton("3 курс" if user_lang == "ru" else "3rd year", callback_data="course_3")],
        [InlineKeyboardButton("4 курс" if user_lang == "ru" else "4th year", callback_data="course_4")],
        [InlineKeyboardButton("↩️ Назад" if user_lang == "ru" else "↩️ Back", callback_data=back_pattern)]
    ]
    return InlineKeyboardMarkup(keyboard)

def curator_welcome_keyboard(user_lang="ru"):
    """Клавиатура для приветствия нового куратора"""
    keyboard = [
        [InlineKeyboardButton(
            "👥 Создать группу" if user_lang == "ru" else "👥 Create group", 
            callback_data="curator_create_group")],
        [InlineKeyboardButton(
            "🏠 В главное меню" if user_lang == "ru" else "🏠 To main menu", 
            callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def professor_welcome_keyboard(user_lang="ru"):
    """Клавиатура для приветствия нового профессора"""
    keyboard = [
        [InlineKeyboardButton(
            "📚 Выбрать группу" if user_lang == "ru" else "📚 Select group", 
            callback_data="professor_select_group")],
        [InlineKeyboardButton(
            "🏠 В главное меню" if user_lang == "ru" else "🏠 To main menu", 
            callback_data="back_to_menu")]
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
         InlineKeyboardButton("По расписанию" if user_lang == "ru" else "By schedule", callback_data="time_schedule")],
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

def back_to_menu_keyboard(user_lang="ru"):
    """Клавиатура с кнопкой возврата в меню"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Назад в меню" if user_lang == "ru" else "🏠 Back to menu", callback_data="back_to_menu")]
    ])

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id

    if not add_new_user(user_id):
        await update.message.reply_text("❌ Ошибка при регистрации. Попробуйте позже.")
        return

    user_data = get_cached_user_data(user_id)

    welcome_text = (
        "👋 Привет! Добро пожаловать в *GSOMPASS бот*.\n\n"
        "Выберите действие ниже:" 
        if user_data["language"] == "ru" else 
        "👋 Hi! Welcome to *GSOMPASS bot*.\n\n"
        "Choose an action below:"
    )

    await update.message.reply_text(
        welcome_text,
        reply_markup=main_menu_keyboard(user_data["language"], user_data),
        parse_mode='Markdown'
    )

async def callback_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню"""
    query = update.callback_query
    await query.answer()
    user_data = get_cached_user_data(query.from_user.id)

    await query.edit_message_text(
        "👋 Вы вернулись в главное меню. Выберите действие:" 
        if user_data["language"] == "ru" else 
        "👋 You're back to the main menu. Choose an action:",
        reply_markup=main_menu_keyboard(user_data["language"], user_data)
    )

async def callback_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать справку"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)

    await query.edit_message_text(
        "📌 Возможности бота:\n\n"
        "• 📋 Посмотреть задания своей группы\n"
        "• ➕ Добавить задание (для кураторов и профессоров)\n"
        "• 🗑️ Удалить задание (для кураторов и профессоров)\n"
        "• 🗓️ Данные берутся из Google Таблицы\n"
        "• 🔔 Напоминания о заданиях\n"
        "• 👥 Выбор/изменение группы\n"
        "• 📝 Отправить отзыв разработчику\n"
        "• 🔒 Доступ к изменению только у кураторов и профессоров" 
        if user_data["language"] == "ru" else 
        "📌 Bot features:\n\n"
        "• 📋 View tasks for your group\n"
        "• ➕ Add task (for curators and professors)\n"
        "• 🗑️ Delete task (for curators and professors)\n"
        "• 🗓️ Data is taken from Google Sheets\n"
        "• 🔔 Task reminders\n"
        "• 👥 Select/change group\n"
        "• 📝 Send feedback to developer\n"
        "• 🔒 Only curators and professors can make changes",
        reply_markup=help_keyboard(user_data["language"], user_id)
    )

async def callback_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-панель для суперадмина"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    user_data = get_cached_user_data(user_id)

    await query.edit_message_text(
        "👑 *АДМИН-ПАНЕЛЬ*\n\n"
        "Выберите действие:" 
        if user_data["language"] == "ru" else 
        "👑 *ADMIN PANEL*\n\n"
        "Choose an action:",
        reply_markup=admin_keyboard(user_data["language"]),
        parse_mode='Markdown'
    )

# ==================== СИСТЕМА КУРАТОРОВ И ПРОФЕССОРОВ ====================
async def admin_make_curator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать процесс назначения куратора по username"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    user_data = get_cached_user_data(user_id)

    await query.edit_message_text(
        "👥 *Назначение куратора*\n\n"
        "Введите @username пользователя (например, @ivanov):\n\n"
        "Пользователь должен был хотя бы раз написать /start боту." 
        if user_data["language"] == "ru" else 
        "👥 *Make Curator*\n\n"
        "Enter user's @username (e.g., @ivanov):\n\n"
        "The user must have started the bot at least once with /start.",
        parse_mode='Markdown',
        reply_markup=admin_back_keyboard(user_data["language"])
    )

    return WAITING_FOR_USERNAME

async def admin_make_professor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать процесс назначения профессора по username"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    user_data = get_cached_user_data(user_id)

    await query.edit_message_text(
        "👨‍🏫 *Назначение профессора*\n\n"
        "Введите @username пользователя (например, @ivanov):\n\n"
        "Пользователь должен был хотя бы раз написать /start боту." 
        if user_data["language"] == "ru" else 
        "👨‍🏫 *Make Professor*\n\n"
        "Enter user's @username (e.g., @ivanov):\n\n"
        "The user must have started the bot at least once with /start.",
        parse_mode='Markdown',
        reply_markup=admin_back_keyboard(user_data["language"])
    )

    context.user_data["appointing_role"] = "professor"
    return WAITING_FOR_USERNAME

async def handle_username_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного username куратора или профессора"""
    user_id = update.effective_user.id

    if user_id not in SUPER_ADMINS:
        await update.message.reply_text("❌ Доступ запрещен")
        return ConversationHandler.END

    username = update.message.text.strip()
    user_data = get_cached_user_data(user_id)

    if username.startswith('@'):
        username = username[1:]

    try:
        target_chat = await context.bot.get_chat(f"@{username}")
        target_id = target_chat.id
        
        users = get_cached_sheet_data("Users")
        user_exists = any(str(target_id) == row[0] for row in users if row and len(row) > 0)

        if not user_exists:
            await update.message.reply_text(
                "❌ Пользователь не найден в системе.\n"
                "Попросите его сначала написать /start боту.",
                reply_markup=admin_back_keyboard(user_data["language"])
            )
            return ConversationHandler.END

        context.user_data["target_id"] = target_id
        context.user_data["target_username"] = username
        context.user_data["target_name"] = f"{target_chat.first_name} {target_chat.last_name or ''}".strip()

        appointing_role = context.user_data.get("appointing_role", "curator")
        
        if appointing_role == "professor":
            confirm_text = (
                f"👤 *Информация о пользователе:*\n\n"
                f"• Имя: {context.user_data['target_name']}\n"
                f"• Username: @{username}\n"
                f"• ID: {target_id}\n\n"
                f"Назначить этого пользователя профессором?" 
                if user_data["language"] == "ru" else
                f"👤 *User Information:*\n\n"
                f"• Name: {context.user_data['target_name']}\n"
                f"• Username: @{username}\n"
                f"• ID: {target_id}\n\n"
                f"Appoint this user as professor?"
            )
            callback_data = "confirm_make_professor"
        else:
            confirm_text = (
                f"👤 *Информация о пользователе:*\n\n"
                f"• Имя: {context.user_data['target_name']}\n"
                f"• Username: @{username}\n"
                f"• ID: {target_id}\n\n"
                f"Назначить этого пользователя куратором?" 
                if user_data["language"] == "ru" else
                f"👤 *User Information:*\n\n"
                f"• Name: {context.user_data['target_name']}\n"
                f"• Username: @{username}\n"
                f"• ID: {target_id}\n\n"
                f"Appoint this user as curator?"
            )
            callback_data = "confirm_make_curator"

        confirm_keyboard = [
            [InlineKeyboardButton("✅ Да, назначить" if user_data["language"] == "ru" else "✅ Yes, appoint", callback_data=callback_data)],
            [InlineKeyboardButton("❌ Отмена" if user_data["language"] == "ru" else "❌ Cancel", callback_data="admin_panel")]
        ]

        await update.message.reply_text(
            confirm_text,
            reply_markup=InlineKeyboardMarkup(confirm_keyboard),
            parse_mode='Markdown'
        )

        return CONFIRM_CURATOR

    except Exception as e:
        logger.error(f"Error getting user by username @{username}: {e}")
        await update.message.reply_text(
            f"❌ Пользователь @{username} не найден или не начинал диалог с ботом.\n"
            "Проверьте правильность username и попросите пользователя написать /start боту." 
            if user_data["language"] == "ru" else
            f"❌ User @{username} not found or hasn't started conversation with bot.\n"
            "Check username correctness and ask user to type /start to the bot.",
            reply_markup=admin_back_keyboard(user_data["language"])
        )
        return ConversationHandler.END

async def confirm_make_curator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение назначения куратора"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    target_id = context.user_data.get("target_id")
    target_username = context.user_data.get("target_username")
    target_name = context.user_data.get("target_name")
    user_data = get_cached_user_data(user_id)

    if not target_id:
        await query.edit_message_text("❌ Ошибка: данные пользователя утеряны")
        return ConversationHandler.END

    # Назначаем куратором
    success = update_cached_user_data(target_id, "is_curator", True)

    if success:
        await query.edit_message_text(
            f"✅ Пользователь {target_name} (@{target_username}) теперь куратор!\n\n"
            "Бот автоматически отправит ему приглашение создать группу." 
            if user_data["language"] == "ru" else
            f"✅ User {target_name} (@{target_username}) is now a curator!\n\n"
            "The bot will automatically send him an invitation to create a group.",
            reply_markup=admin_keyboard(user_data["language"])
        )

        # Отправляем уведомление новому куратору и запускаем процесс создания группы
        try:
            target_data = get_cached_user_data(target_id)
            await context.bot.send_message(
                target_id,
                "🎉 *ВЫ НАЗНАЧЕНЫ КУРАТОРОМ!*\n\n"
                "Теперь вы можете создать группу для ваших студентов.\n"
                "Нажмите кнопку ниже, чтобы начать:" 
                if target_data["language"] == "ru" else
                "🎉 *YOU HAVE BEEN APPOINTED AS A CURATOR!*\n\n"
                "Now you can create a group for your students.\n"
                "Click the button below to get started:",
                reply_markup=curator_welcome_keyboard(target_data["language"]),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error notifying curator {target_id}: {e}")
            await query.edit_message_text(
                f"✅ Куратор назначен, но не удалось отправить уведомление.\n"
                f"Попросите его нажать /start и создать группу через меню." 
                if user_data["language"] == "ru" else
                f"✅ Curator appointed, but failed to send notification.\n"
                f"Ask him to press /start and create group through menu."
            )
    else:
        await query.edit_message_text(
            "❌ Ошибка при назначении куратора" 
            if user_data["language"] == "ru" else
            "❌ Error appointing curator",
            reply_markup=admin_back_keyboard(user_data["language"])
        )

    # Очищаем временные данные
    context.user_data.pop("target_id", None)
    context.user_data.pop("target_username", None)
    context.user_data.pop("target_name", None)
    context.user_data.pop("appointing_role", None)

    return ConversationHandler.END

async def confirm_make_professor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение назначения профессора"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    target_id = context.user_data.get("target_id")
    target_username = context.user_data.get("target_username")
    target_name = context.user_data.get("target_name")
    user_data = get_cached_user_data(user_id)

    if not target_id:
        await query.edit_message_text("❌ Ошибка: данные пользователя утеряны")
        return ConversationHandler.END

    # Назначаем профессором
    success = update_cached_user_data(target_id, "is_professor", True)

    if success:
        await query.edit_message_text(
            f"✅ Пользователь {target_name} (@{target_username}) теперь профессор!\n\n"
            "Бот автоматически отправит ему приглашение выбрать группу." 
            if user_data["language"] == "ru" else
            f"✅ User {target_name} (@{target_username}) is now a professor!\n\n"
            "The bot will automatically send him an invitation to select a group.",
            reply_markup=admin_keyboard(user_data["language"])
        )

        # Отправляем уведомление новому профессору
        try:
            target_data = get_cached_user_data(target_id)
            await context.bot.send_message(
                target_id,
                "🎉 *ВЫ НАЗНАЧЕНЫ ПРОФЕССОРОМ!*\n\n"
                "Теперь вы можете добавлять задания для студентов.\n"
                "Нажмите кнопку ниже, чтобы выбрать группу:" 
                if target_data["language"] == "ru" else
                "🎉 *YOU HAVE BEEN APPOINTED AS A PROFESSOR!*\n\n"
                "Now you can add tasks for students.\n"
                "Click the button below to select a group:",
                reply_markup=professor_welcome_keyboard(target_data["language"]),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error notifying professor {target_id}: {e}")
            await query.edit_message_text(
                f"✅ Профессор назначен, но не удалось отправить уведомление.\n"
                f"Попросите его нажать /start и выбрать группу через меню." 
                if user_data["language"] == "ru" else
                f"✅ Professor appointed, but failed to send notification.\n"
                f"Ask him to press /start and select group through menu."
            )
    else:
        await query.edit_message_text(
            "❌ Ошибка при назначении профессора" 
            if user_data["language"] == "ru" else
            "❌ Error appointing professor",
            reply_markup=admin_back_keyboard(user_data["language"])
        )

    # Очищаем временные данные
    context.user_data.pop("target_id", None)
    context.user_data.pop("target_username", None)
    context.user_data.pop("target_name", None)
    context.user_data.pop("appointing_role", None)

    return ConversationHandler.END

async def become_curator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь хочет стать куратором"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)

    # Проверяем, не является ли уже куратором или профессором
    if user_data.get("is_curator", False) or user_data.get("is_professor", False):
        await query.edit_message_text(
            "ℹ️ Вы уже являетесь куратором или профессором!" 
            if user_data["language"] == "ru" else
            "ℹ️ You are already a curator or professor!",
            reply_markup=main_menu_keyboard(user_data["language"], user_data)
        )
        return ConversationHandler.END

    # Отправляем заявку суперадмину
    try:
        user_chat = await context.bot.get_chat(user_id)
        user_name = f"{user_chat.first_name} {user_chat.last_name or ''}".strip()
        username = f"@{user_chat.username}" if user_chat.username else "нет username"

        # Формируем сообщение для суперадмина
        admin_message = (
            f"📨 *НОВАЯ ЗАЯВКА НА КУРАТОРСТВО*\n\n"
            f"• Пользователь: {user_name}\n"
            f"• Username: {username}\n"
            f"• ID: {user_id}\n\n"
            f"Назначить куратором?"
        )

        admin_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Назначить", callback_data=f"approve_curator_{user_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_curator_{user_id}")]
        ])

        # Отправляем всем суперадминам
        for admin_id in SUPER_ADMINS:
            try:
                await context.bot.send_message(
                    admin_id,
                    admin_message,
                    reply_markup=admin_keyboard,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Error sending curator application to admin {admin_id}: {e}")

        await query.edit_message_text(
            "✅ Ваша заявка отправлена администратору! Ожидайте решения." 
            if user_data["language"] == "ru" else
            "✅ Your application has been sent to administrator! Please wait for approval.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data)
        )

    except Exception as e:
        logger.error(f"Error processing curator application: {e}")
        await query.edit_message_text(
            "❌ Ошибка при отправке заявки. Попробуйте позже." 
            if user_data["language"] == "ru" else
            "❌ Error sending application. Please try again later.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data)
        )

    return ConversationHandler.END

async def handle_curator_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка одобрения/отклонения заявки на кураторство"""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    if admin_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    # Извлекаем action и user_id из callback_data
    action, user_id_str = query.data.split('_')[1:3]
    curator_id = int(user_id_str)
    admin_data = get_cached_user_data(admin_id)

    try:
        if action == "approve":
            # Назначаем куратором
            success = update_cached_user_data(curator_id, "is_curator", True)

            if success:
                # Уведомляем админа
                await query.edit_message_text(
                    "✅ Заявка одобрена! Пользователь назначен куратором." 
                    if admin_data["language"] == "ru" else
                    "✅ Application approved! User appointed as curator.",
                    reply_markup=admin_back_keyboard(admin_data["language"])
                )

                # Уведомляем нового куратора и запускаем процесс создания группы
                curator_data = get_cached_user_data(curator_id)
                await context.bot.send_message(
                    curator_id,
                    "🎉 *ВАША ЗАЯВКА ОДОБРЕНА!*\n\n"
                    "Теперь вы куратор и можете создать группу для ваших студентов.\n"
                    "Нажмите кнопку ниже, чтобы начать:" 
                    if curator_data["language"] == "ru" else
                    "🎉 *YOUR APPLICATION HAS BEEN APPROVED!*\n\n"
                    "Now you are a curator and can create a group for your students.\n"
                    "Click the button below to get started:",
                    reply_markup=curator_welcome_keyboard(curator_data["language"]),
                    parse_mode='Markdown'
                )
            else:
                await query.edit_message_text(
                    "❌ Ошибка при назначении куратора" 
                    if admin_data["language"] == "ru" else
                    "❌ Error appointing curator",
                    reply_markup=admin_back_keyboard(admin_data["language"])
                )

        elif action == "reject":
            # Отклоняем заявку
            await query.edit_message_text(
                "❌ Заявка отклонена." 
                if admin_data["language"] == "ru" else
                "❌ Application rejected.",
                reply_markup=admin_back_keyboard(admin_data["language"])
            )

                        # Уведомляем пользователя об отклонении
            curator_data = get_cached_user_data(curator_id)
            await context.bot.send_message(
                curator_id,
                "❌ Ваша заявка на кураторство была отклонена администратором." 
                if curator_data["language"] == "ru" else
                "❌ Your curator application has been rejected by administrator.",
                reply_markup=main_menu_keyboard(curator_data["language"], curator_data)
            )

    except Exception as e:
        logger.error(f"Error handling curator approval: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при обработке заявки." 
            if admin_data["language"] == "ru" else
            "❌ An error occurred while processing the application.",
            reply_markup=admin_back_keyboard(admin_data["language"])
        )

    return ConversationHandler.END

async def curator_create_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать процесс создания группы куратором"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)

    if not user_data.get("is_curator", False):
        await query.edit_message_text("❌ У вас нет прав куратора")
        return

    await query.edit_message_text(
        "🎓 *СОЗДАНИЕ ГРУППЫ*\n\n"
        "Выберите курс для вашей группы:" 
        if user_data["language"] == "ru" else 
        "🎓 *CREATE GROUP*\n\n"
        "Select the year for your group:",
        reply_markup=generate_courses_keyboard(user_data["language"], "back_to_menu"),
        parse_mode='Markdown'
    )

    return NEW_CURATOR_COURSE

async def professor_select_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать процесс выбора группы профессором"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)

    if not user_data.get("is_professor", False):
        await query.edit_message_text("❌ У вас нет прав профессора")
        return

    await query.edit_message_text(
        "🎓 *ВЫБОР ГРУППЫ*\n\n"
        "Выберите курс:" 
        if user_data["language"] == "ru" else 
        "🎓 *SELECT GROUP*\n\n"
        "Select the year:",
        reply_markup=generate_courses_keyboard(user_data["language"], "back_to_menu"),
        parse_mode='Markdown'
    )

    return PROFESSOR_SELECT_COURSE

async def handle_professor_course_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора курса профессором"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)
    
    course = query.data.replace("course_", "")
    context.user_data["professor_course"] = course
    
    # Получаем все группы этого курса
    course_groups = get_groups_by_course(course)
    
    if not course_groups:
        await query.edit_message_text(
            f"❌ На {COURSES[course]} пока нет групп\n\n"
            "Выберите другой курс:" 
            if user_data["language"] == "ru" else
            f"❌ No groups for {COURSES[course]}\n\n"
            "Select another year:",
            reply_markup=generate_courses_keyboard(user_data["language"], "professor_select_group")
        )
        return
    
    # Убираем префикс курса для отображения
    display_groups = [group.replace(f"{course}/", "") for group in course_groups]
    display_groups.sort()
    
    # Создаем кнопки групп курса
    group_keyboard = []
    row_buttons = []
    
    for group in display_groups:
        full_group_name = f"{course}/{group}"
        row_buttons.append(InlineKeyboardButton(group, callback_data=f"professor_set_group_{full_group_name}"))
        
        if len(row_buttons) == 2:
            group_keyboard.append(row_buttons)
            row_buttons = []
    
    if row_buttons:
        group_keyboard.append(row_buttons)
    
    group_keyboard.append([InlineKeyboardButton(
        "↩️ Назад к курсам" 
        if user_data["language"] == "ru" else 
        "↩️ Back to years", 
        callback_data="professor_select_group")])
    
    await query.edit_message_text(
        f"🎓 {COURSES[course]}\n\n"
        "Выберите группу для добавления заданий:" 
        if user_data["language"] == "ru" else
        f"🎓 {COURSES[course]}\n\n"
        "Select group to add tasks:",
        reply_markup=InlineKeyboardMarkup(group_keyboard)
    )
    
    return PROFESSOR_SELECT_GROUP

async def handle_professor_group_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора группы профессором"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    group = query.data.replace("professor_set_group_", "")

    if update_cached_user_data(user_id, "group", group):
        user_data = get_cached_user_data(user_id)
        await query.edit_message_text(
            f"✅ Группа установлена: {group}\n\n"
            "Теперь вы можете добавлять задания для этой группы." 
            if user_data["language"] == "ru" else 
            f"✅ Group is set: {group}\n\n"
            "Now you can add tasks for this group.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))
    else:
        user_data = get_cached_user_data(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при установке группы." 
            if user_data["language"] == "ru" else 
            "⛔ An error occurred while setting the group.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))

    return ConversationHandler.END

async def handle_new_curator_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора курса новым куратором"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)
    
    course = query.data.replace("course_", "")
    context.user_data["creating_group_course"] = course
    
    await query.edit_message_text(
        f"🎓 Выбран {COURSES[course]}\n\n"
        "Теперь введите название вашей группы:\n"
        "• Можно использовать любое название\n"
        "• Например: Б-18, М-21, А-1, Finance-2023\n\n"
        "*Внимание:* Это название будет видно всем студентам!" 
        if user_data["language"] == "ru" else
        f"🎓 Selected {COURSES[course]}\n\n"
        "Now enter your group name:\n"
        "• Any name is allowed\n"
        "• Example: B-18, M-21, A-1, Finance-2023\n\n"
        "*Note:* This name will be visible to all students!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад к курсам" if user_data["language"] == "ru" else "↩️ Back to years", 
                                callback_data="curator_create_group")]
        ])
    )
    
    return NEW_CURATOR_GROUP

async def handle_new_curator_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода названия группы новым куратором"""
    user_id = update.effective_user.id
    group_name = update.message.text.strip()
    
    user_data = get_cached_user_data(user_id)
    if not user_data.get("is_curator", False):
        await update.message.reply_text("❌ У вас нет прав куратора")
        return ConversationHandler.END

    course = context.user_data.get("creating_group_course")
    if not course:
        await update.message.reply_text("❌ Сначала выберите курс")
        return await curator_create_group(update, context)
    
    # Создаем полное название группы с курсом: "2/Б-18"
    full_group_name = f"{course}/{group_name}"

    # Проверяем, не существует ли уже такая группа
    all_groups = get_all_groups()
    if full_group_name in all_groups:
        await update.message.reply_text(
            f"❌ Группа '{full_group_name}' уже существует!\n"
            "Пожалуйста, выберите другое название." 
            if user_data["language"] == "ru" else
            f"❌ Group '{full_group_name}' already exists!\n"
            "Please choose a different name.",
            reply_markup=curator_welcome_keyboard(user_data["language"])
        )
        return ConversationHandler.END

    # Архивируем старый лист если он есть
    old_group = user_data.get("group")
    if old_group and old_group in gsh.sheets:
        gsh.archive_worksheet(old_group)

    # Создаем новый лист
    try:
        gsh.create_worksheet(full_group_name)

        # Устанавливаем группу и курс куратору
        update_cached_user_data(user_id, "group", full_group_name)
        update_cached_user_data(user_id, "course", course)

        # Отправляем памятку куратору
        curator_guide = (
            "📋 *ПАМЯТКА ДЛЯ КУРАТОРА*\n\n"
            "• ✍️ *Детали задания:* Необязательное поле, можно пропустить\n"
            "• 📖 *Открытая книга:* Нажатие на 📖 означает 'можно пользоваться материалами'\n"
            "• 📕 *Закрытая книга:* Нажатие на 📕 означает 'без материалов'\n"
            "• 💯 *Баллы:* Указываются баллы от курса за это задание\n"
            "• ⏰ *Время:* 'По расписанию' означает дедлайн в конце дня\n\n"
            "Теперь вы можете добавлять задания для вашей группы!"
            if user_data["language"] == "ru" else
            "📋 *CURATOR GUIDE*\n\n"
            "• ✍️ *Details:* Optional field, can be skipped\n"
            "• 📖 *Open book:* Clicking 📖 means 'materials allowed'\n"
            "• 📕 *Closed book:* Clicking 📕 means 'no materials allowed'\n"
            "• 💯 *Points:* Course points for this task\n"
            "• ⏰ *Time:* 'By schedule' means deadline at the end of the day\n\n"
            "Now you can add tasks for your group!"
        )

        await update.message.reply_text(
            f"✅ *Группа создана успешно!*\n\n"
            f"Курс: {COURSES[course]}\n"
            f"Название группы: {group_name}\n"
            f"Полное название: {full_group_name}\n\n"
            "Теперь вам доступны:\n"
            "• 📝 Добавление заданий\n"
            "• 🗑️ Удаление заданий\n"
            "• 👥 Просмотр заданий вашей группы\n\n"
            "Студенты теперь могут найти вашу группу в списке!" 
            if user_data["language"] == "ru" else
            f"✅ *Group created successfully!*\n\n"
            f"Year: {COURSES[course]}\n"
            f"Group name: {group_name}\n"
            f"Full name: {full_group_name}\n\n"
            "Now you have access to:\n"
            "• 📝 Adding tasks\n"
            "• 🗑️ Deleting tasks\n"
            "• 👥 Viewing your group's tasks\n\n"
            "Students can now find your group in the list!",
            parse_mode='Markdown',
            reply_markup=main_menu_keyboard(user_data["language"], user_data)
        )

        # Отправляем памятку отдельным сообщением
        await update.message.reply_text(
            curator_guide,
            parse_mode='Markdown'
        )

        # Инвалидируем кэш групп
        invalidate_sheet_cache()

    except Exception as e:
        logger.error(f"Error creating worksheet: {e}")
        await update.message.reply_text(
            "❌ Ошибка при создании группы. Попробуйте другое название.\n"
            f"Ошибка: {str(e)}" 
            if user_data["language"] == "ru" else
            "❌ Error creating group. Try a different name.\n"
            f"Error: {str(e)}",
            reply_markup=main_menu_keyboard(user_data["language"], user_data)
        )

    # Очищаем временные данные
    context.user_data.pop("creating_group_course", None)
    
    return ConversationHandler.END

async def admin_list_curators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список всех кураторов"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    user_data = get_cached_user_data(user_id)
    curators = get_all_curators()

    if not curators:
        await query.edit_message_text(
            "📋 Список кураторов пуст" 
            if user_data["language"] == "ru" else 
            "📋 Curators list is empty",
            reply_markup=admin_back_keyboard(user_data["language"])
        )
        return

    response = "📋 *СПИСОК КУРАТОРОВ:*\n\n" if user_data["language"] == "ru" else "📋 *CURATORS LIST:*\n\n"

    for curator in curators:
        try:
            curator_chat = await context.bot.get_chat(int(curator['user_id']))
            curator_name = f"{curator_chat.first_name} {curator_chat.last_name or ''}".strip()
            curator_username = f"@{curator_chat.username}" if curator_chat.username else "нет username"
            
            status = f"Группа: {curator['group']}" if curator['group'] else "Группа не создана"
            professor_status = " | 👨‍🏫 Профессор" if curator.get('is_professor') else ""
            response += f"• {curator_name} ({curator_username}) | {status}{professor_status}\n"
        except Exception as e:
            logger.error(f"Error getting chat info for curator {curator['user_id']}: {e}")
            response += f"• ID: {curator['user_id']} | Группа: {curator['group']}\n"

    await query.edit_message_text(
        response, 
        parse_mode='Markdown',
        reply_markup=admin_back_keyboard(user_data["language"])
    )

async def admin_list_professors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список всех профессоров"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    user_data = get_cached_user_data(user_id)
    professors = get_all_professors()

    if not professors:
        await query.edit_message_text(
            "👨‍🏫 Список профессоров пуст" 
            if user_data["language"] == "ru" else 
            "👨‍🏫 Professors list is empty",
            reply_markup=admin_back_keyboard(user_data["language"])
        )
        return

    response = "👨‍🏫 *СПИСОК ПРОФЕССОРОВ:*\n\n" if user_data["language"] == "ru" else "👨‍🏫 *PROFESSORS LIST:*\n\n"

    for professor in professors:
        try:
            professor_chat = await context.bot.get_chat(int(professor['user_id']))
            professor_name = f"{professor_chat.first_name} {professor_chat.last_name or ''}".strip()
            professor_username = f"@{professor_chat.username}" if professor_chat.username else "нет username"
            
            status = f"Группа: {professor['group']}" if professor['group'] else "Группа не выбрана"
            curator_status = " | 👥 Куратор" if professor.get('is_curator') else ""
            response += f"• {professor_name} ({professor_username}) | {status}{curator_status}\n"
        except Exception as e:
            logger.error(f"Error getting chat info for professor {professor['user_id']}: {e}")
            response += f"• ID: {professor['user_id']} | Группа: {professor['group']}\n"

    await query.edit_message_text(
        response, 
        parse_mode='Markdown',
        reply_markup=admin_back_keyboard(user_data["language"])
    )

async def admin_new_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск нового учебного года"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    user_data = get_cached_user_data(user_id)

    # Подтверждение
    confirm_keyboard = [
        [InlineKeyboardButton("✅ Да, начать новый год" if user_data["language"] == "ru" else "✅ Yes, start new year", callback_data="confirm_new_year")],
        [InlineKeyboardButton("❌ Отмена" if user_data["language"] == "ru" else "❌ Cancel", callback_data="admin_panel")]
    ]

    await query.edit_message_text(
        "🎓 *НОВЫЙ УЧЕБНЫЙ ГОД*\n\n"
        "⚠️ *ВНИМАНИЕ:* Это действие:\n"
        "• 🗑️ БЕЗВОЗВРАТНО УДАЛИТ все листы групп\n"
        "• 🔄 Сбросит группы у всех кураторов\n"
        "• 📝 Попросит кураторов создать новые группы\n\n"
        "Продолжить?" 
        if user_data["language"] == "ru" else 
        "🎓 *NEW ACADEMIC YEAR*\n\n"
        "⚠️ *WARNING:* This action will:\n"
        "• 🗑️ PERMANENTLY DELETE all group sheets\n"
        "• 🔄 Reset groups for all curators\n"
        "• 📝 Ask curators to create new groups\n\n"
        "Continue?",
        reply_markup=InlineKeyboardMarkup(confirm_keyboard),
        parse_mode='Markdown'
    )

async def confirm_new_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение начала нового учебного года"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in SUPER_ADMINS:
        await query.edit_message_text("❌ Доступ запрещен")
        return

    user_data = get_cached_user_data(user_id)

    try:
        # Удаляем все активные листы групп
        curators = get_all_curators()
        deleted_count = 0
        notified_count = 0

        for curator in curators:
            if curator['group'] and curator['group'] in gsh.sheets:
                if gsh.delete_worksheet(curator['group']):
                    deleted_count += 1
                # Сбрасываем группу и курс у куратора
                update_cached_user_data(int(curator['user_id']), "group", "")
                update_cached_user_data(int(curator['user_id']), "course", "")

        # Сбрасываем группы у профессоров
        professors = get_all_professors()
        for professor in professors:
            update_cached_user_data(int(professor['user_id']), "group", "")

        # Уведомляем всех кураторов
        for curator in curators:
            try:
                curator_lang = curator.get('language', 'ru')
                await context.bot.send_message(
                    int(curator['user_id']),
                    "🎓 *НОВЫЙ УЧЕБНЫЙ ГОД!*\n\n"
                    "Данные прошлого года удалены.\n"
                    "Пожалуйста, создайте новую группу для текущего учебного года:" 
                    if curator_lang == "ru" else
                    "🎓 *NEW ACADEMIC YEAR!*\n\n"
                    "Last year data has been deleted.\n"
                    "Please create a new group for the current academic year:",
                    reply_markup=curator_welcome_keyboard(curator_lang),
                    parse_mode='Markdown'
                )
                notified_count += 1
            except Exception as e:
                logger.error(f"Error notifying curator {curator['user_id']}: {e}")

        # Уведомляем профессоров
        for professor in professors:
            try:
                professor_lang = professor.get('language', 'ru')
                await context.bot.send_message(
                    int(professor['user_id']),
                    "🎓 *НОВЫЙ УЧЕБНЫЙ ГОД!*\n\n"
                    "Данные прошлого года удалены.\n"
                    "Пожалуйста, выберите группу для текущего учебного года:" 
                    if professor_lang == "ru" else
                    "🎓 *NEW ACADEMIC YEAR!*\n\n"
                    "Last year data has been deleted.\n"
                    "Please select a group for the current academic year:",
                    reply_markup=professor_welcome_keyboard(professor_lang),
                    parse_mode='Markdown'
                )
                notified_count += 1
            except Exception as e:
                logger.error(f"Error notifying professor {professor['user_id']}: {e}")

        await query.edit_message_text(
            f"✅ *Новый учебный год запущен!*\n\n"
            f"• Удалено листов: {deleted_count}\n"
            f"• Уведомлено пользователей: {notified_count}/{len(curators) + len(professors)}\n\n"
            "Все кураторы и профессоры получили запрос на создание/выбор новых групп." 
            if user_data["language"] == "ru" else
            f"✅ *New academic year started!*\n\n"
            f"• Deleted sheets: {deleted_count}\n"
            f"• Notified users: {notified_count}/{len(curators) + len(professors)}\n\n"
            "All curators and professors received a request to create/select new groups.",
            parse_mode='Markdown',
            reply_markup=admin_keyboard(user_data["language"])
        )

    except Exception as e:
        logger.error(f"Error starting new academic year: {e}")
        await query.edit_message_text(
            "❌ Ошибка при запуске нового учебного года" 
            if user_data["language"] == "ru" else
            "❌ Error starting new academic year",
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

    user_data = get_cached_user_data(user_id)

    try:
        users = get_cached_sheet_data("Users")
        total_users = len(users) - 1  # minus header
        curators = get_all_curators()
        professors = get_all_professors()
        active_curators = sum(1 for c in curators if c['group'])
        active_professors = sum(1 for p in professors if p['group'])

        response = (
            f"📊 *СТАТИСТИКА БОТА*\n\n"
            f"• Всего пользователей: {total_users}\n"
            f"• Кураторов: {len(curators)}\n"
            f"• Активных кураторов (с группой): {active_curators}\n"
            f"• Профессоров: {len(professors)}\n"
            f"• Активных профессоров (с группой): {active_professors}\n"
            f"• Всего листов: {len(gsh.sheets)}\n\n"
            f"*Группы с заданиями:*\n"
        )

        # Считаем задания по группам
        group_stats = {}
        for sheet_name in gsh.sheets:
            if not (sheet_name == "Users" or sheet_name.endswith('Archive')):
                data = get_cached_sheet_data(sheet_name)
                task_count = len(data) - 1  # minus header
                if task_count > 0:
                    group_stats[sheet_name] = task_count

        for group, count in group_stats.items():
            response += f"• {group}: {count} заданий\n"

        if not group_stats:
            response += "Пока нет активных групп с заданиями"

        await query.edit_message_text(
            response, 
            parse_mode='Markdown',
            reply_markup=admin_back_keyboard(user_data["language"])
        )

    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await query.edit_message_text(
            "❌ Ошибка при получении статистики" 
            if user_data["language"] == "ru" else
            "❌ Error getting statistics",
            reply_markup=admin_back_keyboard(user_data["language"])
        )

# ==================== СИСТЕМА ВЫБОРА ГРУППЫ ====================
async def callback_select_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор группы - сначала курс, потом группы курса"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)

    # Проверяем, не является ли пользователь куратором
    if user_data.get("is_curator", False):
        await query.edit_message_text(
            "❌ Кураторы не могут выбирать группы. Вы привязаны к своей группе." 
            if user_data["language"] == "ru" else
            "❌ Curators cannot select groups. You are bound to your group.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data)
        )
        return
    
    # Профессоры используют отдельный процесс выбора группы
    if user_data.get("is_professor", False):
        await professor_select_group(update, context)
        return
    
    await query.edit_message_text(
        "🎓 Выберите ваш курс:" 
        if user_data["language"] == "ru" else 
        "🎓 Select your year:",
        reply_markup=generate_courses_keyboard(user_data["language"])
    )

async def handle_course_selection_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора курса студентом"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)
    
    course = query.data.replace("course_", "")
    
    # Получаем все группы этого курса
    course_groups = get_groups_by_course(course)
    
    if not course_groups:
        await query.edit_message_text(
            f"❌ На {COURSES[course]} пока нет групп\n\n"
            "Выберите другой курс:" 
            if user_data["language"] == "ru" else
            f"❌ No groups for {COURSES[course]}\n\n"
            "Select another year:",
            reply_markup=generate_courses_keyboard(user_data["language"])
        )
        return
    
    # Убираем префикс курса для отображения
    display_groups = [group.replace(f"{course}/", "") for group in course_groups]
    display_groups.sort()
    
    # Создаем кнопки групп курса
    group_keyboard = []
    row_buttons = []
    
    for group in display_groups:
        full_group_name = f"{course}/{group}"
        row_buttons.append(InlineKeyboardButton(group, callback_data=f"set_group_{full_group_name}"))
        
        if len(row_buttons) == 2:
            group_keyboard.append(row_buttons)
            row_buttons = []
    
    if row_buttons:
        group_keyboard.append(row_buttons)
    
    group_keyboard.append([InlineKeyboardButton(
        "↩️ Назад к курсам" 
        if user_data["language"] == "ru" else 
        "↩️ Back to years", 
        callback_data="select_group")])
    
    await query.edit_message_text(
        f"🎓 {COURSES[course]}\n\n"
        "Выберите вашу группу:" 
        if user_data["language"] == "ru" else
        f"🎓 {COURSES[course]}\n\n"
        "Select your group:",
        reply_markup=InlineKeyboardMarkup(group_keyboard)
    )

async def set_user_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить группу пользователя"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    group = query.data.replace("set_group_", "")

    if update_cached_user_data(user_id, "group", group):
        user_data = get_cached_user_data(user_id)
        await query.edit_message_text(
            f"✅ Ваша группа установлена: {group}" 
            if user_data["language"] == "ru" else 
            f"✅ Your group is set: {group}",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))

        if user_data["reminders_enabled"]:
            await schedule_reminders_for_user(context.application.job_queue, user_id)
    else:
        user_data = get_cached_user_data(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при установке группы." 
            if user_data["language"] == "ru" else 
            "⛔ An error occurred while setting the group.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))

# ==================== СИСТЕМА ЗАДАНИЙ (С КЭШИРОВАНИЕМ) ====================
async def show_tasks_for_group(query, group, show_delete_buttons=False, user_data=None):
    """Показать задания для группы (использует кэш)"""
    if user_data is None:
        user_data = get_cached_user_data(query.from_user.id)
        
    try:
        data = get_cached_sheet_data(group)
        if not data or len(data) <= 1:
            response = "ℹ️ Пока нет заданий для вашей группы." if user_data["language"] == "ru" else "ℹ️ No tasks for your group yet."
            await query.edit_message_text(
                response,
                reply_markup=back_to_menu_keyboard(user_data["language"])
            )
            return

        data = data[1:]  # Пропускаем заголовок

        response = f"📌 Задания для группы {group}:\n\n" if user_data["language"] == "ru" else f"📌 Tasks for group {group}:\n\n"
        count = 0
        tasks = []

        for idx, row in enumerate(data, start=2):
            if len(row) >= 7 and row[6] == group:
                try:
                    if not row[0] or not row[4]:
                        continue

                    day, month = map(int, row[4].split('.'))
                    current_date = datetime.now(MOSCOW_TZ)

                    proposed_date = datetime(current_date.year, month, day)
                    if proposed_date.date() < current_date.date():
                        continue

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

                time_display = format_time_display(row[5], user_data["language"])
                book_icon = "📖" if len(row) > 7 and row[7] == "open-book" else "📕"

                details = ""
                if len(row) > 8 and row[8] and row[8].strip() and row[8] not in ["не выбраны", "not selected"]:
                    details = f" | {row[8]}\n"

                response += (
                    f"📚 *{row[0]}* — {row[1]} {book_icon} | {row[2]}\n"
                    f"📅 {row[4]} | 🕒 {time_display} | *{row[3]}* баллов курса\n" 
                    f"{details}\n"
                    if user_data["language"] == "ru" else
                    f"📚 *{row[0]}* — {row[1]} {book_icon} | {row[2]}\n"
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
            reply_markup = back_to_menu_keyboard(user_data["language"])

        await query.edit_message_text(response, parse_mode='Markdown', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка при получении заданий: {e}")
        await query.edit_message_text(
            f"⛔ Ошибка при получении заданий: {str(e)}" 
            if user_data["language"] == "ru" else 
            f"⛔ Error getting tasks: {str(e)}",
            reply_markup=back_to_menu_keyboard(user_data["language"]))

async def callback_get_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить данные о заданиях (использует кэш)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)

    if user_data["group"]:
        await show_tasks_for_group(query, user_data["group"], user_data=user_data)
    else:
        await callback_select_group(update, context)

async def format_task_message(context):
    """Форматирование сообщения о задании"""
    task_data = context.user_data.get("task_data", {})
    user_id = context._user_id if hasattr(context, '_user_id') else None
    user_data = get_cached_user_data(user_id) if user_id else {"language": "ru"}

    message = "📝 Редактирование задания:\n\n" if user_data["language"] == "ru" else "📝 Editing task:\n\n"
    message += f"🔹 <b>Предмет:</b> {task_data.get('subject', 'не выбрано' if user_data['language'] == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Тип задания:</b> {task_data.get('task_type', 'не выбрано' if user_data['language'] == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Макс. баллы:</b> {task_data.get('max_points', 'не выбрано' if user_data['language'] == 'ru' else 'not selected')}\n"
    message += f"🔹 <b>Дата:</b> {task_data.get('date', 'не выбрана' if user_data['language'] == 'ru' else 'not selected')}\n"

    time_display = task_data.get('time', 'не выбрано' if user_data['language'] == 'ru' else 'not selected')
    if time_display == "schedule":
        time_display = "По расписанию" if user_data['language'] == "ru" else "By schedule"
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
    user_data = get_cached_user_data(user_id)

    # Проверяем права куратора или профессора
    if not can_edit_tasks(user_data):
        await query.edit_message_text(
            "⛔ У вас нет доступа к добавлению заданий." 
            if user_data["language"] == "ru" else 
            "⛔ You don't have access to add tasks.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))
        return ConversationHandler.END

    # Проверяем что установлена группа
    if not user_data.get("group"):
        if user_data.get("is_professor", False):
            await query.edit_message_text(
                "📝 Сначала выберите группу через меню профессора."
                if user_data["language"] == "ru" else
                "📝 Please select a group first through the professor menu.",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
        else:
            await query.edit_message_text(
                "📝 Сначала создайте группу через меню куратора."
                if user_data["language"] == "ru" else
                "📝 Please create a group first through the curator menu.",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
        return ConversationHandler.END

    # Инициализируем данные задания
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
    """Редактирование параметров задания"""
    query = update.callback_query
    await query.answer()
    user_data = get_cached_user_data(query.from_user.id)

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
            time_value = "schedule"  # Храним как "schedule" в таблице
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
                task_data["time"],  # Теперь храним "schedule" для "По расписанию"
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
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
        except Exception as e:
            logger.error(f"Ошибка при сохранении задания: {e}")
            await query.edit_message_text(
                f"⛔ Произошла ошибка при сохранении: {str(e)}" if user_data["language"] == "ru" else f"⛔ Error saving: {str(e)}",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
        return ConversationHandler.END
    elif query.data == "cancel_task":
        context.user_data.clear()
        await query.edit_message_text(
            "🚫 Добавление задания отменено." if user_data["language"] == "ru" else "🚫 Task addition canceled.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))
        return ConversationHandler.END

    return EDITING_TASK

async def handle_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    waiting_for = context.user_data.get("waiting_for")
    user_data = get_cached_user_data(update.effective_user.id)

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
    user_data = get_cached_user_data(user_id)

    # Проверяем права куратора или профессора
    if not can_edit_tasks(user_data):
        await query.edit_message_text(
            "⛔ У вас нет доступа к удалению заданий." if user_data["language"] == "ru" else "⛔ You don't have access to delete tasks.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))
        return ConversationHandler.END

    # Проверяем что установлена группа
    if not user_data.get("group"):
        if user_data.get("is_professor", False):
            await query.edit_message_text(
                "📝 Сначала выберите группу через меню профессора."
                if user_data["language"] == "ru" else
                "📝 Please select a group first through the professor menu.",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
        else:
            await query.edit_message_text(
                "📝 Сначала создайте группу через меню куратора."
                if user_data["language"] == "ru" else
                "📝 Please create a group first through the curator menu.",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
        return ConversationHandler.END

    await show_tasks_for_group(query, user_data["group"], show_delete_buttons=True, user_data=user_data)
    return EDITING_TASK

async def handle_task_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_cached_user_data(query.from_user.id)

    if query.data == "back_to_menu":
        await callback_back_to_menu(update, context)
        return ConversationHandler.END

    if query.data.startswith("delete_"):
        try:
            _, group, row_idx = query.data.split("_")
            row_idx = int(row_idx)

            success = gsh.delete_row(group, row_idx)

            if success:
                await query.edit_message_text(
                    "✅ Задание успешно удалено!" if user_data["language"] == "ru" else "✅ Task deleted successfully!",
                    reply_markup=main_menu_keyboard(user_data["language"], user_data))

                # Обновляем напоминания для всех пользователей группы
                await refresh_reminders_for_group(context.application.job_queue, group)
            else:
                await query.edit_message_text(
                    "⛔ Задание уже было удалено" if user_data["language"] == "ru" else "⛔ Task was already deleted",
                    reply_markup=main_menu_keyboard(user_data["language"], user_data))
        except Exception as e:
            logger.error(f"Ошибка при удалении задания: {e}")
            await query.edit_message_text(
                f"⛔ Ошибка при удалении: {str(e)}" if user_data["language"] == "ru" else f"⛔ Error deleting: {str(e)}",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))

    return ConversationHandler.END

# ==================== СИСТЕМА НАПОМИНАНИЙ ====================
async def callback_reminder_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_cached_user_data(query.from_user.id)

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
            reply_markup=main_menu_keyboard(user_data["language"], user_data))

async def toggle_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_cached_user_data(user_id)

    try:
        new_state = not user_data["reminders_enabled"]
        if update_cached_user_data(user_id, "reminders_enabled", new_state):
            user_data["reminders_enabled"] = new_state

        await schedule_reminders_for_user(context.application.job_queue, user_id)

        await query.edit_message_text(
            f"✅ Напоминания {'включены' if new_state else 'выключены'}!" if user_data["language"] == "ru" else f"✅ Reminders {'enabled' if new_state else 'disabled'}!",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))
    except Exception as e:
        logger.error(f"Ошибка в toggle_reminders: {e}")
        await query.edit_message_text(
            "⛔ Произошла ошибка при изменении настроек." if user_data["language"] == "ru" else "⛔ Error changing settings.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))

async def schedule_reminders_for_user(job_queue: JobQueue, user_id: int):
    """Запланировать напоминания для пользователя"""
    try:
        logger.info(f"Scheduling reminders for user {user_id}")

        # Удаление старых напоминаний
        for job in job_queue.jobs():
            if job.name and str(user_id) in job.name:
                job.schedule_removal()

        user_data = get_cached_user_data(user_id)
        if not user_data["reminders_enabled"] or not user_data["group"]:
            return

        data = get_cached_sheet_data(user_data["group"])
        if not data or len(data) <= 1:
            return
            
        data = data[1:]
        now = datetime.now(MOSCOW_TZ)
        today = now.date()
        tasks_for_reminder = []

        for row in data:
            if len(row) >= 7 and row[6] == user_data["group"]:
                try:
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

    user_data = get_cached_user_data(user_id)

    tasks_by_days = {}
    for task in tasks:
        if task['days_left'] not in tasks_by_days:
            tasks_by_days[task['days_left']] = []
        tasks_by_days[task['days_left']].append(task)

    sorted_days = sorted(tasks_by_days.keys())

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
            time_display = format_time_display(task['time'], user_data["language"])
            book_icon = "📖" if task.get('book_type') == "open-book" else "📕"

            details = ""
            if (task.get('details') and 
                task['details'].strip() and 
                task['details'] not in ["не выбраны", "not selected", ""]):
                details = f" | {task['details']}\n"

            message += (
                f"{book_icon} *{task['subject']}* — {task['task_type']} | {task['format']}\n"
                f"📅 {task['date']} | 🕒 {time_display} | *{task['max_points']}* баллов курса\n" 
                f"{details}"
                if user_data["language"] == "ru" else
                f"{book_icon} *{task['subject']}* — {task['task_type']} ({task['format']})\n"                   
                f"📅 {task['date']} | 🕒 {time_display} | *{task['max_points']}* course points\n"
                f"{details}"
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
        users = get_cached_sheet_data("Users")
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
        users = get_cached_sheet_data("Users")
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
    user_data = get_cached_user_data(query.from_user.id)

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
        if update_cached_user_data(user_id, "language", lang):
            user_data = get_cached_user_data(user_id)
            await query.edit_message_text(
                "✅ Язык изменен на русский!" if user_data["language"] == "ru" else "✅ Language changed to English!",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
    except Exception as e:
        logger.error(f"Ошибка при изменении языка: {e}")
        user_data = get_cached_user_data(user_id)
        await query.edit_message_text(
            "⛔ Произошла ошибка при изменении языка." if user_data["language"] == "ru" else "⛔ Error changing language.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))

# ==================== СИСТЕМА ОБРАТНОЙ СВЯЗИ ====================
async def callback_leave_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_cached_user_data(query.from_user.id)

    await query.edit_message_text(
        "📝 Пожалуйста, напишите ваш отзыв или предложение по улучшению бота:" if user_data["language"] == "ru" else 
        "📝 Please write your feedback or suggestion for improving the bot:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить" if user_data["language"] == "ru" else "↩️ Cancel", callback_data="cancel_feedback")]])
    )
    return WAITING_FOR_FEEDBACK

async def handle_feedback_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    feedback_text = update.message.text
    user_data = get_cached_user_data(user_id)

    try:
        if update_cached_user_data(user_id, "feedback", feedback_text):
            await update.message.reply_text(
                "✅ Спасибо за ваш отзыв! Мы учтем ваши пожелания." if user_data["language"] == "ru" else 
                "✅ Thank you for your feedback! We'll take it into account.",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
        else:
            await update.message.reply_text(
                "⛔ Не удалось сохранить отзыв. Попробуйте позже." if user_data["language"] == "ru" else 
                "⛔ Failed to save feedback. Please try again later.",
                reply_markup=main_menu_keyboard(user_data["language"], user_data))
    except Exception as e:
        logger.error(f"Ошибка при сохранении фидбэка: {e}")
        await update.message.reply_text(
            "⛔ Произошла ошибка при сохранении отзыва." if user_data["language"] == "ru" else 
            "⛔ An error occurred while saving feedback.",
            reply_markup=main_menu_keyboard(user_data["language"], user_data))

    return ConversationHandler.END

async def cancel_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = get_cached_user_data(query.from_user.id)

    await query.edit_message_text(
        "🚫 Отправка отзыва отменена." if user_data["language"] == "ru" else "🚫 Feedback submission canceled.",
        reply_markup=main_menu_keyboard(user_data["language"], user_data))
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
    application.add_handler(CallbackQueryHandler(set_user_group, pattern="^set_group_"))
    application.add_handler(CallbackQueryHandler(callback_admin_panel, pattern="admin_panel"))

    # Обработчики настроек
    application.add_handler(CallbackQueryHandler(callback_reminder_settings, pattern="reminder_settings"))
    application.add_handler(CallbackQueryHandler(toggle_reminders, pattern="toggle_reminders"))
    application.add_handler(CallbackQueryHandler(callback_language_settings, pattern="language_settings"))
    application.add_handler(CallbackQueryHandler(set_user_language, pattern="^set_lang_ru$|^set_lang_en$"))

    # Обработчики админ-панели
    application.add_handler(CallbackQueryHandler(admin_make_curator, pattern="admin_make_curator"))
    application.add_handler(CallbackQueryHandler(admin_make_professor, pattern="admin_make_professor"))
    application.add_handler(CallbackQueryHandler(admin_list_curators, pattern="admin_list_curators"))
    application.add_handler(CallbackQueryHandler(admin_list_professors, pattern="admin_list_professors"))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern="admin_stats"))
    application.add_handler(CallbackQueryHandler(admin_new_year, pattern="admin_new_year"))
    application.add_handler(CallbackQueryHandler(confirm_new_year, pattern="confirm_new_year"))
    application.add_handler(CallbackQueryHandler(handle_curator_approval, pattern="^approve_curator_|^reject_curator_"))

    # Обработчики для кураторов и профессоров
    application.add_handler(CallbackQueryHandler(become_curator, pattern="become_curator"))
    application.add_handler(CallbackQueryHandler(curator_create_group, pattern="curator_create_group"))
    application.add_handler(CallbackQueryHandler(professor_select_group, pattern="professor_select_group"))
    application.add_handler(CallbackQueryHandler(handle_new_curator_course, pattern="^course_[1-4]$"))
    application.add_handler(CallbackQueryHandler(handle_professor_course_selection, pattern="^course_[1-4]$"))
    application.add_handler(CallbackQueryHandler(handle_professor_group_selection, pattern="^professor_set_group_"))

    # Обработчик для добавления заданий
    add_task_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_add_task, pattern="add_task")],
        states={
            EDITING_TASK: [CallbackQueryHandler(edit_task_parameter)],
            WAITING_FOR_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_input)],
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
        per_message=False
    )

    # Обработчик для удаления заданий
    delete_task_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_delete_task, pattern="delete_task")],
        states={
            EDITING_TASK: [CallbackQueryHandler(handle_task_deletion)]
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
        per_message=False
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
        per_message=False
    )

    # Обработчик для назначения кураторов и профессоров по username
    appointment_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_make_curator, pattern="admin_make_curator"),
            CallbackQueryHandler(admin_make_professor, pattern="admin_make_professor")
        ],
        states={
            WAITING_FOR_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username_input)],
            CONFIRM_CURATOR: [
                CallbackQueryHandler(confirm_make_curator, pattern="confirm_make_curator"),
                CallbackQueryHandler(confirm_make_professor, pattern="confirm_make_professor")
            ]
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
        per_message=False
    )

    # Обработчик для создания групп кураторами
    group_creation_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(curator_create_group, pattern="curator_create_group")],
        states={
            NEW_CURATOR_COURSE: [CallbackQueryHandler(handle_new_curator_course, pattern="^course_[1-4]$")],
            NEW_CURATOR_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_curator_group_name)]
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
        per_message=False
    )

    # Обработчик для выбора группы студентами
    group_selection_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_select_group, pattern="select_group")],
        states={
            CHOOSING_COURSE_FOR_GROUP: [CallbackQueryHandler(handle_course_selection_student, pattern="^course_[1-4]$")],
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
        per_message=False
    )

    # Обработчик для выбора группы профессорами
    professor_group_selection_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(professor_select_group, pattern="professor_select_group")],
        states={
            PROFESSOR_SELECT_COURSE: [CallbackQueryHandler(handle_professor_course_selection, pattern="^course_[1-4]$")],
            PROFESSOR_SELECT_GROUP: [CallbackQueryHandler(handle_professor_group_selection, pattern="^professor_set_group_")]
        },
        fallbacks=[CommandHandler("cancel", callback_back_to_menu)],
        per_message=False
    )

    application.add_handler(add_task_handler)
    application.add_handler(delete_task_handler)
    application.add_handler(feedback_handler)
    application.add_handler(appointment_handler)
    application.add_handler(group_creation_handler)
    application.add_handler(group_selection_handler)
    application.add_handler(professor_group_selection_handler)

    # Настраиваем периодическую проверку напоминаний
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(
            check_reminders_now,
            interval=timedelta(minutes=REMINDER_CHECK_INTERVAL),
            first=10
        )

        # Периодическое обновление кэша (раз в 5 минут)
        job_queue.run_repeating(
            lambda context: invalidate_sheet_cache(),
            interval=timedelta(minutes=5),
            first=60
        )

    logger.info("Bot started successfully with professor system and fixed time display!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
