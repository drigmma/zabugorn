import os
import asyncio
import json
import logging
import re
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, CallbackQuery, Contact
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

import aiosqlite
import gspread
from google.oauth2.service_account import Credentials as GoogleCredentials

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT")

GOOGLE_CREDS_JSON_PATH = os.environ.get("GOOGLE_CREDS_JSON_PATH")
GOOGLE_CREDS_JSON_CONTENT = os.environ.get("GOOGLE_CREDS_JSON_CONTENT")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Заявки")

DB_PATH = os.environ.get("DB_PATH", "requests.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

ADMINS = []
for a in ADMIN_IDS_RAW.split(","):
    if not a.strip():
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
    extra_phone = State()
    brand_model = State()
    exterior = State()
    interior = State()
    package = State()
    budget = State()
    year = State()
    # priority removed
    wishes = State()

class AdminState(StatesGroup):
    waiting_admin_message = State()

# ---------- DB ----------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    name TEXT,
    phones TEXT,
    brand_model TEXT,
    exterior TEXT,
    interior TEXT,
    package TEXT,
    budget TEXT,
    year TEXT,
    priority TEXT DEFAULT 'без срочности',
    wishes TEXT,
    sheet_row INTEGER,
    status TEXT DEFAULT 'new'
)
"""

...
# Этот "..." просто как заглушка, не влияет на логику

# ---------- Simple in-memory consent storage ----------
class ConsentStore:
    def __init__(self):
        self._store = {}

    def set(self, user_id: int, value: bool):
        self._store[user_id] = value

    def get(self, user_id: int, default=False) -> bool:
        return self._store.get(user_id, default)

CONSENT_STORE = ConsentStore()

# ---------- Google Sheets ----------
def get_google_client():
    """
    Инициализация клиента Google Sheets.

    Можно либо передать путь к файлу JSON сервисного аккаунта через GOOGLE_CREDS_JSON_PATH,
    либо указать содержимое JSON целиком в переменной GOOGLE_CREDS_JSON_CONTENT.
    """
    if GOOGLE_CREDS_JSON_PATH:
        logger.info("Loading Google credentials from JSON file: %s", GOOGLE_CREDS_JSON_PATH)
        creds = GoogleCredentials.from_service_account_file(
            GOOGLE_CREDS_JSON_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    elif GOOGLE_CREDS_JSON_CONTENT:
        logger.info("Loading Google credentials from JSON content in env var")
        info = json.loads(GOOGLE_CREDS_JSON_CONTENT)
        creds = GoogleCredentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    else:
        raise RuntimeError("No Google service account credentials provided")

    client = gspread.authorize(creds)
    return client

async def append_to_sheet(row_values):
    """
    Добавление строки в Google Sheets. Выполняется в отдельном потоке, чтобы не блокировать event loop.
    """
    loop = asyncio.get_running_loop()
    def _append():
        try:
            client = get_google_client()
            sh = client.open_by_key(SPREADSHEET_ID)
            worksheet = sh.worksheet(WORKSHEET_NAME)
            worksheet.append_row(row_values, value_input_option="USER_ENTERED")
            row_number = len(worksheet.get_all_values())
            return row_number
        except Exception as e:
            logger.exception("Error while appending to Google Sheets: %s", e)
            return None

    row_number = await loop.run_in_executor(None, _append)
    return row_number

# ---------- Bot initialization ----------
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ---------- Keyboards ----------
def main_user_keyboard():
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Оставить заявку на автомобиль")],
        [KeyboardButton(text="💬 Написать в поддержку")]
    ], resize_keyboard=True)
    return kb

def contact_request_kb():
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📱 Отправить номер", request_contact=True)],
        [KeyboardButton(text="✏️ Ввести вручную")]
    ], resize_keyboard=True)
    return kb

def username_inline_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Вставить мой username", callback_data="use_username")]
    ])
    return kb

def request_inline_kb(request_id: int, sheet_row: Optional[int]):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"take:{request_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request_id}")
        ],
        [
            InlineKeyboardButton(text="💌 Написать", callback_data=f"admin_msg:{request_id}")
        ]
    ]
    if sheet_row:
        buttons.append(
            [InlineKeyboardButton(text="📄 Открыть в таблице", url=f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid=0&range=A{sheet_row}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---------- Database helpers ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()

async def save_request_to_db(
    user_id: int,
    username: str,
    name: str,
    phones: str,
    brand_model: str,
    exterior: str,
    interior: str,
    package: str,
    budget: str,
    year: str,
    wishes: str,
    sheet_row: Optional[int]
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO requests (
                user_id, username, name, phones, brand_model,
                exterior, interior, package, budget, year,
                wishes, sheet_row
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, name, phones, brand_model,
             exterior, interior, package, budget, year,
             wishes, sheet_row)
        )
        await db.commit()
        return cursor.lastrowid

async def update_request_status(request_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))
        await db.commit()

async def get_request_by_id(request_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, user_id, username, name, phones, brand_model, exterior, interior, package, budget, year, wishes, sheet_row, status "
            "FROM requests WHERE id=?",
            (request_id,)
        )
        row = await cursor.fetchone()
    return row

# ---------- Support state holder ----------
class SupportStateHolder:
    """
    Простая in-memory структура для хранения того,
    что пользователь сейчас пишет в поддержку.
    """
    _support_users = set()

    @classmethod
    def set_support_state(cls, user_id: int):
        cls._support_users.add(user_id)

    @classmethod
    def remove(cls, user_id: int):
        cls._support_users.discard(user_id)

    @classmethod
    def is_waiting(cls, user_id: int) -> bool:
        return user_id in cls._support_users

# ---------- Misc helpers ----------
def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if digits.startswith("7"):
        return "+" + digits
    return phone

# ---------- Handlers ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    CONSENT_STORE.set(message.from_user.id, True)
    await state.clear()
    text = (
        "👋 <b>Здравствуйте!</b>\n\n"
        "Я бот компании <b>ЗАБУГОРНЫЙLUX</b>.\n\n"
        "Через меня вы можете:\n"
        "• 📋 Оставить заявку на подбор автомобиля\n"
        "• 💬 Написать в поддержку\n\n"
        "Выберите нужный пункт в меню ниже."
    )
    await message.answer(text, reply_markup=main_user_keyboard(), parse_mode="HTML")

@dp.message(Command(commands=["help"]))
async def cmd_help(message: Message):
    await message.reply(
        "ℹ️ Для работы используйте кнопки меню:\n"
        "• 📋 Оставить заявку на автомобиль\n"
        "• 💬 Написать в поддержку",
        parse_mode="HTML"
    )

@dp.message(F.text == "💬 Написать в поддержку")
async def ask_support(message: Message):
    user_id = message.from_user.id
    if not CONSENT_STORE.get(user_id, False):
        await message.reply("❌ Сначала подтвердите обработку персональных данных через /start")
        return
    await message.reply(
        "📝 <b>Напишите вашу проблему или вопрос</b>\n\n"
        "Сообщение будет отправлено нашему менеджеру ЗАБУГОРНЫЙLUX, и мы свяжемся с вами.",
        parse_mode="HTML"
    )
    SupportStateHolder.set_support_state(user_id)

@dp.message(F.text == "📋 Оставить заявку на автомобиль")
async def start_form_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not CONSENT_STORE.get(user_id, False):
        await message.reply("❌ Сначала подтвердите обработку персональных данных через /start")
        return
    await message.answer(
        "📋 <b>Начнём заполнение анкеты!</b>\n\n"
        "Введите ваше полное имя (ФИО):",
        parse_mode="HTML"
    )
    await state.set_state(Form.name)

@dp.message(Form.name)
async def process_name(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.reply("❗ Пожалуйста, введите имя текстом.")
        return
    await state.update_data(name=text)
    await message.answer(
        "📱 <b>Укажите ваш номер телефона</b>\n\n"
        "Вы можете отправить контакт кнопкой ниже или ввести номер вручную.",
        reply_markup=contact_request_kb(),
        parse_mode="HTML"
    )
    await state.set_state(Form.phone)

@dp.message(Form.phone, F.contact)
async def process_phone_contact(message: Message, state: FSMContext):
    contact: Contact = message.contact
    phone = contact.phone_number
    phone_norm = normalize_phone(phone)
    await state.update_data(phone=phone_norm)
    await ask_username(message, state)

@dp.message(Form.phone)
async def process_phone_manual(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.reply("❗ Введите номер телефона или отправьте контакт.")
        return
    phone_norm = normalize_phone(text)
    await state.update_data(phone=phone_norm)
    await ask_username(message, state)

async def ask_username(message: Message, state: FSMContext):
    await message.answer(
        "💬 <b>Укажите ваш Telegram username</b> (если есть),\n"
        "или нажмите кнопку ниже, чтобы вставить автоматически.",
        reply_markup=username_inline_kb(),
        parse_mode="HTML"
    )
    await state.set_state(Form.username)

@dp.callback_query(F.data == "use_username")
async def cb_use_username(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.username:
        uname = "@" + cb.from_user.username
        await state.update_data(username=uname)
        await cb.message.edit_text(
            f"Ваш username: <b>{uname}</b>",
            parse_mode="HTML"
        )
        await ask_extra_phone(cb.message, state)
    else:
        await cb.answer("У вас нет username в Telegram", show_alert=True)

@dp.message(Form.username)
async def process_username(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text and not text.startswith("@"):
        text = "@" + text
    await state.update_data(username=text)
    await ask_extra_phone(message, state)

async def ask_extra_phone(message: Message, state: FSMContext):
    await message.answer(
        "📞 <b>Дополнительный номер телефона</b>\n\n"
        "Если хотите, укажите ещё один номер телефона или напишите <i>«нет»</i>.",
        parse_mode="HTML"
    )
    await state.set_state(Form.extra_phone)

@dp.message(Form.extra_phone)
async def process_extra_phone(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    if text in ("нет", "no", "не надо", "нету", "none", "ничего"):
        await state.update_data(extra_phone="")
    else:
        phone_norm = normalize_phone(message.text or "")
        await state.update_data(extra_phone=phone_norm)
    await message.answer(
        "🚗 <b>Марка и модель автомобиля</b>\n\n"
        "Например: <i>Mercedes-Benz S-Class</i>",
        parse_mode="HTML"
    )
    await state.set_state(Form.brand_model)

@dp.message(Form.brand_model)
async def process_brand_model(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.reply("❗ Введите марку и модель автомобиля.")
        return
    await state.update_data(brand_model=text)
    await message.answer(
        "🎨 <b>Желаемый цвет экстерьера (кузова)</b>\n\n"
        "Например: <i>чёрный, белый, не принципиально</i>.",
        parse_mode="HTML"
    )
    await state.set_state(Form.exterior)

@dp.message(Form.exterior)
async def process_exterior(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    await state.update_data(exterior=text)
    await message.answer(
        "🪑 <b>Желаемый цвет/тип интерьера салона</b>\n\n"
        "Например: <i>чёрный кожа, бежевый, ткань, не принципиально</i>.",
        parse_mode="HTML"
    )
    await state.set_state(Form.interior)

@dp.message(Form.interior)
async def process_interior(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    await state.update_data(interior=text)
    await message.answer(
        "📦 <b>Комплектация</b>\n\n"
        "Укажите пожелания по комплектации (опции, пакеты) или напишите <i>«стандарт»</i>.",
        parse_mode="HTML"
    )
    await state.set_state(Form.package)

@dp.message(Form.package)
async def process_package(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    await state.update_data(package=text)
    await message.answer(
        "💰 <b>Бюджет</b>\n\n"
        "Укажите бюджет на автомобиль (в рублях), например: <i>от 5 до 7 млн</i>.",
        parse_mode="HTML"
    )
    await state.set_state(Form.budget)

@dp.message(Form.budget)
async def process_budget(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    await state.update_data(budget=text)
    await message.answer(
        "📅 <b>Желаемый год выпуска</b>\n\n"
        "Например: <i>от 2020, 2018-2022, не старше 5 лет</i>.",
        parse_mode="HTML"
    )
    await state.set_state(Form.year)

@dp.message(Form.year)
async def process_year(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    await state.update_data(year=text)
    await message.answer(
        "✏️ <b>Дополнительные пожелания</b>\n\n"
        "Напишите всё, что считаете важным: пробег, состояние, страна привоза, и т.д.\n"
        "Если пожеланий нет — напишите <i>«нет»</i>.",
        parse_mode="HTML"
    )
    await state.set_state(Form.wishes)

@dp.message(Form.wishes)
async def process_wishes(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower() in ("нет", "no", "none", "ничего"):
        text = ""
    await state.update_data(wishes=text)

    data = await state.get_data()

    user = message.from_user
    user_id = user.id
    username = data.get("username") or (f"@{user.username}" if user.username else "")
    name = data.get("name", "")
    phone = data.get("phone", "")
    extra_phone = data.get("extra_phone", "")
    brand_model = data.get("brand_model", "")
    exterior = data.get("exterior", "")
    interior = data.get("interior", "")
    package = data.get("package", "")
    budget = data.get("budget", "")
    year = data.get("year", "")
    wishes = data.get("wishes", "")

    phones_combined = phone
    if extra_phone:
        phones_combined += f", {extra_phone}"

    tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz).strftime("%d.%m.%Y %H:%M")

    row_values = [
        str(user_id),
        username,
        name,
        phones_combined,
        brand_model,
        exterior,
        interior,
        package,
        budget,
        year,
        wishes,
        "new",
        now
    ]

    await message.answer("⏳ Сохраняем вашу заявку, пожалуйста подождите...")

    sheet_row = await append_to_sheet(row_values)
    request_id = await save_request_to_db(
        user_id=user_id,
        username=username,
        name=name,
        phones=phones_combined,
        brand_model=brand_model,
        exterior=exterior,
        interior=interior,
        package=package,
        budget=budget,
        year=year,
        wishes=wishes,
        sheet_row=sheet_row
    )

    await state.clear()

    text_confirm = (
        "✅ <b>Ваша заявка сохранена!</b>\n\n"
        f"Номер заявки: <b>{request_id}</b>\n"
        "Наш менеджер свяжется с вами в ближайшее время."
    )
    await message.answer(text_confirm, parse_mode="HTML", reply_markup=main_user_keyboard())

    await notify_admins_new_request(
        request_id=request_id,
        user_id=user_id,
        username=username,
        name=name,
        phones=phones_combined,
        brand_model=brand_model,
        exterior=exterior,
        interior=interior,
        package=package,
        budget=budget,
        year=year,
        wishes=wishes,
        sheet_row=sheet_row
    )

async def notify_admins_new_request(
    request_id: int,
    user_id: int,
    username: str,
    name: str,
    phones: str,
    brand_model: str,
    exterior: str,
    interior: str,
    package: str,
    budget: str,
    year: str,
    wishes: str,
    sheet_row: Optional[int]
):
    if not ADMINS:
        logger.warning("No admins configured, cannot notify about new request")
        return

    text = (
        "🆕 <b>Новая заявка</b>\n\n"
        f"<b>ID:</b> {request_id}\n"
        f"<b>Пользователь:</b> {name}\n"
        f"<b>Telegram:</b> {username or 'нет'}\n"
        f"<b>Телефоны:</b> {phones}\n"
        f"<b>Авто:</b> {brand_model}\n"
        f"<b>Экстерьер:</b> {exterior}\n"
        f"<b>Интерьер:</b> {interior}\n"
        f"<b>Комплектация:</b> {package}\n"
        f"<b>Бюджет:</b> {budget}\n"
        f"<b>Год:</b> {year}\n"
        f"<b>Пожелания:</b> {wishes or '—'}\n"
    )

    kb = request_inline_kb(request_id, sheet_row)

    for admin_id in ADMINS:
        try:
            await bot.send_message(
                admin_id,
                text,
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception as e:
            logger.warning("Failed to notify admin %s: %s", admin_id, e)

@dp.callback_query(F.data.startswith("take:"))
async def cb_take_request(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        request_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return

    await update_request_status(request_id, "in_progress")
    await cb.answer("Заявка взята в работу")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject_request(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        request_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return

    await update_request_status(request_id, "rejected")
    await cb.answer("Заявка отклонена")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

@dp.callback_query(F.data.startswith("admin_msg:"))
async def admin_msg(cb: CallbackQuery, state: FSMContext):
    user_id = int(cb.data.split(":", 1)[1])
    await cb.answer()
    await cb.message.reply(
        f"💬 Введите сообщение для пользователя {user_id}",
        parse_mode="HTML"
    )
    await state.set_state(AdminState.waiting_admin_message)
    await state.update_data(target_user=user_id)

@dp.message(AdminState.waiting_admin_message)
async def handle_admin_message(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get('target_user')
    if target:
        try:
            await bot.send_message(
                target,
                f"💬 <b>Сообщение от менеджера:</b>\n\n{message.text}",
                parse_mode="HTML"
            )
            await message.reply("✅ Сообщение отправлено пользователю")
        except Exception as e:
            await message.reply(f"❌ Ошибка при отправке: {e}")
    await state.clear()

@dp.message(Command(commands=["list_requests"]))
async def list_requests(message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ Только для администраторов")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, name, phones, brand_model, status FROM requests ORDER BY id DESC LIMIT 50")
        rows = await cursor.fetchall()
    if not rows:
        await message.reply("📭 Нет заявок в базе")
        return
    for r in rows:
        req_id, name, phones, brand_model, status = r
        status_emoji = "🆕" if status == "new" else ("⚙️" if status == "in_progress" else "❌")
        await message.reply(
            f"{status_emoji} <b>Заявка {req_id}</b>\n"
            f"<b>Имя:</b> {name}\n"
            f"<b>Телефоны:</b> {phones}\n"
            f"<b>Авто:</b> {brand_model}",
            parse_mode="HTML"
        )

@dp.message()
async def catch_all_messages(message: Message):
    user_id = message.from_user.id
    text = message.text or ""
    if SupportStateHolder.is_waiting(user_id):
        for admin in ADMINS:
            try:
                reply_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💌 Ответить пользователю", callback_data=f"admin_msg:{user_id}")]
                ])
                await bot.send_message(
                    admin,
                    (
                        "💬 <b>Сообщение в поддержку</b>\n\n"
                        f"От: {message.from_user.full_name} (@{message.from_user.username or 'нет username'})\n\n"
                        f"{text}"
                    ),
                    reply_markup=reply_kb,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning("Failed to forward support to admin %s: %s", admin, e)
        SupportStateHolder.remove(user_id)
        await message.answer(
            "✅ <b>Спасибо!</b>\n\n"
            "Ваше сообщение отправлено в поддержку.\n"
            "Наш менеджер ЗАБУГОРНЫЙLUX свяжется с Вами.",
            parse_mode="HTML"
        )
        return
    await message.reply(
        "👋 Пожалуйста, используйте кнопки меню для навигации. Если возникли вопросы — напишите в 'Поддержку'.",
        parse_mode="HTML"
    )

# ---------- Startup/Run ----------
async def on_startup():
    await init_db()
    logger.info("Bot started")

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception("Unhandled exception in bot: %s", e)
        
