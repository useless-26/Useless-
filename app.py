#!/usr/bin/env python3
import os
import sys
import asyncio
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from threading import Thread
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

# ============ CONFIG ============
BOT_TOKEN = os.getenv('8625405642:AAFZqw9qe5dz4WAm59CTkvh6gFthr1s-0d8')
OWNER_ID = int(os.getenv('8263194061', 0))

if not BOT_TOKEN:
    print("❌ BOT_TOKEN not found!")
    sys.exit(1)

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SCRIPTS_DIR = BASE_DIR / "scripts"
SCRIPTS_DIR.mkdir(exist_ok=True)

# Bot initialization
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Store running scripts
running_scripts = {}

# Flask app for health checks
flask_app = Flask(__name__)

# ============ KEYBOARDS ============

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Upload Script", callback_data="upload")],
        [InlineKeyboardButton(text="📁 My Scripts", callback_data="list")],
        [InlineKeyboardButton(text="🔄 Running", callback_data="running")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="stats")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="help")]
    ])

def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")]
    ])

# ============ BOT HANDLERS ============

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        await message.answer("❌ Unauthorized!")
        return
    
    await message.answer(
        "🚀 **Python Script Hosting Bot**\n\n"
        "Upload and run Python scripts on server!\n\n"
        "Send me a `.py` file to get started.",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🏠 **Main Menu**\n\nChoose an option:",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "upload")
async def upload_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📤 **Send me a `.py` file**\n\n"
        "Limits:\n"
        "- Max 50 scripts\n"
        "- Max 10 MB per file",
        reply_markup=back_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "list")
async def list_callback(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    user_scripts_dir = SCRIPTS_DIR / user_id
    
    if not user_scripts_dir.exists() or not list(user_scripts_dir.glob("*.py")):
        await callback.message.edit_text(
            "📁 **No scripts found**\n\nUpload your first script!",
            reply_markup=main_keyboard(),
            parse_mode="Markdown"
        )
        await callback.answer()
        return
    
    scripts = list(user_scripts_dir.glob("*.py"))
    
    text = f"📁 **Your Scripts ({len(scripts)}/50)**\n\n"
    buttons = []
    
    for script in scripts:
        script_name = script.name
        is_running = any(s['name'] == script_name and s['owner'] == user_id for s in running_scripts.values())
        status = "🟢" if is_running else "⚪"
        text += f"{status} `{script_name}`\n"
        buttons.append([
            InlineKeyboardButton(
                text="🛑 Stop" if is_running else "▶️ Run",
                callback_data=f"stop:{script_name}" if is_running else f"run:{script_name}"
            ),
            InlineKeyboardButton(text="📄 Out", callback_data=f"output:{script_name}"),
            InlineKeyboardButton(text="🗑️", callback_data=f"delete:{script_name}")
        ])
    
    buttons.append([InlineKeyboardButton(text="🏠 Menu", callback_data="menu")])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "running")
