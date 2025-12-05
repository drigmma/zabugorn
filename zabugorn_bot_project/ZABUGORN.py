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
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME")

DB_PATH = os.environ.get("DB_PATH", "requests.db")
AUTO_CONVERT_8_TO_7 = os.environ.get("AUTO_CONVERT_8_TO_7", "1") == "1"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable required")

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

async def migrate_db():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("PRAGMA table_info(requests)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]
        
        if 'phones' not in column_names:
            logger.info("Adding 'phones' column to requests table...")
            try:
                await db.execute("ALTER TABLE requests ADD COLUMN phones TEXT DEFAULT '-'")
                await db.commit()
                logger.info("Column 'phones' added successfully")
            except Exception as e:
                logger.error("Failed to add 'phones' column: %s", e)
                raise

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()
    await migrate_db()

# ---------- Google Sheets helpers ----------
_GS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def _load_service_account_credentials() -> Optional[GoogleCredentials]:
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
    creds = _load_service_account_credentials()
    if not creds:
        return None
    try:
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        logger.exception("Error authorizing gspread client: %s", e)
        return None

async def append_to_sheet(row: list) -> Optional[int]:
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
            sh = client.open_by_key(SPREADSHEET_ID)
        else:
            sh = client.open(GOOGLE_SHEET_NAME)

        worksheet = sh.sheet1
        worksheet.append_row(row, value_input_option='USER_ENTERED')
        values = worksheet.get_all_values()
        last = len(values)
        return last
    except Exception as e:
        logger.exception("Error appending to sheet: %s", e)
        return None

# ---------- Keyboards ----------
def privacy_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, ознакомился(ась)", callback_data="privacy_yes")],
        [InlineKeyboardButton(text="❌ Нет, не согласен(на)", callback_data="privacy_no")]
    ])
    return kb

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
        [InlineKeyboardButton(text="🔗 Вставить мой username", callback_data="use_my_username")]
    ])
    return kb

def admin_request_kb(request_id: int, phone: str, user_id: int):
    buttons = [
        [InlineKeyboardButton(text="💌 Написать", callback_data=f"admin_msg:{user_id}")],
        [InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"take:{request_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{request_id}")]
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

# ---------- Validation helpers ----------
NAME_RE = re.compile(r"^[А-Яа-яЁё\-\s]+$")
PHONE_RE = re.compile(r"^\+7\d{10}$")

def normalize_phone(p: Optional[str]) -> str:
    if not p:
        return "-"
    p = p.strip()
    if p.startswith("+"):
        digits = re.sub(r"\D", "", p)
        if not digits:
            return "-"
        return "+" + digits

    digits = re.sub(r"\D", "", p)
    if not digits:
        return "-"

    if AUTO_CONVERT_8_TO_7 and digits.startswith("8") and len(digits) >= 10:
        return "+7" + digits[1:]

    return "+" + digits

def tz_now_str() -> str:
    try:
        tz = ZoneInfo("Asia/Jerusalem")
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---------- Handlers ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Перед началом работы вы ознакомились с политикой обработки персональных данных?",
        reply_markup=privacy_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("privacy_"))
async def privacy_answer(cb: CallbackQuery):
    user_id = cb.from_user.id
    if cb.data == "privacy_yes":
        CONSENT_STORE[user_id] = True
        await cb.message.edit_text("✅ <b>Спасибо!</b>\n\nВы можете продолжить работу с ботом.", parse_mode="HTML")
        await bot.send_message(
            user_id,
            "🚗 <b>Что вы хотите сделать?</b>",
            reply_markup=main_user_keyboard(),
            parse_mode="HTML"
        )
    else:
        CONSENT_STORE[user_id] = False
        await cb.message.edit_text(
            "❌ К сожалению, без согласия на обработку персональных данных вы не можете пользоваться ботом.",
            parse_mode="HTML"
        )
    await cb.answer()

