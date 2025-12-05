"""
Aiogram v3 Telegram bot: Car request form -> Google Sheets + admin actions
Версия: без использования .env / python-dotenv

Как задавать конфигурацию:
 - Перед запуском экспортируйте переменные окружения в системе (bash/zsh):
     export BOT_TOKEN="..."
     export ADMIN_IDS="123456,789012"
     export SUPPORT_CONTACT="@drigmma"
     export GOOGLE_CREDS_JSON_PATH="/absolute/path/to/creds.json"  # либо GOOGLE_CREDS_JSON_CONTENT
     export SPREADSHEET_ID="195orywPJeGm0oPzmRy2QRe5pFG4G6wUUvRGNMdbM3Gs"
     export GOOGLE_SHEET_NAME="Telegram Car Requests"

 - Альтернатива: если вы используете systemd/Docker, задайте ту же переменную окружения в сервисе/контейнере.

Требуемые библиотеки:
 pip install aiogram aiosqlite gspread google-auth

"""

import os
import asyncio
import json
import logging
from typing import Optional

# ОБЯЗАТЕЛЬНО: загрузить .env ДО всего остального
from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

import aiosqlite

# Google Sheets (modern auth)
import gspread
from google.oauth2.service_account import Credentials as GoogleCredentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Конфигурация (без .env) ----------
# Читается только из переменных окружения
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@drigmma")

# Google credentials
GOOGLE_CREDS_JSON_PATH = os.environ.get("GOOGLE_CREDS_JSON_PATH")
GOOGLE_CREDS_JSON_CONTENT = os.environ.get("GOOGLE_CREDS_JSON_CONTENT")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME")

DB_PATH = os.environ.get("DB_PATH", "requests.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable required")

# Convert admin ids to ints
ADMINS = []
for a in ADMIN_IDS_RAW.split(","):
    if not a:
        continue
    try:
        ADMINS.append(int(a.strip()))
    except Exception:
        logger.warning("Invalid ADMIN_ID skipped: %s", a)

# ---------- FSM states ----------
class Form(StatesGroup):
    name = State()
    phone = State()
    username = State()
    brand_model = State()
    exterior = State()
    interior = State()
    package = State()
    budget = State()
    year = State()
    priority = State()
    wishes = State()

class AdminState(StatesGroup):
    waiting_admin_message = State()

# ---------- DB setup ----------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    name TEXT,
    phone TEXT,
    brand_model TEXT,
    exterior TEXT,
    interior TEXT,
    package TEXT,
    budget TEXT,
    year TEXT,
    priority TEXT,
    wishes TEXT,
    sheet_row INTEGER,
    status TEXT DEFAULT 'new'
)
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()

# ---------- Google Sheets helpers ----------
_GS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def _load_service_account_credentials() -> Optional[GoogleCredentials]:
    """Попытка загрузить креды из файла или из JSON-строки.
    Возвращает google.oauth2.service_account.Credentials или None.
    """
    try:
        if GOOGLE_CREDS_JSON_PATH and os.path.isfile(GOOGLE_CREDS_JSON_PATH):
            logger.info("Loading Google credentials from file: %s", GOOGLE_CREDS_JSON_PATH)
            creds = GoogleCredentials.from_service_account_file(GOOGLE_CREDS_JSON_PATH, scopes=_GS_SCOPES)
            return creds

        if GOOGLE_CREDS_JSON_CONTENT:
            logger.info("Loading Google credentials from JSON content in env var")
            info = json.loads(GOOGLE_CREDS_JSON_CONTENT)
            creds = GoogleCredentials.from_service_account_info(info, scopes=_GS_SCOPES)
            return creds

        logger.warning("No Google credentials provided. Set GOOGLE_CREDS_JSON_PATH or GOOGLE_CREDS_JSON_CONTENT")
        return None
    except Exception as e:
        logger.exception("Failed to load Google service account credentials: %s", e)
        return None


