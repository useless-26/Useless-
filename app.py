import asyncio
import os
import sys
import logging
import subprocess
import psutil
import sqlite3
import hashlib
import json
import zipfile
import shutil
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import aiohttp
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('BOT_TOKEN')
OWNER_ID_STR = os.getenv('OWNER_ID')
ADMIN_ID_STR = os.getenv('ADMIN_ID')
YOUR_USERNAME = os.getenv('YOUR_USERNAME')
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL')

if not TOKEN:
    logger.error("BOT_TOKEN not found in environment variables!")
    raise ValueError("BOT_TOKEN is required.")

if not OWNER_ID_STR or not ADMIN_ID_STR:
    logger.error("OWNER_ID or ADMIN_ID not found in environment variables!")
    raise ValueError("OWNER_ID and ADMIN_ID are required.")

try:
    OWNER_ID = int(OWNER_ID_STR)
    ADMIN_ID = int(ADMIN_ID_STR)
except ValueError:
    logger.error("OWNER_ID or ADMIN_ID must be valid integers!")
    raise

YOUR_USERNAME = YOUR_USERNAME or '@YourUsername'
UPDATE_CHANNEL = UPDATE_CHANNEL or 'https://t.me/YourChannel'

BASE_DIR = Path(__file__).parent.absolute()
UPLOAD_BOTS_DIR = BASE_DIR / 'upload_bots'
IROTECH_DIR = BASE_DIR / 'inf'
DATABASE_PATH = IROTECH_DIR / 'bot_data.db'

FREE_USER_LIMIT = 20
SUBSCRIBED_USER_LIMIT = 50
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

UPLOAD_BOTS_DIR.mkdir(exist_ok=True)
IROTECH_DIR.mkdir(exist_ok=True)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

bot_scripts = {}
user_subscriptions = {}
user_files = {}
user_favorites = {}
banned_users = set()
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False
bot_stats = {'total_uploads': 0, 'total_downloads': 0, 'total_runs': 0}

# ---- NEW: rate limiting & feedback & schedules & sharing ----
user_rate_limits = {}          # user_id -> list of timestamps
user_feedback = {}             # {user_id: [{file_name, rating, comment, date}]}
scheduled_tasks = {}           # task_id -> {user_id, file_name, run_at, repeat_hours, active}
pending_feedback = {}          # user_id -> file_name (waiting for feedback msg)
pending_schedule = {}          # user_id -> {file_name, step}
pending_share = {}             # user_id -> file_name
pending_broadcast = {}         # admin_id -> {text, run_at}  (scheduled broadcast)
RATE_LIMIT_WINDOW = 60         # seconds
RATE_LIMIT_MAX = 10            # max actions per window

# ============ DB INIT & MIGRATION ============

def migrate_db():
    logger.info("Running database migrations...")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute("PRAGMA table_info(user_files)")
        columns = [row[1] for row in c.fetchall()]
        if 'upload_date' not in columns:
            c.execute('ALTER TABLE user_files ADD COLUMN upload_date TEXT')
        c.execute("PRAGMA table_info(active_users)")
        columns = [row[1] for row in c.fetchall()]
        if 'join_date' not in columns:
            c.execute('ALTER TABLE active_users ADD COLUMN join_date TEXT')
        if 'last_active' not in columns:
            c.execute('ALTER TABLE active_users ADD COLUMN last_active TEXT')
        conn.commit()
        conn.close()
        logger.info("Migrations done.")
    except Exception as e:
        logger.error(f"Migration error: {e}", exc_info=True)

