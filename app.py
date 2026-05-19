#!/usr/bin/env python3
import os
import sys
import asyncio
import subprocess
import logging
import shutil
from datetime import datetime
from pathlib import Path
from threading import Thread
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

# ============ CONFIG ============
BOT_TOKEN = os.getenv('8625405642:AAFZqw9qe5dz4WAm59CTkvh6gFthr1s-0d8')
OWNER_ID = int(os.getenv('8263194061', 0))

if not BOT_TOKEN:
    print("❌ BOT_TOKEN not found!")
    sys.exit(1)

if not OWNER_ID:
    print("⚠️ OWNER_ID not set, but continuing...")

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
script_outputs = {}

# Flask app for API
flask_app = Flask(__name__)

# ============ KEYBOARDS ============

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Upload Script", callback_data="upload")],
        [InlineKeyboardButton(text="📁 My Scripts", callback_data="list")],
        [InlineKeyboardButton(text="🔄 Running Scripts", callback_data="running")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="stats")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="help")]
    ])

def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")]
    ])

# ============ TELEGRAM BOT HANDLERS ============

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    if OWNER_ID and user_id != OWNER_ID:
        await message.answer("❌ Unauthorized! Only owner can use this bot.")
        return
    
    await message.answer(
        "🚀 **Python Script Hosting Bot**\n\n"
        "Upload and run Python scripts on server!\n\n"
        "📌 **Features:**\n"
        "• Upload .py files\n"
        "• Run scripts in background\n"
        "• View real-time output\n"
        "• Stop running scripts\n"
        "• Delete scripts\n"
        "• Download script files\n\n"
        "Use buttons below to get started 👇",
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
        "📤 **Upload Python Script**\n\n"
        "Send me a `.py` file.\n\n"
        "**Limits:**\n"
        "• Max 50 scripts\n"
        "• Max 10 MB per file\n"
        "• Only .py files allowed\n\n"
        "Just send the file and I'll save it!",
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
            "📁 **No scripts found**\n\nUpload your first script using /upload or the button below!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Upload Script", callback_data="upload")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
        return
    
    scripts = list(user_scripts_dir.glob("*.py"))
    
    text = f"📁 **Your Scripts ({len(scripts)}/{50})**\n\n"
    buttons = []
    
    for script in sorted(scripts, key=lambda x: x.stat().st_mtime, reverse=True):
        script_name = script.name
        size_kb = script.stat().st_size / 1024
        
        # Check if running
        is_running = any(s['name'] == script_name and s['owner'] == user_id for s in running_scripts.values())
        status_icon = "🟢" if is_running else "⚪"
        
        text += f"{status_icon} `{script_name}` ({size_kb:.1f}KB)\n"
        buttons.append([
            InlineKeyboardButton(
                text=f"▶️ Run" if not is_running else f"🛑 Stop",
                callback_data=f"stop:{script_name}" if is_running else f"run:{script_name}"
            ),
            InlineKeyboardButton(text=f"📄 Output", callback_data=f"output:{script_name}"),
            InlineKeyboardButton(text=f"🗑️", callback_data=f"delete:{script_name}")
        ])
    
    # Add navigation buttons
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="list")])
    buttons.append([InlineKeyboardButton(text="➕ Upload New", callback_data="upload")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "running")
async def running_callback(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    
    # Get running scripts for this user
    user_running = {k: v for k, v in running_scripts.items() if v['owner'] == user_id}
    
    if not user_running:
        await callback.message.edit_text(
            "🔄 **No scripts running**\n\nAll scripts are stopped.",
            reply_markup=back_keyboard(),
            parse_mode="Markdown"
        )
        await callback.answer()
        return
    
    text = f"🔄 **Running Scripts ({len(user_running)})**\n\n"
    buttons = []
    
    for script_id, info in user_running.items():
        runtime = (datetime.now() - info['start_time']).total_seconds()
        text += f"📄 `{info['name']}`\n"
        text += f"   🆔 PID: {info['pid']} | ⏱️ Runtime: {int(runtime)}s\n\n"
        buttons.append([
            InlineKeyboardButton(
                text=f"🛑 Stop {info['name'][:20]}",
                callback_data=f"stop:{info['name']}"
            ),
            InlineKeyboardButton(
                text=f"📄 Output",
                callback_data=f"output:{info['name']}"
            )
        ])
    
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="running")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")])
    
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
    
    total_size = 0
    if user_scripts_dir.exists():
        for script in user_scripts_dir.glob("*.py"):
            total_size += script.stat().st_size
    
    text = f"""
📊 **Your Statistics**

📁 Total Scripts: {scripts_count}/50
🔄 Running: {running_count}/10
💾 Total Size: {total_size / (1024*1024):.2f} MB

📈 **Bot Stats:**
• Total Scripts (All Users): {sum(1 for _ in SCRIPTS_DIR.glob("*/*.py"))}
• Running Globally: {len(running_scripts)}

🎯 **Limits:**
• Max 50 scripts per user
• Max 10 concurrent scripts
• Script timeout: 1 hour
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=back_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    help_text = """
ℹ️ **Help Guide**

**Commands:**
/start - Start the bot
/upload - Upload script
/list - List scripts
/help - This help

**How to use:**

1️⃣ **Upload Script:**
   • Click 'Upload Script'
   • Send .py file
   • Max 10MB

2️⃣ **Run Script:**
   • Go to 'My Scripts'
   • Click 'Run' on any script
   • Script runs in background

3️⃣ **View Output:**
   • Click 'Output' on running script
   • See real-time logs

4️⃣ **Stop Script:**
   • Click 'Stop' on running script
   • Script will be terminated

**Script Requirements:**
• Must be valid Python code
• Can use print() for output
• Use sys.stdout for logging

**Example Script:**
```python
import time
for i in range(10):
    print(f"Count: {i}")
    time.sleep(1)
print("Done!")