def get_gspread_client():
    """Возвращает авторизованный gspread.Client или None."""
    creds = _load_service_account_credentials()
    if not creds:
        return None
    try:
        client = gspread.authorize(creds)
        try:
            sa_email = creds.service_account_email
            logger.info("Authorized Google client. Service account email: %s", sa_email)
        except Exception:
            logger.info("Authorized Google client (could not read service_account_email)")
        return client
    except Exception as e:
        logger.exception("Error authorizing gspread client: %s", e)
        return None


async def append_to_sheet(row: list) -> Optional[int]:
    """Добавляет строку в sheet1 указанной таблицы. Возвращает номер добавленной строки (1-based) или None."""
    creds_available = bool(GOOGLE_CREDS_JSON_PATH or GOOGLE_CREDS_JSON_CONTENT)
    if not creds_available:
        logger.info("Google Sheets not configured (no credentials). Skipping append.")
        return None

    client = get_gspread_client()
    if not client:
        logger.warning("Could not create gspread client")
        return None

    try:
        if SPREADSHEET_ID:
            logger.info("Opening spreadsheet by key: %s", SPREADSHEET_ID)
            sh = client.open_by_key(SPREADSHEET_ID)
        else:
            logger.info("Opening spreadsheet by name: %s", GOOGLE_SHEET_NAME)
            sh = client.open(GOOGLE_SHEET_NAME)

        worksheet = sh.sheet1
        worksheet.append_row(row, value_input_option='USER_ENTERED')
        values = worksheet.get_all_values()
        last = len(values)
        logger.info("Appended row to sheet, new total rows: %s", last)
        return last
    except Exception as e:
        logger.exception("Error appending to sheet: %s", e)
        return None

# ---------- Keyboards ----------

def privacy_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, ознакомился(ась)", callback_data="privacy_yes")],
        [InlineKeyboardButton(text="Нет, не согласен(на)", callback_data="privacy_no")]
    ])
    return kb


def main_user_keyboard():
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Заполнить анкету")],
        [KeyboardButton(text="Написать в поддержку")]
    ], resize_keyboard=True)
    return kb