def init_db():
    logger.info(f"Initializing DB at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT, upload_date TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY, join_date TEXT, last_active TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                     (user_id INTEGER PRIMARY KEY, banned_date TEXT, reason TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS favorites
                     (user_id INTEGER, file_name TEXT, PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_stats
                     (stat_name TEXT PRIMARY KEY, stat_value INTEGER)''')
        # NEW TABLES
        c.execute('''CREATE TABLE IF NOT EXISTS feedback
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER, file_name TEXT, rating INTEGER,
                      comment TEXT, date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS scheduled_tasks
                     (task_id TEXT PRIMARY KEY, user_id INTEGER, file_name TEXT,
                      run_at TEXT, repeat_hours REAL, active INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS shared_files
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      from_user INTEGER, to_user INTEGER,
                      file_name TEXT, share_date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS webhook_configs
                     (user_id INTEGER PRIMARY KEY, url TEXT, secret TEXT, active INTEGER)''')

        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        for stat in ['total_uploads', 'total_downloads', 'total_runs']:
            c.execute('INSERT OR IGNORE INTO bot_stats (stat_name, stat_value) VALUES (?, 0)', (stat,))
        conn.commit()
        conn.close()
        logger.info("DB initialized.")
    except Exception as e:
        logger.error(f"DB init error: {e}", exc_info=True)

def load_data():
    logger.info("Loading data...")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for user_id, expiry in c.fetchall():
            try:
                user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
            except ValueError:
                pass
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id].append((file_name, file_type))
        c.execute('SELECT user_id FROM active_users')
        active_users.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id FROM banned_users')
        banned_users.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id, file_name FROM favorites')
        for user_id, file_name in c.fetchall():
            if user_id not in user_favorites:
                user_favorites[user_id] = []
            user_favorites[user_id].append(file_name)
        c.execute('SELECT stat_name, stat_value FROM bot_stats')
        for stat_name, stat_value in c.fetchall():
            bot_stats[stat_name] = stat_value
        # Load scheduled tasks
        c.execute('SELECT task_id, user_id, file_name, run_at, repeat_hours, active FROM scheduled_tasks')
        for task_id, user_id, file_name, run_at, repeat_hours, active in c.fetchall():
            scheduled_tasks[task_id] = {
                'user_id': user_id, 'file_name': file_name,
                'run_at': datetime.fromisoformat(run_at),
                'repeat_hours': repeat_hours, 'active': bool(active)
            }
        conn.close()
        logger.info(f"Loaded: {len(active_users)} users.")
    except Exception as e:
        logger.error(f"Load error: {e}", exc_info=True)

init_db()
migrate_db()
load_data()

# ============ HELPERS ============

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def check_rate_limit(user_id):
    now = datetime.now().timestamp()
    if user_id not in user_rate_limits:
        user_rate_limits[user_id] = []
    user_rate_limits[user_id] = [t for t in user_rate_limits[user_id] if now - t < RATE_LIMIT_WINDOW]
    if len(user_rate_limits[user_id]) >= RATE_LIMIT_MAX:
        return False
    user_rate_limits[user_id].append(now)
    return True

def is_premium(user_id):
    return user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now()

# ============ KEYBOARDS ============

def get_main_keyboard(user_id):
    if user_id in admin_ids:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Updates", url=UPDATE_CHANNEL)],
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file"),
             InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
            [InlineKeyboardButton(text="⭐ Favorites", callback_data="my_favorites"),
             InlineKeyboardButton(text="🔍 Search Files", callback_data="search_files")],
            [InlineKeyboardButton(text="⚡ Bot Speed", callback_data="bot_speed"),
             InlineKeyboardButton(text="📊 My Stats", callback_data="statistics")],
            [InlineKeyboardButton(text="ℹ️ Help & Info", callback_data="help_info"),
             InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
            [InlineKeyboardButton(text="⏰ Schedules", callback_data="my_schedules"),
             InlineKeyboardButton(text="📤 Share File", callback_data="share_file_menu")],
            [InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel"),
             InlineKeyboardButton(text="💬 Contact", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")]
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Updates Channel", url=UPDATE_CHANNEL)],
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file"),
             InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
            [InlineKeyboardButton(text="⭐ Favorites", callback_data="my_favorites"),
             InlineKeyboardButton(text="🔍 Search Files", callback_data="search_files")],
            [InlineKeyboardButton(text="⚡ Bot Speed", callback_data="bot_speed"),
             InlineKeyboardButton(text="📊 My Stats", callback_data="statistics")],
            [InlineKeyboardButton(text="⏰ Schedules", callback_data="my_schedules"),
             InlineKeyboardButton(text="📤 Share File", callback_data="share_file_menu")],
            [InlineKeyboardButton(text="💎 Get Premium", callback_data="get_premium"),
             InlineKeyboardButton(text="ℹ️ Help", callback_data="help_info")],
            [InlineKeyboardButton(text="🎯 Features", callback_data="all_features"),
             InlineKeyboardButton(text="💬 Contact Owner", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")]
        ])
    return keyboard

def get_admin_panel_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 User Stats", callback_data="admin_total_users"),
         InlineKeyboardButton(text="📁 Files Stats", callback_data="admin_total_files")],
        [InlineKeyboardButton(text="🚀 Running Scripts", callback_data="admin_running_scripts"),
         InlineKeyboardButton(text="💎 Premium Users", callback_data="admin_premium_users")],
        [InlineKeyboardButton(text="➕ Add Admin", callback_data="admin_add_admin"),
         InlineKeyboardButton(text="➖ Remove Admin", callback_data="admin_remove_admin")],
        [InlineKeyboardButton(text="🚫 Ban User", callback_data="admin_ban_user"),
         InlineKeyboardButton(text="✅ Unban User", callback_data="admin_unban_user")],
        [InlineKeyboardButton(text="📊 Bot Analytics", callback_data="admin_analytics"),
         InlineKeyboardButton(text="⚙️ System Info", callback_data="admin_system_status")],
        [InlineKeyboardButton(text="🔒 Lock/Unlock", callback_data="lock_bot"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton(text="📢 Schedule Broadcast", callback_data="admin_schedule_broadcast"),
         InlineKeyboardButton(text="👥 All Users List", callback_data="admin_all_users")],
        [InlineKeyboardButton(text="🗑️ Clean Files", callback_data="admin_clean_files"),
         InlineKeyboardButton(text="💾 Backup DB", callback_data="admin_backup_db")],
        [InlineKeyboardButton(text="📝 View Logs", callback_data="admin_view_logs"),
         InlineKeyboardButton(text="🔄 Restart Bot", callback_data="admin_restart_bot")],
        [InlineKeyboardButton(text="⚠️ Upload Notifs", callback_data="admin_upload_notifications"),
         InlineKeyboardButton(text="💰 Expiry Alerts", callback_data="admin_expiry_alerts")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    return keyboard

# ============ CORE HANDLERS ============

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if user_id in banned_users:
        await message.answer("🚫 <b>You are banned!</b>\n\nContact admin.", parse_mode="HTML")
        return
    active_users.add(user_id)
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute('INSERT OR REPLACE INTO active_users (user_id, join_date, last_active) VALUES (?, ?, ?)',
                  (user_id, now, now))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving active user: {e}")

    welcome_text = f"""
╔═══════════════════════╗
    🌟 <b>WELCOME TO FILE HOST BOT</b> 🌟
╚═══════════════════════╝

👋 <b>Hi,</b> {message.from_user.full_name}!

🆔 <b>Your ID:</b> <code>{user_id}</code>
📦 <b>Upload Limit:</b> {get_user_file_limit(user_id)} files
💎 <b>Account:</b> {'Premium ✨' if is_premium(user_id) else 'Free 🆓'}

━━━━━━━━━━━━━━━━━━━━
<b>🎯 FEATURES:</b>

📤 Upload • 📁 Manage • ▶️ Run Scripts
⏰ Auto-Schedule • 📤 Share Files
🔄 Auto-Restart • 📦 Requirements Install
💬 Rate Scripts • 📝 Download Logs

━━━━━━━━━━━━━━━━━━━━
<b>✨ Start exploring now! ✨</b>
"""
    await message.answer(welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")

@dp.callback_query(F.data == "back_to_main")
async def callback_back_to_main(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    welcome_text = f"""
╔═══════════════════════╗
    🏠 <b>MAIN MENU</b> 🏠
╚═══════════════════════╝

👤 <b>User:</b> {callback.from_user.full_name}
🆔 <b>ID:</b> <code>{user_id}</code>
📦 <b>Files:</b> {len(user_files.get(user_id, []))}/{get_user_file_limit(user_id)}
💎 <b>Account:</b> {'Premium ✨' if is_premium(user_id) else 'Free 🆓'}

Use buttons below to navigate 👇
"""
    await callback.message.edit_text(welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "upload_file")
async def callback_upload_file(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if bot_locked and user_id not in admin_ids:
        await callback.answer("🔒 Bot is locked for maintenance!", show_alert=True)
        return
    current_files = len(user_files.get(user_id, []))
    limit = get_user_file_limit(user_id)
    upload_text = f"""
╔═══════════════════════╗
    📤 <b>UPLOAD FILES</b> 📤
╚═══════════════════════╝

📊 <b>Current Usage:</b> {current_files}/{limit} files

📝 <b>Supported Formats:</b>
🐍 Python (.py)
🟨 JavaScript (.js)
📦 ZIP Archives (.zip)

💡 <b>Tips:</b>
• Include requirements.txt in ZIP for auto-install
• Script logs auto-saved after run
• Use Schedule to auto-run at set time
"""
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    await callback.message.edit_text(upload_text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "check_files")
async def callback_check_files(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    files = user_files.get(user_id, [])
    if not files:
        text = """
╔═══════════════════════╗
    📁 <b>MY FILES</b> 📁
╚═══════════════════════╝

📭 <b>No files found!</b>

Upload your first file to get started! 🚀
"""
        back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
        ])
    else:
        text = f"""
╔═══════════════════════╗
    📁 <b>MY FILES ({len(files)})</b> 📁
╚═══════════════════════╝

"""
        buttons = []
        for i, (file_name, file_type) in enumerate(files, 1):
            icon = "🐍" if file_type == "py" else "🟨" if file_type == "js" else "📦"
            text += f"{i}. {icon} <code>{file_name}</code>\n"
            is_favorite = file_name in user_favorites.get(user_id, [])
            star = "⭐" if is_favorite else "☆"
            buttons.append([
                InlineKeyboardButton(text=f"▶️ Run", callback_data=f"run_script:{file_name}"),
                InlineKeyboardButton(text=f"{star}", callback_data=f"toggle_fav:{file_name}"),
                InlineKeyboardButton(text="📝 Log", callback_data=f"download_log:{file_name}"),
            ])
            buttons.append([
                InlineKeyboardButton(text=f"ℹ️ Info", callback_data=f"file_info:{file_name}"),
                InlineKeyboardButton(text=f"⏰ Schedule", callback_data=f"schedule_file:{file_name}"),
                InlineKeyboardButton(text=f"🗑️ Del", callback_data=f"delete_file:{file_name}")
            ])
        buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")])
        back_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

# ============ FILE UPLOAD HANDLER ============

@dp.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    if user_id in banned_users:
        await message.answer("🚫 You are banned!")
        return
    if bot_locked and user_id not in admin_ids:
        await message.answer("🔒 Bot is currently locked!")
        return
    if not check_rate_limit(user_id):
        await message.answer("⚠️ <b>Rate limit reached!</b>\n\nPlease wait 60 seconds.", parse_mode="HTML")
        return

    document = message.document
    file_name = document.file_name
    file_ext = os.path.splitext(file_name)[1].lower()
    if file_ext not in ['.py', '.js', '.zip']:
        await message.answer("❌ Only .py, .js, and .zip files are supported!")
        return
    current_files = len(user_files.get(user_id, []))
    limit = get_user_file_limit(user_id)
    if current_files >= limit:
        await message.answer(f"❌ Upload limit reached! ({current_files}/{limit})\n\n💎 Upgrade to premium!")
        return

    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    user_folder.mkdir(exist_ok=True)
    file_path = user_folder / file_name

    try:
        file_size_kb = document.file_size / 1024
        status_msg = await message.answer(
            f"📤 <b>Uploading...</b>\n📄 <code>{file_name}</code>\n▓░░░░░░░░░ 0%",
            parse_mode="HTML"
        )
        await asyncio.sleep(0.3)
        await status_msg.edit_text(
            f"📥 <b>Downloading...</b>\n📄 <code>{file_name}</code>\n▓▓▓░░░░░░░ 30%",
            parse_mode="HTML"
        )
        await bot.download(document, destination=file_path)
        await status_msg.edit_text(
            f"💾 <b>Saving...</b>\n📄 <code>{file_name}</code>\n▓▓▓▓▓▓▓░░░ 70%",
            parse_mode="HTML"
        )

        # AUTO-INSTALL requirements.txt if ZIP
        install_msg = ""
        if file_ext == '.zip':
            try:
                with zipfile.ZipFile(file_path, 'r') as z:
                    z.extractall(user_folder)
                req_path = user_folder / 'requirements.txt'
                if req_path.exists():
                    install_msg = "\n📦 <b>Installing requirements...</b>"
                    await status_msg.edit_text(
                        f"📦 <b>Installing requirements.txt...</b>\n📄 <code>{file_name}</code>",
                        parse_mode="HTML"
                    )
                    result = subprocess.run(
                        [sys.executable, '-m', 'pip', 'install', '-r', str(req_path),
                         '--quiet', '--break-system-packages'],
                        capture_output=True, text=True, timeout=120
                    )
                    if result.returncode == 0:
                        install_msg = "\n✅ <b>Requirements installed!</b>"
                    else:
                        install_msg = f"\n⚠️ <b>Install partial:</b> {result.stderr[:100]}"
            except Exception as e:
                install_msg = f"\n⚠️ ZIP extract error: {str(e)[:80]}"

        if user_id not in user_files:
            user_files[user_id] = []
        if not any(f[0] == file_name for f in user_files[user_id]):
            user_files[user_id].append((file_name, file_ext[1:]))

        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?, ?, ?, ?)',
                  (user_id, file_name, file_ext[1:], now))
        c.execute('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?', ('total_uploads',))
        conn.commit()
        conn.close()
        bot_stats['total_uploads'] = bot_stats.get('total_uploads', 0) + 1

        # NOTIFY OWNER about new upload
        try:
            await bot.send_message(
                OWNER_ID,
                f"📤 <b>New Upload!</b>\n\n"
                f"👤 User: <code>{user_id}</code> ({message.from_user.full_name})\n"
                f"📄 File: <code>{file_name}</code>\n"
                f"💾 Size: {file_size_kb:.1f} KB\n"
                f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                parse_mode="HTML"
            )
        except Exception:
            pass

        if file_ext == '.zip':
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Extract ZIP", callback_data=f"extract_zip:{file_name}"),
                 InlineKeyboardButton(text="⭐ Favorite", callback_data=f"toggle_fav:{file_name}")],
                [InlineKeyboardButton(text="ℹ️ Info", callback_data=f"file_info:{file_name}"),
                 InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_file:{file_name}")],
                [InlineKeyboardButton(text="📁 My Files", callback_data="check_files"),
                 InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
            ])
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Run Now", callback_data=f"run_script:{file_name}"),
                 InlineKeyboardButton(text="⏰ Schedule", callback_data=f"schedule_file:{file_name}")],
                [InlineKeyboardButton(text="⭐ Favorite", callback_data=f"toggle_fav:{file_name}"),
                 InlineKeyboardButton(text="📤 Share", callback_data=f"share_this:{file_name}")],
                [InlineKeyboardButton(text="ℹ️ Info", callback_data=f"file_info:{file_name}"),
                 InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_file:{file_name}")],
                [InlineKeyboardButton(text="📁 My Files", callback_data="check_files"),
                 InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
            ])

        await status_msg.edit_text(
            f"""
╔═══════════════════════╗
    ✅ <b>UPLOAD SUCCESS!</b> ✅
╚═══════════════════════╝

📄 <b>File:</b> <code>{file_name}</code>
📦 <b>Type:</b> {file_ext[1:].upper()}
💾 <b>Size:</b> {file_size_kb:.2f} KB
📊 <b>Usage:</b> {current_files + 1}/{limit}{install_msg}

🎉 File uploaded successfully!
""",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await message.answer(f"❌ Upload failed: {str(e)}")

# ============ RUN SCRIPT ============

@dp.callback_query(F.data.startswith("run_script:"))
async def callback_run_script(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    if not check_rate_limit(user_id):
        await callback.answer("⚠️ Rate limit! Wait 60s.", show_alert=True)
        return
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    if not file_path.exists():
        await callback.answer("❌ File not found!", show_alert=True)
        return
    script_key = f"{user_id}_{file_name}"
    if script_key in bot_scripts:
        await callback.answer("⚠️ Already running!", show_alert=True)
        return
    file_ext = file_path.suffix.lower()
    try:
        log_file_path = user_folder / f"{file_path.stem}.log"
        log_file = open(log_file_path, 'w')
        if file_ext == '.py':
            process = subprocess.Popen(
                [sys.executable, str(file_path)],
                cwd=str(user_folder), stdout=log_file, stderr=log_file
            )
        elif file_ext == '.js':
            process = subprocess.Popen(
                ['node', str(file_path)],
                cwd=str(user_folder), stdout=log_file, stderr=log_file
            )
        else:
            log_file.close()
            await callback.answer("❌ Cannot run this file type!", show_alert=True)
            return

        bot_scripts[script_key] = {
            'process': process, 'file_name': file_name,
            'script_owner_id': user_id, 'start_time': datetime.now(),
            'user_folder': str(user_folder), 'type': file_ext[1:],
            'log_file': log_file, 'crash_restarts': 0
        }
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?', ('total_runs',))
        conn.commit()
        conn.close()
        bot_stats['total_runs'] = bot_stats.get('total_runs', 0) + 1

        await callback.answer(f"✅ Started! PID: {process.pid}", show_alert=True)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛑 Stop Script", callback_data=f"stop_script:{script_key}"),
             InlineKeyboardButton(text="📝 View Log", callback_data=f"download_log:{file_name}")],
            [InlineKeyboardButton(text="💬 Rate Script", callback_data=f"rate_script:{file_name}"),
             InlineKeyboardButton(text="📁 My Files", callback_data="check_files")]
        ])
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Run error: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("stop_script:"))
async def callback_stop_script(callback: types.CallbackQuery):
    script_key = callback.data.split(":", 1)[1]
    if script_key not in bot_scripts:
        await callback.answer("❌ Script not found or already stopped!", show_alert=True)
        return
    try:
        script_info = bot_scripts[script_key]
        process = script_info['process']
        log_file = script_info.get('log_file')
        if log_file and not log_file.closed:
            log_file.close()
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                child.terminate()
            parent.terminate()
        except Exception:
            pass
        del bot_scripts[script_key]
        await callback.answer("✅ Script stopped!", show_alert=True)
        await callback_back_to_main(callback)
    except Exception as e:
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

# ============ NEW: AUTO-RESTART on crash ============

async def monitor_scripts():
    """Background task: auto-restart scripts that crashed"""
    while True:
        await asyncio.sleep(15)
        for script_key, info in list(bot_scripts.items()):
            try:
                process = info['process']
                if process.poll() is not None:  # process ended
                    exit_code = process.returncode
                    user_id = info['script_owner_id']
                    file_name = info['file_name']
                    restarts = info.get('crash_restarts', 0)

                    log_file = info.get('log_file')
                    if log_file and not log_file.closed:
                        log_file.close()

                    if exit_code != 0 and restarts < 3:
                        # Auto-restart
                        await asyncio.sleep(2)
                        user_folder = Path(info['user_folder'])
                        file_path = user_folder / file_name
                        file_ext = file_path.suffix.lower()
                        new_log = open(user_folder / f"{file_path.stem}.log", 'a')
                        new_log.write(f"\n\n--- AUTO-RESTART #{restarts+1} at {datetime.now()} ---\n\n")

                        if file_ext == '.py':
                            new_process = subprocess.Popen(
                                [sys.executable, str(file_path)],
                                cwd=str(user_folder), stdout=new_log, stderr=new_log
                            )
                        else:
                            new_process = subprocess.Popen(
                                ['node', str(file_path)],
                                cwd=str(user_folder), stdout=new_log, stderr=new_log
                            )

                        bot_scripts[script_key] = {
                            **info,
                            'process': new_process,
                            'log_file': new_log,
                            'crash_restarts': restarts + 1,
                            'start_time': datetime.now()
                        }
                        try:
                            await bot.send_message(
                                user_id,
                                f"🔄 <b>Auto-Restart #{restarts+1}</b>\n\n"
                                f"📄 <code>{file_name}</code> crashed (exit: {exit_code})\n"
                                f"♻️ Restarted automatically!\n"
                                f"⚠️ Max 3 restarts allowed.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    else:
                        # Script finished normally or max restarts reached
                        del bot_scripts[script_key]
                        if exit_code != 0 and restarts >= 3:
                            try:
                                await bot.send_message(
                                    user_id,
                                    f"💀 <b>Script Stopped</b>\n\n"
                                    f"📄 <code>{file_name}</code> crashed 3 times.\n"
                                    f"❌ Auto-restart disabled. Check your script!",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
            except Exception as e:
                logger.error(f"Monitor error for {script_key}: {e}")

# ============ NEW: DOWNLOAD LOG ============

@dp.callback_query(F.data.startswith("download_log:"))
async def callback_download_log(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    stem = Path(file_name).stem
    log_path = user_folder / f"{stem}.log"
    if not log_path.exists() or log_path.stat().st_size == 0:
        await callback.answer("📝 No log yet! Run script first.", show_alert=True)
        return
    try:
        await callback.message.answer_document(
            FSInputFile(log_path),
            caption=f"📝 <b>Log: {file_name}</b>\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            parse_mode="HTML"
        )
        await callback.answer("✅ Log sent!")
    except Exception as e:
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

# ============ NEW: SCHEDULE ============

@dp.callback_query(F.data.startswith("schedule_file:"))
async def callback_schedule_file(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    pending_schedule[user_id] = {'file_name': file_name, 'step': 'delay'}
    text = f"""
╔═══════════════════════╗
    ⏰ <b>SCHEDULE SCRIPT</b> ⏰
╚═══════════════════════╝

📄 File: <code>{file_name}</code>

Choose when to run:
"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ In 5 min", callback_data=f"sched_delay:{file_name}:5"),
         InlineKeyboardButton(text="▶️ In 30 min", callback_data=f"sched_delay:{file_name}:30")],
        [InlineKeyboardButton(text="▶️ In 1 hour", callback_data=f"sched_delay:{file_name}:60"),
         InlineKeyboardButton(text="▶️ In 6 hours", callback_data=f"sched_delay:{file_name}:360")],
        [InlineKeyboardButton(text="🔁 Every 1 hr", callback_data=f"sched_repeat:{file_name}:1"),
         InlineKeyboardButton(text="🔁 Every 6 hrs", callback_data=f"sched_repeat:{file_name}:6")],
        [InlineKeyboardButton(text="🔁 Every 12 hrs", callback_data=f"sched_repeat:{file_name}:12"),
         InlineKeyboardButton(text="🔁 Every 24 hrs", callback_data=f"sched_repeat:{file_name}:24")],
        [InlineKeyboardButton(text="🏠 Cancel", callback_data="back_to_main")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("sched_delay:"))
async def callback_sched_delay(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    file_name = parts[1]
    minutes = int(parts[2])
    run_at = datetime.now() + timedelta(minutes=minutes)
    task_id = f"{user_id}_{file_name}_{int(run_at.timestamp())}"
    scheduled_tasks[task_id] = {
        'user_id': user_id, 'file_name': file_name,
        'run_at': run_at, 'repeat_hours': 0, 'active': True
    }
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO scheduled_tasks VALUES (?,?,?,?,?,?)',
                  (task_id, user_id, file_name, run_at.isoformat(), 0, 1))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Schedule DB error: {e}")
    await callback.answer(f"⏰ Scheduled in {minutes} min!", show_alert=True)
    await callback_back_to_main(callback)

@dp.callback_query(F.data.startswith("sched_repeat:"))
async def callback_sched_repeat(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    file_name = parts[1]
    hours = float(parts[2])
    run_at = datetime.now() + timedelta(hours=hours)
    task_id = f"{user_id}_{file_name}_repeat_{int(run_at.timestamp())}"
    scheduled_tasks[task_id] = {
        'user_id': user_id, 'file_name': file_name,
        'run_at': run_at, 'repeat_hours': hours, 'active': True
    }
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO scheduled_tasks VALUES (?,?,?,?,?,?)',
                  (task_id, user_id, file_name, run_at.isoformat(), hours, 1))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Schedule DB error: {e}")
    await callback.answer(f"🔁 Repeating every {hours}h!", show_alert=True)
    await callback_back_to_main(callback)

@dp.callback_query(F.data == "my_schedules")
async def callback_my_schedules(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_tasks = {k: v for k, v in scheduled_tasks.items() if v['user_id'] == user_id and v['active']}
    if not user_tasks:
        text = "⏰ <b>No active schedules!</b>\n\nUse 'Schedule' button on any file."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
        ])
    else:
        text = f"⏰ <b>MY SCHEDULES ({len(user_tasks)})</b>\n\n"
        buttons = []
        for task_id, info in user_tasks.items():
            time_left = (info['run_at'] - datetime.now()).total_seconds()
            if time_left < 0:
                time_str = "Running soon..."
            elif time_left < 3600:
                time_str = f"In {int(time_left/60)}m"
            else:
                time_str = f"In {time_left/3600:.1f}h"
            repeat = f" 🔁{info['repeat_hours']}h" if info['repeat_hours'] else ""
            text += f"📄 <code>{info['file_name']}</code>{repeat}\n⏱ {time_str}\n\n"
            buttons.append([InlineKeyboardButton(
                text=f"❌ Cancel {info['file_name'][:20]}",
                callback_data=f"cancel_schedule:{task_id}"
            )])
        buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("cancel_schedule:"))
async def callback_cancel_schedule(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    if task_id in scheduled_tasks:
        scheduled_tasks[task_id]['active'] = False
        del scheduled_tasks[task_id]
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            c = conn.cursor()
            c.execute('DELETE FROM scheduled_tasks WHERE task_id = ?', (task_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
    await callback.answer("✅ Schedule cancelled!", show_alert=True)
    await callback_my_schedules(callback)

async def run_scheduled_tasks():
    """Background: run scheduled scripts"""
    while True:
        await asyncio.sleep(30)
        now = datetime.now()
        for task_id, info in list(scheduled_tasks.items()):
            if not info['active']:
                continue
            if info['run_at'] <= now:
                user_id = info['user_id']
                file_name = info['file_name']
                user_folder = UPLOAD_BOTS_DIR / str(user_id)
                file_path = user_folder / file_name
                if file_path.exists():
                    script_key = f"{user_id}_{file_name}"
                    if script_key not in bot_scripts:
                        try:
                            log_file_path = user_folder / f"{Path(file_name).stem}.log"
                            log_file = open(log_file_path, 'a')
                            log_file.write(f"\n--- SCHEDULED RUN at {now} ---\n")
                            file_ext = file_path.suffix.lower()
                            if file_ext == '.py':
                                process = subprocess.Popen(
                                    [sys.executable, str(file_path)],
                                    cwd=str(user_folder), stdout=log_file, stderr=log_file
                                )
                            else:
                                process = subprocess.Popen(
                                    ['node', str(file_path)],
                                    cwd=str(user_folder), stdout=log_file, stderr=log_file
                                )
                            bot_scripts[script_key] = {
                                'process': process, 'file_name': file_name,
                                'script_owner_id': user_id, 'start_time': now,
                                'user_folder': str(user_folder), 'type': file_ext[1:],
                                'log_file': log_file, 'crash_restarts': 0
                            }
                            await bot.send_message(
                                user_id,
                                f"⏰ <b>Scheduled Run!</b>\n📄 <code>{file_name}</code> started.\nPID: {process.pid}",
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"Scheduled run error: {e}")

                # Repeat or remove
                if info['repeat_hours'] > 0:
                    scheduled_tasks[task_id]['run_at'] = now + timedelta(hours=info['repeat_hours'])
                    try:
                        conn = sqlite3.connect(DATABASE_PATH)
                        c = conn.cursor()
                        c.execute('UPDATE scheduled_tasks SET run_at=? WHERE task_id=?',
                                  (scheduled_tasks[task_id]['run_at'].isoformat(), task_id))
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass
                else:
                    scheduled_tasks[task_id]['active'] = False
                    del scheduled_tasks[task_id]
                    try:
                        conn = sqlite3.connect(DATABASE_PATH)
                        c = conn.cursor()
                        c.execute('DELETE FROM scheduled_tasks WHERE task_id=?', (task_id,))
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass

# ============ NEW: FILE SHARING ============

@dp.callback_query(F.data == "share_file_menu")
async def callback_share_file_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    files = user_files.get(user_id, [])
    if not files:
        await callback.answer("❌ No files to share!", show_alert=True)
        return
    text = "📤 <b>SHARE FILE</b>\n\nChoose a file to share:"
    buttons = []
    for file_name, file_type in files:
        icon = "🐍" if file_type == "py" else "🟨" if file_type == "js" else "📦"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {file_name[:30]}",
            callback_data=f"share_this:{file_name}"
        )])
    buttons.append([InlineKeyboardButton(text="🏠 Cancel", callback_data="back_to_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("share_this:"))
async def callback_share_this(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    pending_share[user_id] = file_name
    await callback.message.edit_text(
        f"📤 <b>Share: <code>{file_name}</code></b>\n\n"
        f"Send the <b>User ID</b> of the person you want to share with:\n\n"
        f"<i>Type their Telegram User ID as a number</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="back_to_main")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(F.text.regexp(r'^\d+$'))
async def handle_numeric_input(message: types.Message):
    user_id = message.from_user.id
    # Handle share target user ID
    if user_id in pending_share:
        file_name = pending_share.pop(user_id)
        target_id = int(message.text.strip())
        if target_id == user_id:
            await message.answer("❌ Cannot share with yourself!")
            return
        if target_id not in active_users:
            await message.answer("❌ User not found! They must have used this bot first.")
            return
        # Copy file to target
        src = UPLOAD_BOTS_DIR / str(user_id) / file_name
        dst_folder = UPLOAD_BOTS_DIR / str(target_id)
        dst_folder.mkdir(exist_ok=True)
        dst = dst_folder / file_name
        if not src.exists():
            await message.answer("❌ File not found!")
            return
        try:
            shutil.copy2(str(src), str(dst))
            file_ext = Path(file_name).suffix[1:]
            if target_id not in user_files:
                user_files[target_id] = []
            if not any(f[0] == file_name for f in user_files[target_id]):
                user_files[target_id].append((file_name, file_ext))
                conn = sqlite3.connect(DATABASE_PATH)
                c = conn.cursor()
                c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?,?,?,?)',
                          (target_id, file_name, file_ext, datetime.now().isoformat()))
                c.execute('INSERT INTO shared_files (from_user, to_user, file_name, share_date) VALUES (?,?,?,?)',
                          (user_id, target_id, file_name, datetime.now().isoformat()))
                conn.commit()
                conn.close()
            await message.answer(f"✅ <b>Shared!</b>\n📄 <code>{file_name}</code> → User <code>{target_id}</code>", parse_mode="HTML")
            try:
                await bot.send_message(
                    target_id,
                    f"📤 <b>File Shared With You!</b>\n\n"
                    f"📄 <code>{file_name}</code>\n"
                    f"👤 From: User <code>{user_id}</code>\n\n"
                    f"Go to 📁 My Files to use it!",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        except Exception as e:
            await message.answer(f"❌ Share failed: {str(e)}")
        return

    # Handle scheduled broadcast time (admin)
    if user_id in admin_ids and user_id in pending_broadcast:
        if pending_broadcast[user_id].get('waiting_minutes'):
            minutes = int(message.text.strip())
            text = pending_broadcast[user_id]['text']
            del pending_broadcast[user_id]
            run_at = datetime.now() + timedelta(minutes=minutes)
            asyncio.create_task(send_scheduled_broadcast(text, run_at))
            await message.answer(f"✅ <b>Broadcast scheduled in {minutes} min!</b>", parse_mode="HTML")
            return

@dp.message(F.text)
async def handle_text_input(message: types.Message):
    user_id = message.from_user.id

    # Feedback comment input
    if user_id in pending_feedback:
        file_name = pending_feedback.pop(user_id)
        comment = message.text.strip()
        if user_id not in user_feedback:
            user_feedback[user_id] = []
        # Update last feedback entry with comment
        for fb in reversed(user_feedback[user_id]):
            if fb['file_name'] == file_name and not fb.get('comment'):
                fb['comment'] = comment
                break
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            c = conn.cursor()
            c.execute('UPDATE feedback SET comment=? WHERE user_id=? AND file_name=? AND comment IS NULL ORDER BY id DESC LIMIT 1',
                      (comment, user_id, file_name))
            conn.commit()
            conn.close()
        except Exception:
            pass
        await message.answer("✅ <b>Feedback saved! Thank you!</b>", parse_mode="HTML",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
                             ]))
        return

    # Scheduled broadcast text (admin)
    if user_id in admin_ids and user_id in pending_broadcast:
        if pending_broadcast[user_id].get('waiting_text'):
            pending_broadcast[user_id]['text'] = message.text
            pending_broadcast[user_id]['waiting_text'] = False
            pending_broadcast[user_id]['waiting_minutes'] = True
            await message.answer("⏱ <b>How many minutes from now?</b>\n\nSend a number:", parse_mode="HTML")
            return

# ============ NEW: FEEDBACK / RATING ============

@dp.callback_query(F.data.startswith("rate_script:"))
async def callback_rate_script(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    text = f"💬 <b>Rate Script</b>\n📄 <code>{file_name}</code>\n\nHow was it?"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐", callback_data=f"give_rating:{file_name}:1"),
         InlineKeyboardButton(text="⭐⭐", callback_data=f"give_rating:{file_name}:2"),
         InlineKeyboardButton(text="⭐⭐⭐", callback_data=f"give_rating:{file_name}:3")],
        [InlineKeyboardButton(text="⭐⭐⭐⭐", callback_data=f"give_rating:{file_name}:4"),
         InlineKeyboardButton(text="⭐⭐⭐⭐⭐", callback_data=f"give_rating:{file_name}:5")],
        [InlineKeyboardButton(text="❌ Skip", callback_data="back_to_main")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("give_rating:"))
async def callback_give_rating(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    file_name = parts[1]
    rating = int(parts[2])
    stars = "⭐" * rating
    if user_id not in user_feedback:
        user_feedback[user_id] = []
    user_feedback[user_id].append({'file_name': file_name, 'rating': rating, 'comment': None, 'date': datetime.now().isoformat()})
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT INTO feedback (user_id, file_name, rating, comment, date) VALUES (?,?,?,?,?)',
                  (user_id, file_name, rating, None, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Feedback error: {e}")
    pending_feedback[user_id] = file_name
    await callback.message.edit_text(
        f"✅ <b>Rated {stars}!</b>\n\n💬 <b>Leave a comment?</b>\n<i>Type your comment or send /skip</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip Comment", callback_data="skip_feedback")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "skip_feedback")
async def callback_skip_feedback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id in pending_feedback:
        del pending_feedback[user_id]
    await callback.answer("✅ Feedback saved!")
    await callback_back_to_main(callback)

# ============ EXISTING HANDLERS (kept intact) ============

@dp.callback_query(F.data == "my_favorites")
async def callback_my_favorites(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    favorites = user_favorites.get(user_id, [])
    if not favorites:
        text = "⭐ <b>No favorites yet!</b>\n\nAdd files to favorites for quick access!"
        buttons = [[InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]]
    else:
        text = f"⭐ <b>FAVORITES ({len(favorites)})</b>\n\n"
        buttons = []
        for i, file_name in enumerate(favorites, 1):
            text += f"{i}. ⭐ <code>{file_name}</code>\n"
            buttons.append([
                InlineKeyboardButton(text=f"▶️ {file_name[:20]}", callback_data=f"run_script:{file_name}"),
                InlineKeyboardButton(text="❌", callback_data=f"toggle_fav:{file_name}")
            ])
        buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "search_files")
async def callback_search_files(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🔍 <b>Search Files</b>\n\nUse command:\n<code>/search filename</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "bot_speed")
async def callback_bot_speed(callback: types.CallbackQuery):
    start = datetime.now()
    await callback.answer("⚡ Pong!")
    ms = (datetime.now() - start).total_seconds() * 1000
    await callback.message.edit_text(
        f"⚡ <b>Bot Speed</b>\n\n🏓 Response: {ms:.0f}ms\n✅ Bot is online!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "statistics")
async def callback_statistics(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    files = len(user_files.get(user_id, []))
    favs = len(user_favorites.get(user_id, []))
    running = sum(1 for k in bot_scripts if k.startswith(f"{user_id}_"))
    schedules = sum(1 for v in scheduled_tasks.values() if v['user_id'] == user_id and v['active'])
    feedback_count = len(user_feedback.get(user_id, []))
    text = f"""
╔═══════════════════════╗
    📊 <b>YOUR STATISTICS</b>
╚═══════════════════════╝

🆔 ID: <code>{user_id}</code>
📦 Files: {files}/{get_user_file_limit(user_id)}
⭐ Favorites: {favs}
🚀 Running: {running}
⏰ Schedules: {schedules}
💬 Feedback Given: {feedback_count}
💎 Account: {'Premium ✨' if is_premium(user_id) else 'Free 🆓'}
"""
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "help_info")
async def callback_help_info(callback: types.CallbackQuery):
    text = """
╔═══════════════════════╗
    ℹ️ <b>HELP & INFO</b>
╚═══════════════════════╝

<b>🎯 HOW TO USE:</b>

1️⃣ <b>Upload Files</b> → Send .py/.js/.zip
2️⃣ <b>Run Scripts</b> → Click ▶️ Run
3️⃣ <b>Schedule</b> → Click ⏰ Schedule
4️⃣ <b>Share</b> → Click 📤 Share File
5️⃣ <b>View Logs</b> → Click 📝 Log
6️⃣ <b>Rate Scripts</b> → Click 💬 Rate

━━━━━━━━━━━━━━━━━━━━
<b>💡 COMMANDS:</b>
/start /help /search /stats /premium

<b>🔄 Auto-Restart:</b> Scripts auto-restart on crash (max 3x)
<b>📦 Auto-Install:</b> Include requirements.txt in ZIP
<b>⏰ Schedules:</b> One-time or repeating runs
<b>📤 Share:</b> Share files with other users
"""
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "all_features")
async def callback_all_features(callback: types.CallbackQuery):
    text = """
╔═══════════════════════╗
    🎯 <b>ALL FEATURES</b>
╚═══════════════════════╝

<b>✨ FREE FEATURES:</b>
📤 Upload .py/.js/.zip
📁 Manage Files | ⭐ Favorites
▶️ Run Scripts | 🛑 Stop
📝 Download Logs | 🔄 Auto-Restart
⏰ Schedule Runs | 📤 Share Files
💬 Rate Scripts | 🔍 Search
📊 Stats | ⚡ Speed Test

<b>📦 AUTO-INSTALL:</b>
Include requirements.txt in ZIP → auto pip install!

<b>💎 PREMIUM:</b>
• 50 file limit (vs 20)
• Priority support
• Premium badge
"""
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Get Premium", callback_data="get_premium")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "get_premium")
async def callback_get_premium(callback: types.CallbackQuery):
    text = """
╔═══════════════════════╗
    💎 <b>PREMIUM PLAN</b>
╚═══════════════════════╝

<b>✨ BENEFITS:</b>
📦 50 File Limit (vs 20)
⚡ Priority Processing
📊 Advanced Analytics
💬 Priority Support
⭐ Premium Badge

<b>💰 PRICING:</b>
1 Month: $5
3 Months: $12
1 Year: $40

<b>Contact owner to upgrade! 💬</b>
"""
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Contact Owner", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_fav:"))
async def callback_toggle_favorite(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    if user_id not in user_favorites:
        user_favorites[user_id] = []
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        if file_name in user_favorites[user_id]:
            user_favorites[user_id].remove(file_name)
            c.execute('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            await callback.answer("❌ Removed from favorites!", show_alert=True)
        else:
            user_favorites[user_id].append(file_name)
            c.execute('INSERT OR IGNORE INTO favorites (user_id, file_name) VALUES (?, ?)', (user_id, file_name))
            await callback.answer("⭐ Added to favorites!", show_alert=True)
        conn.commit()
        conn.close()
        await callback_check_files(callback)
    except Exception as e:
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("file_info:"))
async def callback_file_info(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    if not file_path.exists():
        await callback.answer("❌ File not found!", show_alert=True)
        return
    file_size = file_path.stat().st_size
    modified_time = datetime.fromtimestamp(file_path.stat().st_mtime)
    is_fav = file_name in user_favorites.get(user_id, [])
    # Avg rating
    fbs = [f for f in user_feedback.get(user_id, []) if f['file_name'] == file_name]
    avg_rating = f"{sum(f['rating'] for f in fbs)/len(fbs):.1f}⭐" if fbs else "No ratings"
    text = f"""
╔═══════════════════════╗
    ℹ️ <b>FILE INFO</b>
╚═══════════════════════╝

📄 <b>Name:</b> <code>{file_name}</code>
💾 <b>Size:</b> {file_size/1024:.2f} KB
📅 <b>Modified:</b> {modified_time.strftime('%Y-%m-%d %H:%M')}
⭐ <b>Favorite:</b> {'Yes' if is_fav else 'No'}
💬 <b>Avg Rating:</b> {avg_rating}
🔐 <b>MD5:</b> <code>{hashlib.md5(file_path.read_bytes()).hexdigest()[:16]}...</code>
"""
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Run", callback_data=f"run_script:{file_name}"),
         InlineKeyboardButton(text="⏰ Schedule", callback_data=f"schedule_file:{file_name}")],
        [InlineKeyboardButton(text="📝 Log", callback_data=f"download_log:{file_name}"),
         InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_file:{file_name}")],
        [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_file:"))
async def callback_delete_file(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    script_key = f"{user_id}_{file_name}"
    if script_key in bot_scripts:
        await callback.answer("❌ Stop the script first!", show_alert=True)
        return
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    if file_path.exists():
        file_path.unlink()
    log_path = user_folder / f"{Path(file_name).stem}.log"
    if log_path.exists():
        log_path.unlink()
    if user_id in user_files:
        user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM user_files WHERE user_id=? AND file_name=?', (user_id, file_name))
        c.execute('DELETE FROM favorites WHERE user_id=? AND file_name=?', (user_id, file_name))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Delete error: {e}")
    await callback.answer("✅ Deleted!", show_alert=True)
    await callback_check_files(callback)

@dp.callback_query(F.data.startswith("extract_zip:"))
async def callback_extract_zip(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    zip_path = user_folder / file_name
    if not zip_path.exists():
        await callback.answer("❌ ZIP not found!", show_alert=True)
        return
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(user_folder)
        await callback.answer("✅ Extracted!", show_alert=True)
        await callback.message.edit_text(
            f"✅ <b>ZIP Extracted!</b>\n📦 <code>{file_name}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

# ============ ADMIN PANEL ============

@dp.callback_query(F.data == "admin_panel")
async def callback_admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin access required!", show_alert=True)
        return
    await callback.message.edit_text(
        "👑 <b>ADMIN PANEL</b>\n\nManage users, files, and system.",
        reply_markup=get_admin_panel_keyboard(), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_total_users")
async def callback_admin_total_users(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    user_list = "\n".join([f"• <code>{uid}</code>" for uid in list(active_users)[:15]])
    text = f"""
👥 <b>USER STATISTICS</b>

📊 Total: {len(active_users)}
🚫 Banned: {len(banned_users)}
✅ Active: {len(active_users) - len(banned_users)}

<b>Recent (15):</b>
{user_list}
{'...' if len(active_users) > 15 else ''}
"""
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Full List", callback_data="admin_all_users")],
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ]), parse_mode="HTML")
    await callback.answer()

# NEW: All Users Full List
@dp.callback_query(F.data == "admin_all_users")
async def callback_admin_all_users(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    text = f"👥 <b>ALL USERS ({len(active_users)})</b>\n\n"
    for uid in list(active_users):
        is_ban = "🚫" if uid in banned_users else "✅"
        is_prem = "💎" if is_premium(uid) else ""
        is_adm = "👑" if uid in admin_ids else ""
        files = len(user_files.get(uid, []))
        text += f"{is_ban}{is_adm}{is_prem} <code>{uid}</code> | {files} files\n"
        if len(text) > 3500:
            text += "\n<i>...truncated</i>"
            break
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_total_files")
async def callback_admin_total_files(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    total = sum(len(f) for f in user_files.values())
    py = sum(1 for files in user_files.values() for f in files if f[1] == 'py')
    js = sum(1 for files in user_files.values() for f in files if f[1] == 'js')
    zp = sum(1 for files in user_files.values() for f in files if f[1] == 'zip')
    text = f"📁 <b>FILE STATS</b>\n\nTotal: {total}\n🐍 Python: {py}\n🟨 JS: {js}\n📦 ZIP: {zp}\n"
    top = sorted(user_files.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    text += "\n<b>Top Users:</b>\n"
    for uid, files in top:
        text += f"• <code>{uid}</code>: {len(files)} files\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_running_scripts")
async def callback_admin_running_scripts(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    if not bot_scripts:
        text = "🚀 <b>No running scripts</b>"
        buttons = [[InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]]
    else:
        text = f"🚀 <b>RUNNING ({len(bot_scripts)})</b>\n\n"
        buttons = []
        for sk, info in bot_scripts.items():
            rt = int((datetime.now() - info['start_time']).total_seconds())
            text += f"🔸 <code>{info['file_name']}</code>\nPID:{info['process'].pid} User:{info['script_owner_id']} {rt}s\n\n"
            buttons.append([InlineKeyboardButton(text=f"🛑 Stop {info['file_name'][:15]}", callback_data=f"stop_script:{sk}")])
        buttons.append([InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_premium_users")
async def callback_admin_premium_users(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    premium = [(u, d) for u, d in user_subscriptions.items() if d['expiry'] > datetime.now()]
    if not premium:
        text = "💎 <b>No active premium users</b>"
    else:
        text = f"💎 <b>PREMIUM ({len(premium)})</b>\n\n"
        for uid, data in premium:
            days_left = (data['expiry'] - datetime.now()).days
            text += f"💎 <code>{uid}</code> — {days_left}d left\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Premium", callback_data="add_premium")],
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_analytics")
async def callback_admin_analytics(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    total_feedback = sum(len(v) for v in user_feedback.values())
    text = f"""
📊 <b>BOT ANALYTICS</b>

📤 Uploads: {bot_stats.get('total_uploads', 0)}
📥 Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Runs: {bot_stats.get('total_runs', 0)}
👥 Users: {len(active_users)}
📁 Files: {sum(len(f) for f in user_files.values())}
🚀 Running: {len(bot_scripts)}
⏰ Schedules: {sum(1 for v in scheduled_tasks.values() if v['active'])}
💬 Feedback: {total_feedback}
💎 Premium Active: {len([u for u in user_subscriptions if user_subscriptions[u]['expiry'] > datetime.now()])}
🚫 Banned: {len(banned_users)}
👑 Admins: {len(admin_ids)}
🔒 Status: {'Locked' if bot_locked else 'Active'}
"""
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_system_status")
async def callback_admin_system_status(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    text = f"""
⚙️ <b>SYSTEM STATUS</b>

💻 CPU: {cpu}% {'🟢' if cpu < 70 else '🔴'}
🧠 RAM: {memory.percent}% ({memory.available/(1024**3):.1f}GB free)
💾 Disk: {disk.percent}% ({disk.free/(1024**3):.1f}GB free)
🤖 Bot: {'🔒 Locked' if bot_locked else '✅ Running'}
🚀 Scripts: {len(bot_scripts)}
"""
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_system_status")],
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ]), parse_mode="HTML")
    await callback.answer()

# NEW: Upload Notifications toggle
@dp.callback_query(F.data == "admin_upload_notifications")
async def callback_admin_upload_notifications(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    await callback.answer("✅ Upload notifications are ON — you get notified on every upload!", show_alert=True)

# NEW: Premium expiry alerts
@dp.callback_query(F.data == "admin_expiry_alerts")
async def callback_admin_expiry_alerts(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    expiring_soon = []
    for uid, data in user_subscriptions.items():
        days_left = (data['expiry'] - datetime.now()).days
        if 0 <= days_left <= 3:
            expiring_soon.append((uid, days_left))
    if not expiring_soon:
        text = "💰 <b>No premiums expiring in 3 days!</b>"
    else:
        text = f"💰 <b>EXPIRING SOON ({len(expiring_soon)})</b>\n\n"
        for uid, days in expiring_soon:
            text += f"⚠️ <code>{uid}</code> — {days}d left\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ]), parse_mode="HTML")
    await callback.answer()

# NEW: Scheduled Broadcast
@dp.callback_query(F.data == "admin_schedule_broadcast")
async def callback_admin_schedule_broadcast(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    pending_broadcast[callback.from_user.id] = {'waiting_text': True}
    await callback.message.edit_text(
        "📢 <b>Scheduled Broadcast</b>\n\nSend the broadcast message text:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_panel")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

async def send_scheduled_broadcast(text, run_at):
    wait = (run_at - datetime.now()).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    sent, failed = 0, 0
    for uid in active_users:
        if uid in banned_users:
            continue
        try:
            await bot.send_message(uid, f"📢 <b>Announcement:</b>\n\n{text}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    try:
        await bot.send_message(OWNER_ID, f"✅ <b>Scheduled Broadcast Done!</b>\nSent: {sent} | Failed: {failed}", parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "admin_add_admin")
async def callback_admin_add_admin(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    await callback.message.edit_text(
        "➕ <b>Add Admin</b>\n\nUse: <code>/addadmin USER_ID</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_remove_admin")
async def callback_admin_remove_admin(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    admins_list = "\n".join([f"👑 <code>{a}</code>" for a in admin_ids])
    await callback.message.edit_text(
        f"➖ <b>Remove Admin</b>\n\n{admins_list}\n\nUse: <code>/removeadmin USER_ID</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_ban_user")
async def callback_admin_ban_user(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    await callback.message.edit_text(
        f"🚫 <b>Ban User</b>\n\nBanned: {len(banned_users)}\n\nUse: <code>/ban USER_ID REASON</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_unban_user")
async def callback_admin_unban_user(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    banned_list = "\n".join([f"🚫 <code>{b}</code>" for b in list(banned_users)[:10]])
    await callback.message.edit_text(
        f"✅ <b>Unban User</b>\n\n{banned_list}\n\nUse: <code>/unban USER_ID</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "lock_bot")
async def callback_lock_bot(callback: types.CallbackQuery):
    global bot_locked
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    bot_locked = not bot_locked
    await callback.answer(f"Bot is now {'🔒 LOCKED' if bot_locked else '🔓 UNLOCKED'}!", show_alert=True)
    await callback_admin_panel(callback)

@dp.callback_query(F.data == "broadcast")
async def callback_broadcast(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    await callback.message.edit_text(
        f"📢 <b>Broadcast</b>\n\nRecipients: {len(active_users)}\n\nUse: <code>/broadcast Your message</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "add_premium")
async def callback_add_premium(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    await callback.message.edit_text(
        "💎 <b>Add Premium</b>\n\nUse: <code>/addpremium USER_ID DAYS</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_clean_files")
async def callback_admin_clean_files(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    await callback.message.edit_text(
        "🗑️ <b>Clean Files</b>\n\nUse: <code>/clean</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_backup_db")
async def callback_admin_backup_db(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    try:
        backup_path = IROTECH_DIR / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        conn = sqlite3.connect(DATABASE_PATH)
        backup_conn = sqlite3.connect(backup_path)
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()
        await callback.answer("✅ Backup created!", show_alert=True)
        await callback.message.answer_document(
            FSInputFile(backup_path),
            caption="💾 <b>DB Backup</b>", parse_mode="HTML"
        )
        backup_path.unlink()
    except Exception as e:
        await callback.answer(f"❌ Backup failed: {str(e)}", show_alert=True)

@dp.callback_query(F.data == "admin_view_logs")
async def callback_admin_view_logs(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    await callback.message.edit_text(
        "📝 <b>System Logs</b>\n\nUser script logs are in user folders.\nUse 📝 Log button on each file.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_restart_bot")
async def callback_admin_restart_bot(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Owner only!", show_alert=True)
        return
    await callback.message.edit_text(
        "🔄 <b>Restart Bot</b>\n\n⚠️ Use <code>/restart</code> to confirm.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ]), parse_mode="HTML"
    )
    await callback.answer()

# ============ COMMANDS ============

@dp.message(Command("addadmin"))
async def cmd_add_admin(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /addadmin USER_ID")
            return
        new_admin_id = int(args[1])
        if new_admin_id in admin_ids:
            await message.answer(f"✅ Already admin!")
            return
        admin_ids.add(new_admin_id)
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (new_admin_id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Admin added: <code>{new_admin_id}</code>", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")

@dp.message(Command("removeadmin"))
async def cmd_remove_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Owner only!")
        return
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /removeadmin USER_ID")
            return
        remove_id = int(args[1])
        if remove_id == OWNER_ID:
            await message.answer("❌ Cannot remove owner!")
            return
        if remove_id not in admin_ids:
            await message.answer("❌ Not an admin!")
            return
        admin_ids.remove(remove_id)
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM admins WHERE user_id = ?', (remove_id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Admin removed: <code>{remove_id}</code>", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")

@dp.message(Command("addpremium"))
async def cmd_add_premium(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    try:
        args = message.text.split()
        if len(args) != 3:
            await message.answer("Usage: /addpremium USER_ID DAYS")
            return
        target_id = int(args[1])
        days = int(args[2])
        if days <= 0:
            await message.answer("❌ Days must be > 0!")
            return
        expiry = datetime.now() + timedelta(days=days)
        user_subscriptions[target_id] = {'expiry': expiry}
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?, ?)',
                  (target_id, expiry.isoformat()))
        conn.commit()
        conn.close()
        await message.answer(
            f"✅ <b>Premium Added!</b>\nUser: <code>{target_id}</code>\nDays: {days}\nExpires: {expiry.strftime('%Y-%m-%d')}",
            parse_mode="HTML"
        )
        # Notify user
        try:
            await bot.send_message(
                target_id,
                f"🎉 <b>Premium Activated!</b>\n\n💎 Your account is now Premium!\n📅 Expires: {expiry.strftime('%Y-%m-%d')}\n\nEnjoy 50 file limit!",
                parse_mode="HTML"
            )
        except Exception:
            pass
    except ValueError:
        await message.answer("❌ Invalid input!")

@dp.message(Command("ban"))
async def cmd_ban_user(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    try:
        args = message.text.split(maxsplit=2)
        if len(args) < 2:
            await message.answer("Usage: /ban USER_ID [REASON]")
            return
        ban_id = int(args[1])
        reason = args[2] if len(args) > 2 else "No reason"
        if ban_id in admin_ids:
            await message.answer("❌ Cannot ban an admin!")
            return
        banned_users.add(ban_id)
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO banned_users VALUES (?, ?, ?)',
                  (ban_id, datetime.now().isoformat(), reason))
        conn.commit()
        conn.close()
        await message.answer(f"🚫 Banned: <code>{ban_id}</code>\nReason: {reason}", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")

@dp.message(Command("unban"))
async def cmd_unban_user(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /unban USER_ID")
            return
        unban_id = int(args[1])
        if unban_id not in banned_users:
            await message.answer("❌ Not banned!")
            return
        banned_users.remove(unban_id)
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM banned_users WHERE user_id = ?', (unban_id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Unbanned: <code>{unban_id}</code>", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    broadcast_text = message.text.replace("/broadcast", "", 1).strip()
    if not broadcast_text:
        await message.answer("Usage: /broadcast Your message")
        return
    sent, failed = 0, 0
    status_msg = await message.answer(f"📢 Broadcasting to {len(active_users)} users...")
    for uid in active_users:
        if uid in banned_users:
            continue
        try:
            await bot.send_message(uid, f"📢 <b>Announcement:</b>\n\n{broadcast_text}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await status_msg.edit_text(f"✅ <b>Broadcast Done!</b>\nSent: {sent} | Failed: {failed}", parse_mode="HTML")

@dp.message(Command("search"))
async def cmd_search_files(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /search filename")
        return
    term = args[1].lower()
    matches = [f for f in user_files.get(user_id, []) if term in f[0].lower()]
    if not matches:
        await message.answer(f"🔍 No files matching '<code>{term}</code>'", parse_mode="HTML")
        return
    text = f"🔍 <b>Results ({len(matches)}):</b>\n\n"
    for fn, ft in matches:
        icon = "🐍" if ft == "py" else "🟨" if ft == "js" else "📦"
        text += f"{icon} <code>{fn}</code>\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("""
ℹ️ <b>HELP</b>

/start /help /search /stats /premium

<b>New Features:</b>
⏰ Schedule auto-runs
🔄 Auto-restart on crash
📝 Download script logs
📦 Auto-install requirements.txt
📤 Share files with users
💬 Rate & review scripts
🚫 Rate limiting protection
""", parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    user_id = message.from_user.id
    files = len(user_files.get(user_id, []))
    favs = len(user_favorites.get(user_id, []))
    running = sum(1 for k in bot_scripts if k.startswith(f"{user_id}_"))
    text = f"""
📊 <b>YOUR STATS</b>

🆔 ID: <code>{user_id}</code>
📦 Files: {files}/{get_user_file_limit(user_id)}
⭐ Favorites: {favs}
🚀 Running: {running}
💎 Account: {'Premium ✨' if is_premium(user_id) else 'Free 🆓'}
📤 Total Uploads: {bot_stats.get('total_uploads', 0)}
▶️ Total Runs: {bot_stats.get('total_runs', 0)}
"""
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("restart"))
async def cmd_restart(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Owner only!")
        return
    await message.answer("🔄 Restarting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ============ PREMIUM EXPIRY BACKGROUND TASK ============

async def check_premium_expiry():
    """Notify users when premium is about to expire"""
    while True:
        await asyncio.sleep(3600)  # check every hour
        now = datetime.now()
        for uid, data in list(user_subscriptions.items()):
            days_left = (data['expiry'] - now).days
            if days_left == 1:  # 1 day left
                try:
                    await bot.send_message(
                        uid,
                        "⚠️ <b>Premium Expiring Tomorrow!</b>\n\n"
                        "💎 Your premium expires in 1 day.\n"
                        "Contact owner to renew!",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            elif days_left <= 0:
                # Expired — notify
                try:
                    await bot.send_message(
                        uid,
                        "😢 <b>Premium Expired!</b>\n\n"
                        "Your account is now Free 🆓\n"
                        "Contact owner to renew! 💎",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

# ============ WEB SERVER ============

async def web_server():
    app = web.Application()
    async def handle(request):
        return web.Response(text="🚀 Advanced File Host Bot - Online!")
    app.router.add_get('/', handle)
    app.router.add_get('/health', lambda r: web.json_response({"status": "ok", "running": len(bot_scripts)}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 5000)))
    await site.start()
    logger.info("🌐 Web server started")

# ============ MAIN ============

async def main():
    logger.info("🚀 Starting Advanced File Host Bot...")
    asyncio.create_task(web_server())
    asyncio.create_task(monitor_scripts())          # Auto-restart crashed scripts
    asyncio.create_task(run_scheduled_tasks())      # Scheduled auto-runs
    asyncio.create_task(check_premium_expiry())     # Premium expiry notifications
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
