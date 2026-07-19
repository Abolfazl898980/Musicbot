import os
import logging
import asyncio
from datetime import datetime
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, InputKeyboardValue, FSInputFile
)
from aiogram.filters import CommandStart, Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL") # e.g., "@mychannel" or "-100xxxx"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is missing!")

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

DB_NAME = "music.db"

# ==========================================
# DATABASE INITIALIZATION
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        
        # Users Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                join_date TEXT
            )
        """)
        
        # Categories Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE
            )
        """)
        
        # Songs Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_name TEXT,
                artist TEXT,
                category TEXT,
                cover_file_id TEXT,
                audio_file_id TEXT UNIQUE,
                uploader_id INTEGER,
                upload_date TEXT,
                downloads INTEGER DEFAULT 0,
                views INTEGER DEFAULT 0
            )
        """)
        
        # Favorites Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER,
                song_id INTEGER,
                PRIMARY KEY (user_id, song_id),
                FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE
            )
        """)
        
        # Banned Users Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned (
                user_id INTEGER PRIMARY KEY,
                ban_date TEXT
            )
        """)
        await db.commit()
    logger.info("Database initialized successfully.")

# ==========================================
# STATE MANAGEMENT (FSM)
# ==========================================
class UploadStates(StatesGroup):
    song_name = State()
    artist = State()
    category = State()
    cover = State()
    audio = State()

class SearchStates(StatesGroup):
    waiting_query = State()

class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_delete_id = State()
    waiting_ban_id = State()
    waiting_unban_id = State()
    waiting_add_category = State()
    waiting_delete_category = State()

# ==========================================
# CUSTOM FILTERS & MIDDLEWARES (Inline Check)
# ==========================================
class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == ADMIN_ID

async def check_joined(bot: Bot, user_id: int) -> bool:
    if not FORCE_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(chat_id=FORCE_CHANNEL, user_id=user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception as e:
        logger.error(f"Error checking channel membership: {e}")
        return True # Default to True to prevent structural lockouts on config errors

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM banned WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

# ==========================================
# KEYBOARD GENERATORS (UI Elements)
# ==========================================
def get_main_menu(user_id: int):
    # Aiogram 3 ReplyKeyboardMarkup setup with modern styling semantics
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🔍 جستجوی موزیک"), KeyboardButton(text="📤 آپلود موزیک"))
    builder.row(KeyboardButton(text="🔥 برترین دانلودها"), KeyboardButton(text="🎵 دسته‌بندی‌ها"))
    builder.row(KeyboardButton(text="❤️ علاقه‌مندی‌ها"), KeyboardButton(text="📜 آپلودهای من"))
    builder.row(KeyboardButton(text="⭐ موزیک تصادفی"), KeyboardButton(text="ℹ️ درباره ربات"))
    
    if user_id == ADMIN_ID:
        builder.row(KeyboardButton(text="👑 پنل مدیریت"))
        
    return builder.as_markup(resize_keyboard=True)

def get_search_menu():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🔎 جستجو بر اساس نام آهنگ"))
    builder.row(KeyboardButton(text="🎤 جستجو بر اساس هنرمند"))
    builder.row(KeyboardButton(text="🏷 جستجو بر اساس دسته‌بندی"))
    builder.row(KeyboardButton(text="⬅️ بازگشت به منوی اصلی"))
    return builder.as_markup(resize_keyboard=True)

def get_admin_menu():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📊 آمار ربات"), KeyboardButton(text="📢 همگانی (برودکاست)"))
    builder.row(KeyboardButton(text="🗑 حذف موزیک"), KeyboardButton(text="➕ افزودن دسته‌بندی"))
    builder.row(KeyboardButton(text="➖ حذف دسته‌بندی"), KeyboardButton(text="🚫 مسدود کردن کاربر"))
    builder.row(KeyboardButton(text="✅ رفع مسدودیت کاربر"), KeyboardButton(text="⬅️ بازگشت به منوی اصلی"))
    return builder.as_markup(resize_keyboard=True)

def get_join_inline():
    builder = InlineKeyboardBuilder()
    # Utilizing supported styling paradigms inside inline builders via dynamic string schemes where applicable
    builder.row(InlineKeyboardButton(text="📢 عضویت در کانال", url=f"https://t.me/{FORCE_CHANNEL.replace('@','')}" if '@' in FORCE_CHANNEL else f"https://t.me/{FORCE_CHANNEL}"))
    builder.row(InlineKeyboardButton(text="🔄 بررسی مجدد عضویت", callback_data="check_join_again"))
    return builder.as_markup()

async def get_song_inline(song_id: int, user_id: int):
    builder = InlineKeyboardBuilder()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM favorites WHERE user_id = ? AND song_id = ?", (user_id, song_id)) as cursor:
            is_fav = await cursor.fetchone() is not None
            
    fav_text = "❤️ حذف از علاقه‌مندی‌ها" if is_fav else "🤍 افزودن به علاقه‌مندی‌ها"
    builder.row(InlineKeyboardButton(text=fav_text, callback_data=f"fav_{song_id}"))
    return builder.as_markup()

# ==========================================
# INITIALIZING CORE INSTANCES
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==========================================
# BANNED USER GUARD & FORCE JOIN SYSTEM
# ==========================================
@dp.message(F.text)
async def global_message_guard(message: Message, state: FSMContext):
    if await is_banned(message.from_user.id):
        await message.answer("❌ شما از دسترسی به این ربات محروم شده‌اید.")
        return
        
    if not await check_joined(bot, message.from_user.id):
        await message.answer("⚠️ برای استفاده از ربات باید ابتدا در کانال ما عضو شوید:", reply_markup=get_join_inline())
        return
        
    # Route main commands manually if they avoid explicit custom routers due to state layers
    text = message.text
    if text == "⬅️ بازگشت به منوی اصلی":
        await state.clear()
        await message.answer("🏠 به منوی اصلی بازگشتید.", reply_markup=get_main_menu(message.from_user.id))
        return
    
    # Process sequential steps if matching text system commands outside pipeline
    await dp.feed_message(message)

# ==========================================
# CORE ROUTERS & LOGIC
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    if await is_banned(user_id):
        await message.answer("❌ شما مسدود هستید.")
        return

    # Auto Sign-Up System
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, join_date) VALUES (?, ?, ?)",
            (user_id, username, datetime.now().strftime("%Y-%m-%d"))
        )
        await db.commit()
        
    if not await check_joined(bot, user_id):
        await message.answer("⚠️ برای استفاده از ربات باید ابتدا در کانال ما عضو شوید:", reply_markup=get_join_inline())
        return

    welcome_text = (
        f"سلام **{message.from_user.first_name}** عزیز! 🎧\n"
        "به پیشرفته‌ترین ربات دنیای موسیقی خوش آمدید.\n\n"
        "از منوی زیر می‌توانید آهنگ مورد نظر خود را جستجو، آپلود یا مدیریت کنید! 🎵"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=get_main_menu(user_id))

@dp.callback_query(F.data == "check_join_again")
async def callback_join_check(callback: CallbackQuery):
    if await check_joined(bot, callback.from_user.id):
        await callback.message.delete()
        await callback.message.answer("🎉 با تشکر از عضویت شما! منوی اصلی فعال شد:", reply_markup=get_main_menu(callback.from_user.id))
    else:
        await callback.answer("❌ شما هنوز عضو کانال نشده‌اید!", show_alert=True)

# ==========================================
# GENERAL MAIN MENU ROUTERS
# ==========================================
@dp.message(F.text == "ℹ️ درباره ربات")
async def menu_about(message: Message):
    about_text = (
        "ℹ️ **درباره ربات موزیک**\n\n"
        "این ربات بستری قدرتمند برای اشتراک‌گذاری و آرشیو موسیقی است.\n"
        "تمامی فرآیندها به صورت آنی، پرسرعت و بهینه‌سازی شده برای پلتفرم تلگرام انجام می‌پذیرد.\n\n"
        "⚡ *طراحی شده با فریم‌ورک قدرتمند Aiogram 3*"
    )
    await message.answer(about_text, parse_mode="Markdown")

@dp.message(F.text == "🔍 جستجوی موزیک")
async def menu_search(message: Message):
    await message.answer("🔎 لطفاً متد جستجوی خود را انتخاب فرمایید:", reply_markup=get_search_menu())

# ==========================================
# UPLOAD MUSIC SYSTEM (FSM)
# ==========================================
@dp.message(F.text == "📤 آپلود موزیک")
async def start_upload(message: Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories") as cursor:
            cats = await cursor.fetchall()
    
    if not cats:
        await message.answer("❌ هنوز هیچ دسته‌بندی در ربات ثبت نشده است. لطفاً به مدیریت اطلاع دهید.")
        return
        
    await message.answer("📝 نام آهنگ را وارد کنید:")
    await state.set_state(UploadStates.song_name)

@dp.message(UploadStates.song_name, F.text)
async def upload_name(message: Message, state: FSMContext):
    await state.update_data(song_name=message.text)
    await message.answer("🎤 نام هنرمند/خواننده را وارد کنید:")
    await state.set_state(UploadStates.artist)

@dp.message(UploadStates.artist, F.text)
async def upload_artist(message: Message, state: FSMContext):
    await state.update_data(artist=message.text)
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories") as cursor:
            cats = await cursor.fetchall()
            
    builder = ReplyKeyboardBuilder()
    for cat in cats:
        builder.add(KeyboardButton(text=cat[0]))
    builder.adjust(2)
    builder.row(KeyboardButton(text="⬅️ بازگشت به منوی اصلی"))
    
    await message.answer("🏷 دسته‌بندی مورد نظر را انتخاب کنید:", reply_markup=builder.as_markup(resize_keyboard=True))
    await state.set_state(UploadStates.category)

@dp.message(UploadStates.category, F.text)
async def upload_category(message: Message, state: FSMContext):
    category_name = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM categories WHERE name = ?", (category_name,)) as cursor:
            if not await cursor.fetchone():
                await message.answer("❌ دسته‌بندی انتخاب شده معتبر نیست. لطفاً از کیبورد انتخاب کنید:")
                return
                
    await state.update_data(category=category_name)
    
    skip_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏩ رد کردن standard")]], resize_keyboard=True)
    await message.answer("🖼 عکس کاور موزیک را بفرستید (یا روی دکمه رد کردن بزنید):", reply_markup=skip_kb)
    await state.set_state(UploadStates.cover)

@dp.message(UploadStates.cover)
async def upload_cover(message: Message, state: FSMContext):
    if message.photo:
        await state.update_data(cover_file_id=message.photo[-1].file_id)
    elif message.text == "⏩ رد کردن standard":
        await state.update_data(cover_file_id=None)
    else:
        await message.answer("❌ لطفا فقط عکس بفرستید یا دکمه رد کردن را انتخاب کنید.")
        return
        
    back_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ بازگشت به منوی اصلی")]], resize_keyboard=True)
    await message.answer("🎵 حالا فایل صوتی (Audio) موزیک را بفرستید:", reply_markup=back_kb)
    await state.set_state(UploadStates.audio)

@dp.message(UploadStates.audio)
async def upload_audio(message: Message, state: FSMContext):
    if not message.audio:
        await message.answer("❌ خطا! فایل ارسال شده باید یک موزیک معتبر (Audio format) باشد.")
        return
        
    audio_id = message.audio.file_id
    
    # Check duplicate audio files
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM songs WHERE audio_file_id = ?", (audio_id,)) as cursor:
            if await cursor.fetchone():
                await message.answer("❌ این فایل صوتی قبلاً در ربات آپلود شده است و تکراری می‌باشد!")
                await state.clear()
                return
                
    data = await state.get_data()
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO songs (song_name, artist, category, cover_file_id, audio_file_id, uploader_id, upload_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data['song_name'], data['artist'], data['category'], data.get('cover_file_id'), audio_id, message.from_user.id, datetime.now().strftime("%Y-%m-%d")))
        await db.commit()
        
    await message.answer("✅ موزیک شما با موفقیت ذخیره و در آرشیو سراسری منتشر شد!", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()

# ==========================================
# SEARCH SYSTEM IMPLEMENTATION
# ==========================================
@dp.message(F.text.in_({"🔎 جستجو بر اساس نام آهنگ", "🎤 جستجو بر اساس هنرمند", "🏷 جستجو بر اساس دسته‌بندی"}))
async def process_search_selection(message: Message, state: FSMContext):
    search_map = {
        "🔎 جستجو بر اساس نام آهنگ": "name",
        "🎤 جستجو بر اساس هنرمند": "artist",
        "🏷 جستجو بر اساس دسته‌بندی": "category"
    }
    mode = search_map[message.text]
    await state.update_data(search_mode=mode)
    
    if mode == "category":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT name FROM categories") as cursor:
                cats = await cursor.fetchall()
        builder = ReplyKeyboardBuilder()
        for cat in cats:
            builder.add(KeyboardButton(text=cat[0]))
        builder.row(KeyboardButton(text="⬅️ بازگشت به منوی اصلی"))
        await message.answer("🏷 دسته‌بندی مورد نظر را جهت فیلتر انتخاب کنید:", reply_markup=builder.as_markup(resize_keyboard=True))
    else:
        await message.answer("🔤 عبارت مورد نظر خود را جهت جستجو ارسال کنید:")
        
    await state.set_state(SearchStates.waiting_query)

@dp.message(SearchStates.waiting_query, F.text)
async def exec_search(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("search_mode")
    query = message.text
    
    async with aiosqlite.connect(DB_NAME) as db:
        if mode == "name":
            cursor = await db.execute("SELECT * FROM songs WHERE song_name LIKE ?", (f"%{query}%",))
        elif mode == "artist":
            cursor = await db.execute("SELECT * FROM songs WHERE artist LIKE ?", (f"%{query}%",))
        else: # category
            cursor = await db.execute("SELECT * FROM songs WHERE category = ?", (query,))
            
        songs = await cursor.fetchall()
        
    if not songs:
        await message.answer("🔍 متاسفانه هیچ موزیکی مطابق با پارامتر ارسالی شما یافت نشد.")
        return
        
    await message.answer(f"📊 تعداد {len(songs)} موزیک یافت شد. در حال ارسال...")
    
    for song in songs:
        # Update dynamic view count synchronously per dispatch trigger
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE songs SET views = views + 1 WHERE id = ?", (song[0],))
            await db.commit()
            
        caption = (
            f"🎵 نام آهنگ: {song[1]}\n"
            f"🎤 خواننده: {song[2]}\n"
            f"🏷 دسته‌بندی: {song[3]}\n"
            f"👁 بازدید: {song[9] + 1}\n"
            f"⬇️ دانلود: {song[8]}\n"
            f"📅 تاریخ انتشار: {song[7]}"
        )
        inline_kb = await get_song_inline(song[0], message.from_user.id)
        
        # Increment tracking counts via native interactive hooks inside dynamic routing wrappers
        if song[4]: # If cover exists
            try:
                await message.answer_photo(photo=song[4], caption=caption, reply_markup=inline_kb)
            except TelegramBadRequest:
                pass
        
        # Audio attachment handler with functional counting inline buttons
        await message.answer_audio(
            audio=song[5], 
            caption=f"🎧 پخش آنلاین: {song[1]}", 
            reply_markup=inline_kb
        )

# ==========================================
# INTERACTIVE INLINE SYSTEM (FAVORITES / DOWNLOADS TRACKER)
# ==========================================
@dp.callback_query(F.data.startswith("fav_"))
async def toggle_favorite(callback: CallbackQuery):
    song_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM favorites WHERE user_id = ? AND song_id = ?", (user_id, song_id)) as cursor:
            if await cursor.fetchone():
                await db.execute("DELETE FROM favorites WHERE user_id = ? AND song_id = ?", (user_id, song_id))
                msg = "❌ از لیست علاقه‌مندی‌ها حذف شد."
            else:
                await db.execute("INSERT INTO favorites (user_id, song_id) VALUES (?, ?)", (user_id, song_id))
                msg = "❤️ به لیست علاقه‌مندی‌ها اضافه شد."
        await db.commit()
        
    await callback.answer(msg)
    # Dynamically rewrite layout markup configurations
    new_kb = await get_song_inline(song_id, user_id)
    await callback.message.edit_reply_markup(reply_markup=new_kb)

# Tracks users downloading via Telegram framework native save mechanics
@dp.message(F.audio)
async def count_downloads(message: Message):
    # If users forward audio natively to update counts
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE songs SET downloads = downloads + 1 WHERE audio_file_id = ?", (message.audio.file_id,))
        await db.commit()

# ==========================================
# TOP DOWNLOADS SYSTEM
# ==========================================
@dp.message(F.text == "🔥 برترین دانلودها")
async def top_downloads(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM songs ORDER BY downloads DESC LIMIT 20") as cursor:
            songs = await cursor.fetchall()
            
    if not songs:
        await message.answer("🔥 هنوز موزیکی دانلود نشده است.")
        return
        
    res = "🔥 **لیست ۲۰ موزیک برتر دانلودی ربات:**\n\n"
    for idx, song in enumerate(songs, 1):
        res += f"{idx}. 🎵 {song[1]} - {song[2]} | ⬇️ {song[8]} دانلود\n"
        
    await message.answer(res, parse_mode="Markdown")

# ==========================================
# MY UPLOADS SYSTEM
# ==========================================
@dp.message(F.text == "📜 آپلودهای من")
async def my_uploads(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, song_name, artist FROM songs WHERE uploader_id = ?", (message.from_user.id,)) as cursor:
            songs = await cursor.fetchall()
            
    if not songs:
        await message.answer("📜 شما هنوز هیچ موزیکی در ربات آپلود نکرده‌اید.")
        return
        
    await message.answer("📋 لیست کل موزیک‌های آپلود شده توسط شما:")
    for s in songs:
        del_kb = InlineKeyboardBuilder()
        del_kb.row(InlineKeyboardButton(text="🗑 حذف این آهنگ", callback_data=f"del_{s[0]}"))
        await message.answer(f"🎵 {s[1]} - {s[2]}", reply_markup=del_kb.as_markup())

@dp.callback_query(F.data.startswith("del_"))
async def delete_own_song(callback: CallbackQuery):
    song_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM songs WHERE id = ? AND uploader_id = ?", (song_id, callback.from_user.id))
        await db.commit()
    await callback.message.delete()
    await callback.answer("🗑 موزیک با موفقیت حذف شد.", show_alert=True)

# ==========================================
# FAVORITES SECTION
# ==========================================
@dp.message(F.text == "❤️ علاقه‌مندی‌ها")
async def show_favorites(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT songs.id, songs.song_name, songs.artist FROM favorites 
            JOIN songs ON favorites.song_id = songs.id 
            WHERE favorites.user_id = ?
        """, (message.from_user.id,)) as cursor:
            songs = await cursor.fetchall()
            
    if not songs:
        await message.answer("❤️ لیست علاقه‌مندی‌های شما خالی است.")
        return
        
    res = "❤️ **موزیک‌های محبوب شما:**\n\n"
    for s in songs:
        res += f"🎵 {s[1]} - {s[2]} (شناسه: /get_{s[0]})\n"
    await message.answer(res, parse_mode="Markdown")