async def running_callback(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    user_running = {k: v for k, v in running_scripts.items() if v['owner'] == user_id}
    
    if not user_running:
        await callback.message.edit_text(
            "🔄 **No scripts running**",
            reply_markup=main_keyboard(),
            parse_mode="Markdown"
        )
        await callback.answer()
        return
    
    text = f"🔄 **Running ({len(user_running)})**\n\n"
    buttons = []
    
    for info in user_running.values():
        runtime = (datetime.now() - info['start_time']).total_seconds()
        text += f"📄 `{info['name']}` (PID: {info['pid']}, {int(runtime)}s)\n"
        buttons.append([
            InlineKeyboardButton(text=f"🛑 Stop", callback_data=f"stop:{info['name']}"),
            InlineKeyboardButton(text=f"📄 Out", callback_data=f"output:{info['name']}")
        ])
    
    buttons.append([InlineKeyboardButton(text="🏠 Menu", callback_data="menu")])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def stats_callback(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    user_scripts_dir = SCRIPTS_DIR / user_id
    scripts_count = len(list(user_scripts_dir.glob("*.py"))) if user_scripts_dir.exists() else 0
    running_count = sum(1 for s in running_scripts.values() if s['owner'] == user_id)
    
    await callback.message.edit_text(
        f"📊 **Your Stats**\n\n📁 Scripts: {scripts_count}/50\n🔄 Running: {running_count}/10",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    help_text = (
        "ℹ️ **Help Guide**\n\n"
        "**How to use:**\n"
        "1. Send .py file to upload\n"
        "2. Click Run to execute\n"
        "3. Click Out to see output\n"
        "4. Click Stop to terminate\n\n"
        "**Limits:**\n"
        "- Max 50 scripts per user\n"
        "- Max 10 concurrent scripts\n"
        "- Max 10 MB per file\n\n"
        "**Commands:**\n"
        "/start - Start the bot\n\n"
        "Made with ❤️"
    )
    
    await callback.message.edit_text(
        help_text,
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(F.document)
async def handle_upload(message: types.Message):
    user_id = str(message.from_user.id)
    
    if OWNER_ID and message.from_user.id != OWNER_ID:
        await message.answer("❌ Unauthorized!")
        return
    
    doc = message.document
    if not doc.file_name.endswith('.py'):
        await message.answer("❌ Only .py files are allowed!")
        return
    
    if doc.file_size > 10 * 1024 * 1024:
        await message.answer("❌ File too large! Max 10MB.")
        return
    
    user_dir = SCRIPTS_DIR / user_id
    user_dir.mkdir(exist_ok=True)
    
    if len(list(user_dir.glob("*.py"))) >= 50:
        await message.answer("❌ Max 50 scripts limit reached!")
        return
    
    file_path = user_dir / doc.file_name
    
    if file_path.exists():
        await message.answer(f"⚠️ `{doc.file_name}` already exists! Delete it first.", parse_mode="Markdown")
        return
    
    await message.answer(f"📤 Uploading `{doc.file_name}`...", parse_mode="Markdown")
    await bot.download(doc, destination=file_path)
    
    await message.answer(
        f"✅ **Uploaded!**\n\n📄 `{doc.file_name}`\n💾 {doc.file_size/1024:.1f} KB",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Run", callback_data=f"run:{doc.file_name}"),
             InlineKeyboardButton(text="📁 List", callback_data="list")]
        ]),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("run:"))
async def run_script(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    script_name = callback.data.split(":")[1]
    script_path = SCRIPTS_DIR / user_id / script_name
    
    if not script_path.exists():
        await callback.answer("❌ Script not found!", show_alert=True)
        return
    
    user_running = sum(1 for s in running_scripts.values() if s['owner'] == user_id)
    if user_running >= 10:
        await callback.answer("❌ Max 10 concurrent scripts!", show_alert=True)
        return
    
    script_id = f"{user_id}_{script_name}_{int(datetime.now().timestamp())}"
    output_path = SCRIPTS_DIR / user_id / f"{script_name}.out"
    
    process = subprocess.Popen(
        [sys.executable, str(script_path)],
        stdout=open(output_path, 'w'),
        stderr=subprocess.STDOUT,
        cwd=str(SCRIPTS_DIR / user_id)
    )
    
    running_scripts[script_id] = {
        'name': script_name,
        'pid': process.pid,
        'process': process,
        'owner': user_id,
        'output_file': output_path,
        'start_time': datetime.now()
    }
    
    await callback.answer(f"✅ Started! PID: {process.pid}", show_alert=True)
    await list_callback(callback)

@dp.callback_query(F.data.startswith("stop:"))
async def stop_script(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    script_name = callback.data.split(":")[1]
    
    for sid, info in list(running_scripts.items()):
        if info['name'] == script_name and info['owner'] == user_id:
            try:
                info['process'].terminate()
            except:
                pass
            del running_scripts[sid]
            await callback.answer("✅ Script stopped!", show_alert=True)
            await list_callback(callback)
            return
    
    await callback.answer("❌ Script not running!", show_alert=True)

@dp.callback_query(F.data.startswith("output:"))
async def view_output(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    script_name = callback.data.split(":")[1]
    output_path = SCRIPTS_DIR / user_id / f"{script_name}.out"
    
    if not output_path.exists():
        await callback.answer("No output yet! Run the script first.", show_alert=True)
        return
    
    output = output_path.read_text()
    if not output:
        output = "No output yet..."
    
    if len(output) > 2000:
        output = output[-2000:] + "\n\n... (truncated)"
    
    await callback.message.edit_text(
        f"📄 **Output: `{script_name}`**\n\n```\n{output}\n```",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"output:{script_name}"),
             InlineKeyboardButton(text="📁 List", callback_data="list")]
        ]),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("delete:"))
async def delete_script(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    script_name = callback.data.split(":")[1]
    
    # Check if running
    for info in running_scripts.values():
        if info['name'] == script_name and info['owner'] == user_id:
            await callback.answer("❌ Stop the script first!", show_alert=True)
            return
    
    script_path = SCRIPTS_DIR / user_id / script_name
    if script_path.exists():
        script_path.unlink()
    
    output_path = SCRIPTS_DIR / user_id / f"{script_name}.out"
    if output_path.exists():
        output_path.unlink()
    
    await callback.answer("✅ Script deleted!", show_alert=True)
    await list_callback(callback)

# ============ FLASK HEALTH CHECK ============

@flask_app.route('/')
def health():
    return jsonify({
        "status": "alive",
        "scripts_running": len(running_scripts),
        "timestamp": datetime.now().isoformat()
    })

@flask_app.route('/health')
def health_check():
    return jsonify({"status": "ok"})

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ============ MAIN ============

async def main():
    logger.info("🚀 Starting Python Script Hosting Bot...")
    
    # Start Flask in background thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Flask API started")
    
    # Start bot
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