def admin_request_kb(request_id: int, phone: str, user_id: int):
    buttons = [
        [InlineKeyboardButton(text="✉️ Написать", callback_data=f"admin_msg:{user_id}")],
        [InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"take:{request_id}")],
        [InlineKeyboardButton(text="🗑 Удалить заявку", callback_data=f"delete:{request_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---------- Bot init ----------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

CONSENT_STORE = {}

class SupportStateHolder:
    _support_waiting = set()
    @classmethod
    def set_support_state(cls, user_id: int):
        cls._support_waiting.add(user_id)
    @classmethod
    def is_waiting(cls, user_id: int) -> bool:
        return user_id in cls._support_waiting
    @classmethod
    def remove(cls, user_id: int):
        cls._support_waiting.discard(user_id)

# ---------- Handlers ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await message.answer("Перед началом работы вы ознакомились с политикой обработки персональных данных?", reply_markup=privacy_keyboard())

@dp.callback_query(F.data.startswith("privacy_"))
async def privacy_answer(cb: CallbackQuery):
    user_id = cb.from_user.id
    if cb.data == "privacy_yes":
        CONSENT_STORE[user_id] = True
        await cb.message.edit_text("Спасибо! Вы можете продолжить.")
        await bot.send_message(user_id, "Выберите действие:", reply_markup=main_user_keyboard())
    else:
        CONSENT_STORE[user_id] = False
        await cb.message.edit_text("К сожалению, без согласия на обработку персональных данных вы не можете пользоваться ботом.")
    await cb.answer()

@dp.message(F.text == "Написать в поддержку")
async def ask_support(message: Message):
    user_id = message.from_user.id
    if not CONSENT_STORE.get(user_id, False):
        await message.reply("Сначала подтвердите обработку персональных данных через /start")
        return
    await message.reply("Опишите вашу проблему или вопрос. Сообщение будет отправлено менеджеру.")
    SupportStateHolder.set_support_state(user_id)

@dp.message(F.text == "Заполнить анкету")
async def start_form_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not CONSENT_STORE.get(user_id, False):
        await message.reply("Сначала подтвердите обработку персональных данных через /start")
        return
    await message.answer("Начнём заполнение анкеты. Введите ФИО:")
    await state.set_state(Form.name)

@dp.message(Form.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Номер телефона (в международном формате, например +7...):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("Username в Telegram (если есть), или напишите '-':")
    await state.set_state(Form.username)

@dp.message(Form.username)
async def process_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text)
    await message.answer("Марка/модель автомобиля:")
    await state.set_state(Form.brand_model)

@dp.message(Form.brand_model)
async def process_brand(message: Message, state: FSMContext):
    await state.update_data(brand_model=message.text)
    await message.answer("Экстерьер (коротко):")
    await state.set_state(Form.exterior)

@dp.message(Form.exterior)
async def process_exterior(message: Message, state: FSMContext):
    await state.update_data(exterior=message.text)
    await message.answer("Интерьер (коротко):")
    await state.set_state(Form.interior)

@dp.message(Form.interior)
async def process_interior(message: Message, state: FSMContext):
    await state.update_data(interior=message.text)
    await message.answer("Комплектация/пакет (коротко):")
    await state.set_state(Form.package)

@dp.message(Form.package)
async def process_package(message: Message, state: FSMContext):
    await state.update_data(package=message.text)
    await message.answer("Бюджет (со включенной логистикой/растаможкой):")
    await state.set_state(Form.budget)

@dp.message(Form.budget)
async def process_budget(message: Message, state: FSMContext):
    await state.update_data(budget=message.text)
    await message.answer("Год выпуска:")
    await state.set_state(Form.year)

@dp.message(Form.year)
async def process_year(message: Message, state: FSMContext):
    await state.update_data(year=message.text)
    await message.answer("Приоритет (срочно/нормально/без срочности):")
    await state.set_state(Form.priority)

@dp.message(Form.priority)
async def process_priority(message: Message, state: FSMContext):
    await state.update_data(priority=message.text)
    await message.answer("Пожелания/комментарии (если есть), или '-':")
    await state.set_state(Form.wishes)

@dp.message(Form.wishes)
async def process_wishes(message: Message, state: FSMContext):
    await state.update_data(wishes=message.text)
    data = await state.get_data()
    user = message.from_user

    # Save to sqlite
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO requests (user_id, username, name, phone, brand_model, exterior, interior, package, budget, year, priority, wishes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user.id,
                data.get('username'),
                data.get('name'),
                data.get('phone'),
                data.get('brand_model'),
                data.get('exterior'),
                data.get('interior'),
                data.get('package'),
                data.get('budget'),
                data.get('year'),
                data.get('priority'),
                data.get('wishes')
            )
        )
        await db.commit()
        request_id = cursor.lastrowid

    # Append to Google Sheets
    row = [
        data.get('name'),
        data.get('phone'),
        data.get('username'),
        data.get('brand_model'),
        data.get('exterior'),
        data.get('interior'),
        data.get('package'),
        data.get('budget'),
        data.get('year'),
        data.get('priority'),
        data.get('wishes')
    ]
    sheet_row = await append_to_sheet(row)

    if sheet_row is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE requests SET sheet_row = ? WHERE id = ?", (sheet_row, request_id))
            await db.commit()

    await message.answer("Спасибо! Ваша заявка отправлена. Наш менеджер свяжется с вами.", reply_markup=types.ReplyKeyboardRemove())

    msg_text = (
        f"Новая заявка #{request_id}\n"
        f"ФИО: {data.get('name')}\n"
        f"Телефон: {data.get('phone')}\n"
        f"Username: {data.get('username')}\n"
        f"Марка/модель: {data.get('brand_model')}\n"
        f"Экстерьер: {data.get('exterior')}\n"
        f"Интерьер: {data.get('interior')}\n"
        f"Комплектация: {data.get('package')}\n"
        f"Бюджет: {data.get('budget')}\n"
        f"Год: {data.get('year')}\n"
        f"Приоритет: {data.get('priority')}\n"
        f"Пожелания: {data.get('wishes')}\n"
    )
    for admin in ADMINS:
        try:
            await bot.send_message(admin, msg_text, reply_markup=admin_request_kb(request_id, data.get('phone'), user.id))
        except Exception as e:
            logger.warning("Failed to send request to admin %s: %s", admin, e)

    await state.clear()

# ---------- Admin callbacks ----------
@dp.callback_query(F.data.startswith("take:"))
async def take_request(cb: CallbackQuery):
    req_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE requests SET status = 'in_progress' WHERE id = ?", (req_id,))
        await db.commit()
    await cb.answer("Заявка взята в работу")
    await cb.message.edit_reply_markup()

@dp.callback_query(F.data.startswith("delete:"))
async def delete_request(cb: CallbackQuery):
    req_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM requests WHERE id = ?", (req_id,))
        await db.commit()
    await cb.answer("Заявка удалена")
    await cb.message.edit_text(cb.message.text + "\n\n(удалено)")

@dp.callback_query(F.data.startswith("admin_msg:"))
async def admin_msg(cb: CallbackQuery, state: FSMContext):
    user_id = int(cb.data.split(":", 1)[1])
    await cb.answer()
    await cb.message.reply(f"Введите сообщение, которое будет отправлено пользователю {user_id}")
    await state.set_state(AdminState.waiting_admin_message)
    await state.update_data(target_user=user_id)

@dp.message(AdminState.waiting_admin_message)
async def handle_admin_message(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get('target_user')
    if target:
        try:
            await bot.send_message(target, f"Сообщение от менеджера: {message.text}")
            await message.reply("Сообщение отправлено пользователю")
        except Exception as e:
            await message.reply(f"Не удалось отправить сообщение: {e}")
    await state.clear()

@dp.message(Command(commands=["list_requests"]))
async def list_requests(message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("Только для админов")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, name, phone, brand_model, status FROM requests ORDER BY id DESC LIMIT 50")
        rows = await cursor.fetchall()
    if not rows:
        await message.reply("Нет актуальных заявок")
        return
    for r in rows:
        req_id, name, phone, brand_model, status = r
        text = f"#{req_id} {name}\n{brand_model}\n{phone}\nСтатус: {status}"
        await message.reply(text, reply_markup=admin_request_kb(req_id, phone, 0))

@dp.message()
async def catch_all_messages(message: Message):
    user_id = message.from_user.id
    text = message.text or ""
    if SupportStateHolder.is_waiting(user_id):
        for admin in ADMINS:
            try:
                await bot.send_message(admin, f"[Support] From {message.from_user.full_name} (@{message.from_user.username}):\n{text}")
            except Exception as e:
                logger.warning("Failed to forward support to admin %s: %s", admin, e)
        SupportStateHolder.remove(user_id)
        await message.answer("Ваше сообщение отправлено в поддержку. Мы свяжемся с вами.")
        return
    await message.reply("Пожалуйста, используйте клавиатуру. Если нужно — напишите 'Заполнить анкету' или 'Написать в поддержку'.")

# ---------- Startup/Run ----------
async def on_startup():
    await init_db()
    logger.info("Bot started")
    logger.info(f"Current working directory: {os.getcwd()}")
    logger.info(f"GOOGLE_CREDS_JSON_PATH: {bool(GOOGLE_CREDS_JSON_PATH)}")
    logger.info(f"GOOGLE_CREDS_JSON_CONTENT: {bool(GOOGLE_CREDS_JSON_CONTENT)}")
    logger.info(f"SPREADSHEET_ID configured: {bool(SPREADSHEET_ID)}")

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