@dp.message(F.text == "💬 Написать в поддержку")
async def ask_support(message: Message):
    user_id = message.from_user.id
    if not CONSENT_STORE.get(user_id, False):
        await message.reply("❌ Сначала подтвердите обработку персональных данных через /start")
        return
    await message.reply(
        "📝 <b>Напишите вашу проблему или вопрос</b>\n\n"
        "Сообщение будет отправлено нашему менеджеру, и мы свяжемся с вами.",
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
        await message.reply("❌ Пожалуйста, введите ФИО кириллицей (например: Иванов Иван Иванович).")
        return
    if not NAME_RE.match(text):
        await message.reply("❌ ФИО должно содержать только кириллицу, пробелы и дефис. Пожалуйста, попробуйте ещё раз.")
        return
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        await message.reply("❌ Пожалуйста, введите минимум фамилию и имя (например: Иванов Иван).")
        return
    await state.update_data(name=text)
    await message.answer(
        "☎️ <b>Укажите номер телефона</b>\n\n"
        "Используйте международный формат (например +7...)",
        reply_markup=contact_request_kb(),
        parse_mode="HTML"
    )
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def process_phone(message: Message, state: FSMContext):
    phone_raw = None
    if getattr(message, "contact", None) and isinstance(message.contact, Contact):
        phone_raw = message.contact.phone_number
    else:
        phone_raw = message.text or ""
    phone = normalize_phone(phone_raw)
    if phone != "-" and not PHONE_RE.match(phone):
        await message.reply("❌ Неверный формат номера. Введите в формате +7... или используйте кнопку 'Отправить номер'.")
        return
    await state.update_data(phone=phone)
    await message.answer(
        "👤 <b>Ваш Telegram username</b>",
        reply_markup=username_inline_kb(),
        parse_mode="HTML"
    )
    await state.set_state(Form.username)

@dp.callback_query(F.data == "use_my_username")
async def use_my_username(cb: CallbackQuery, state: FSMContext):
    raw = cb.from_user.username or "-"
    if raw == "-":
        username = "-"
    else:
        username = raw if raw.startswith("@") else "@" + raw
    await state.update_data(username=username)
    await cb.answer()
    await cb.message.edit_text(f"✅ Username выбран: <b>{username}</b>", parse_mode="HTML")
    await bot.send_message(
        cb.from_user.id,
        "☎️ <b>Дополнительный номер телефона</b> (если есть)\n\n"
        "Введите в формате +7... или напишите '-' если не нужен",
        reply_markup=contact_request_kb(),
        parse_mode="HTML"
    )
    await state.set_state(Form.extra_phone)

@dp.message(Form.username)
async def process_username(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.reply("❌ Пожалуйста, введите username или нажмите кнопку 'Вставить мой username'.")
        return
    if text != '-' and not text.startswith('@'):
        text = '@' + text
    await state.update_data(username=text)
    await message.answer(
        "☎️ <b>Дополнительный номер телефона</b> (если есть)\n\n"
        "Введите в формате +7... или напишите '-' если не нужен",
        reply_markup=contact_request_kb(),
        parse_mode="HTML"
    )
    await state.set_state(Form.extra_phone)

@dp.message(Form.extra_phone)
async def process_extra_phone(message: Message, state: FSMContext):
    raw = None
    if getattr(message, "contact", None) and isinstance(message.contact, Contact):
        raw = message.contact.phone_number
    else:
        raw = message.text or ""
    
    if raw.strip() == "-":
        extra = "-"
    else:
        extra = normalize_phone(raw)
        if extra != "-" and not PHONE_RE.match(extra):
            await message.reply("❌ Неверный формат. Номер должен быть в формате +7 с 10-15 цифрами, или напишите '-'.")
            return
    
    await state.update_data(extra_phone=extra)
    await message.answer(
        "🚗 <b>Какую марку автомобиля вы хотите заказать?</b>\n\n"
        "(например: BMW X5, Mercedes-Benz GLE)",
        parse_mode="HTML"
    )
    await state.set_state(Form.brand_model)

@dp.message(Form.brand_model)
async def process_brand(message: Message, state: FSMContext):
    await state.update_data(brand_model=message.text or "-")
    await message.answer(
        "🎨 <b>Экстерьер</b>\n\n"
        "(цвет, состояние, пробег и т.д.)",
        parse_mode="HTML"
    )
    await state.set_state(Form.exterior)

@dp.message(Form.exterior)
async def process_exterior(message: Message, state: FSMContext):
    await state.update_data(exterior=message.text or "-")
    await message.answer(
        "🛋 <b>Интерьер</b>\n\n"
        "(материалы, состояние и т.д.)",
        parse_mode="HTML"
    )
    await state.set_state(Form.interior)

@dp.message(Form.interior)
async def process_interior(message: Message, state: FSMContext):
    await state.update_data(interior=message.text or "-")
    await message.answer(
        "📦 <b>Комплектация/Пакет</b>",
        parse_mode="HTML"
    )
    await state.set_state(Form.package)

@dp.message(Form.package)
async def process_package(message: Message, state: FSMContext):
    await state.update_data(package=message.text or "-")
    await message.answer(
        "💰 <b>Ваш бюджет</b>\n\n"
        "(включая логистику и растаможку)",
        parse_mode="HTML"
    )
    await state.set_state(Form.budget)

@dp.message(Form.budget)
async def process_budget(message: Message, state: FSMContext):
    await state.update_data(budget=message.text or "-")
    await message.answer(
        "📅 <b>Год выпуска</b>",
        parse_mode="HTML"
    )
    await state.set_state(Form.year)

@dp.message(Form.year)
async def process_year(message: Message, state: FSMContext):
    await state.update_data(year=message.text or "-")
    # priority question removed — сразу переходим к пожеланиям
    await message.answer(
        "✨ <b>Пожелания и комментарии</b>\n\n"
        "(если есть, или напишите '-')",
        parse_mode="HTML"
    )
    await state.set_state(Form.wishes)

@dp.message(Form.wishes)
async def process_wishes(message: Message, state: FSMContext):
    await state.update_data(wishes=message.text or "-")
    data = await state.get_data()
    user = message.from_user

    phones_combined = f"({data.get('phone','-')}), ({data.get('extra_phone','-')})"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            # priority column intentionally omitted from INSERT; DB has default value
            "INSERT INTO requests (user_id, username, name, phones, brand_model, exterior, interior, package, budget, year, wishes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                user.id,
                data.get('username', '-'),
                data.get('name', '-'),
                phones_combined,
                data.get('brand_model', '-'),
                data.get('exterior', '-'),
                data.get('interior', '-'),
                data.get('package', '-'),
                data.get('budget', '-'),
                data.get('year', '-'),
                data.get('wishes', '-')
            )
        )
        await db.commit()
        request_id = cursor.lastrowid

    timestamp = tz_now_str()
    row = [
        timestamp,
        data.get('name', '-'),
        phones_combined,
        data.get('username', '-'),
        data.get('brand_model', '-'),
        data.get('exterior', '-'),
        data.get('interior', '-'),
        data.get('package', '-'),
        data.get('budget', '-'),
        data.get('year', '-'),
        # priority omitted
        data.get('wishes', '-')
    ]
    sheet_row = await append_to_sheet(row)

    if sheet_row is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE requests SET sheet_row = ? WHERE id = ?", (sheet_row, request_id))
            await db.commit()

    await message.answer(
        "✅ <b>Спасибо!</b>\n\n"
        "Ваша заявка успешно отправлена 🎉\n"
        "Наш менеджер ЗАБУГОРНЫЙLUX свяжется с вами в ближайшее время!",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="HTML"
    )

    msg_text = (
        f"🆕 <b>Новая заявка #{request_id}</b>\n\n"
        f"👤 <b>ФИО:</b> {data.get('name')}\n"
        f"☎️ <b>Телефоны:</b> {phones_combined}\n"
        f"👤 <b>Username:</b> {data.get('username')}\n"
        f"🚗 <b>Марка/модель:</b> {data.get('brand_model')}\n"
        f"🎨 <b>Экстерьер:</b> {data.get('exterior')}\n"
        f"🛋 <b>Интерьер:</b> {data.get('interior')}\n"
        f"📦 <b>Комплектация:</b> {data.get('package')}\n"
        f"💰 <b>Бюджет:</b> {data.get('budget')}\n"
        f"📅 <b>Год:</b> {data.get('year')}\n"
        # priority line removed
        f"✨ <b>Пожелания:</b> {data.get('wishes')}\n"
    )
    for admin in ADMINS:
        try:
            await bot.send_message(
                admin,
                msg_text,
                reply_markup=admin_request_kb(request_id, phones_combined, user.id),
                parse_mode="HTML"
            )
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
    await cb.answer("✅ Заявка взята в работу")
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass

@dp.callback_query(F.data.startswith("delete:"))
async def delete_request(cb: CallbackQuery):
    req_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM requests WHERE id = ?", (req_id,))
        await db.commit()
    await cb.answer("✅ Заявка удалена")
    try:
        await cb.message.edit_text(cb.message.text + "\n\n<i>(заявка удалена)</i>", parse_mode="HTML")
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
        status_emoji = "🆕" if status == "new" else "⏳" if status == "in_progress" else "✅"
        text = f"#{req_id}\n👤 {name}\n🚗 {brand_model}\n☎️ {phones}\n{status_emoji} {status}"
        await message.reply(text, reply_markup=admin_request_kb(req_id, phones, 0))

@dp.message()
async def catch_all_messages(message: Message):
    user_id = message.from_user.id
    text = message.text or ""
    if SupportStateHolder.is_waiting(user_id):
        for admin in ADMINS:
            try:
                await bot.send_message(
                    admin,
                    (
                        "💬 <b>Сообщение в поддержку</b>\n\n"
                        f"От: {message.from_user.full_name} (@{message.from_user.username or 'нет username'})\n\n"
                        f"{text}"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning("Failed to forward support to admin %s: %s", admin, e)
        SupportStateHolder.remove(user_id)
        await message.answer(
            "✅ <b>Спасибо!</b>\n\nВаше сообщение отправлено в поддержку. Мы с Вами свяжимся.",
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