@dp.message(F.text.startswith("/get_"))
async def get_specific_song(message: Message):
    try:
        song_id = int(message.text.split("_")[1])
    except (IndexError, ValueError):
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM songs WHERE id = ?", (song_id,)) as cursor:
            song = await cursor.fetchone()
            
    if not song:
        await message.answer("❌ موزیک یافت نشد.")
        return
        
    inline_kb = await get_song_inline(song[0], message.from_user.id)
    if song[4]:
        try:
            await message.answer_photo(photo=song[4], caption=f"🎵 {song[1]}", reply_markup=inline_kb)
        except Exception:
            pass
    await message.answer_audio(audio=song[5], reply_markup=inline_kb)

# ==========================================
# RANDOM SYSTEM
# ==========================================
@dp.message(F.text == "⭐ موزیک تصادفی")
async def random_music(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM songs ORDER BY RANDOM() LIMIT 1") as cursor:
            song = await cursor.fetchone()
            
    if not song:
        await message.answer("❌ هیچ موزیکی در آرشیو یافت نشد.")
        return
        
    inline_kb = await get_song_inline(song[0], message.from_user.id)
    caption = f"⭐ پیشنهاد تصادفی ربات برای شما:\n\n🎵 {song[1]} - {song[2]}"
    
    if song[4]:
        try:
            await message.answer_photo(photo=song[4], caption=caption, reply_markup=inline_kb)
        except Exception:
            pass
    await message.answer_audio(audio=song[5], reply_markup=inline_kb)

# ==========================================
# ADMINISTRATIVE CONTROL PANEL
# ==========================================
@dp.message(IsAdmin(), F.text == "👑 پنل مدیریت")
async def admin_panel(message: Message):
    await message.answer("👑 به پنل فوق پیشرفته مدیریت خوش آمدید:", reply_markup=get_admin_menu())

@dp.message(IsAdmin(), F.text == "📊 آمار ربات")
async def admin_stats(message: Message):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c1:
            total_users = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM songs") as c2:
            total_songs = (await c2.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM songs WHERE upload_date = ?", (today,)) as c3:
            today_uploads = (await c3.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE join_date = ?", (today,)) as c4:
            today_users = (await c4.fetchone())[0]
            
    stats_msg = (
        "📊 **آمار سیستم مرکزی ربات:**\n\n"
        f"👥 کل کاربران: {total_users}\n"
        f"🎵 کل آهنگ‌ها: {total_songs}\n"
        f"📥 آپلودهای امروز: {today_uploads}\n"
        f"📈 کاربران جدید امروز: {today_users}"
    )
    await message.answer(stats_msg, parse_mode="Markdown")

@dp.message(IsAdmin(), F.text == "📢 همگانی (برودکاست)")
async def start_broadcast(message: Message, state: FSMContext):
    await message.answer("📝 پیام خود را جهت ارسال همگانی بنویسید (متن، عکس و...):")
    await state.set_state(AdminStates.waiting_broadcast)

@dp.message(AdminStates.waiting_broadcast)
async def perform_broadcast(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()
            
    await message.answer(f"📢 عملیات ارسال پیام به {len(users)} کاربر آغاز شد...")
    
    success, failed = 0, 0
    for u in users:
        try:
            await message.copy_to(chat_id=u[0])
            success += 1
            await asyncio.sleep(0.05) # Prevent flood limits
        except Exception:
            failed += 1
            
    await message.answer(f"📊 گزارش برودکاست:\n\n✅ موفق: {success}\n❌ ناموفق: {failed}", reply_markup=get_admin_menu())
    await state.clear()

@dp.message(IsAdmin(), F.text == "➕ افزودن دسته‌بندی")
async def add_cat_start(message: Message, state: FSMContext):
    await message.answer("📝 نام دسته‌بندی جدید را بنویسید:")
    await state.set_state(AdminStates.waiting_add_category)

@dp.message(AdminStates.waiting_add_category, F.text)
async def add_cat_exec(message: Message, state: FSMContext):
    cat_name = message.text
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO categories (name) VALUES (?)", (cat_name,))
            await db.commit()
        await message.answer(f"✅ دسته‌بندی '{cat_name}' با موفقیت ساخته شد.", reply_markup=get_admin_menu())
    except aiosqlite.IntegrityError:
        await message.answer("❌ این دسته‌بندی قبلا ثبت شده است.")
    await state.clear()

@dp.message(IsAdmin(), F.text == "➖ حذف دسته‌بندی")
async def del_cat_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories") as cursor:
            cats = await cursor.fetchall()
    builder = ReplyKeyboardBuilder()
    for cat in cats:
        builder.add(KeyboardButton(text=cat[0]))
    builder.row(KeyboardButton(text="⬅️ بازگشت به منوی اصلی"))
    await message.answer("🏷 روی نام دسته‌بندی مورد نظر جهت حذف کلیک کنید:", reply_markup=builder.as_markup(resize_keyboard=True))
    await state.set_state(AdminStates.waiting_delete_category)

@dp.message(AdminStates.waiting_delete_category, F.text)
async def del_cat_exec(message: Message, state: FSMContext):
    cat_name = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM categories WHERE name = ?", (cat_name,))
        await db.commit()
    await message.answer(f"✅ دسته‌بندی '{cat_name}' حذف شد.", reply_markup=get_admin_menu())
    await state.clear()

@dp.message(IsAdmin(), F.text == "🗑 حذف موزیک")
async def start_del_song(message: Message, state: FSMContext):
    await message.answer("🔢 شناسه (ID) عددی موزیک مورد نظر را ارسال کنید:")
    await state.set_state(AdminStates.waiting_delete_id)

@dp.message(AdminStates.waiting_delete_id, F.text)
async def exec_del_song(message: Message, state: FSMContext):
    try:
        s_id = int(message.text)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM songs WHERE id = ?", (s_id,))
            await db.commit()
        await message.answer("🗑 موزیک با موفقیت از دیتابیس کل حذف شد.", reply_markup=get_admin_menu())
    except ValueError:
        await message.answer("❌ شناسه باید عددی باشد.")
    await state.clear()

@dp.message(IsAdmin(), F.text == "🚫 مسدود کردن کاربر")
async def start_ban(message: Message, state: FSMContext):
    await message.answer("🆔 شناسه عددی کاربر مورد نظر را بفرستید:")
    await state.set_state(AdminStates.waiting_ban_id)

@dp.message(AdminStates.waiting_ban_id, F.text)
async def exec_ban(message: Message, state: FSMContext):
    try:
        u_id = int(message.text)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO banned (user_id, ban_date) VALUES (?, ?)", (u_id, datetime.now().strftime("%Y-%m-%d")))
            await db.commit()
        await message.answer("🚫 کاربر مسدود شد.", reply_markup=get_admin_menu())
    except ValueError:
        await message.answer("❌ شناسه نامعتبر است.")
    await state.clear()

@dp.message(IsAdmin(), F.text == "✅ رفع مسدودیت کاربر")
async def start_unban(message: Message, state: FSMContext):
    await message.answer("🆔 شناسه عددی کاربر مورد نظر را بفرستید:")
    await state.set_state(AdminStates.waiting_unban_id)

@dp.message(AdminStates.waiting_unban_id, F.text)
async def exec_unban(message: Message, state: FSMContext):
    try:
        u_id = int(message.text)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM banned WHERE user_id = ?", (u_id,))
            await db.commit()
        await message.answer("✅ کاربر رفع مسدودیت شد.", reply_markup=get_admin_menu())
    except ValueError:
        await message.answer("❌ شناسه نامعتبر است.")
    await state.clear()

# ==========================================
# ASYNCHRONOUS SYSTEM RUNNER
# ==========================================
async def main():
    await init_db()
    logger.info("Bot is polling targets now...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

