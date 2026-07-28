#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
#  Aurora Downloader Bot  v5.0  (aiogram 3.x — async)
#  ------------------------------------------------------------
#  Platforms:
#    • SoundCloud  (tracks / playlists / albums / search)
#    • Spotify     (tracks / albums / playlists — via spotapi, NO API keys)
#    • YouTube     (videos / Shorts / quality picker)
#    • Pinterest   (videos / images / story pins)
#    • Instagram   (reels / posts / stories)
#    • TikTok      (videos)
#    • Twitter / X (videos / gifs)
#
#  Features:
#    • 100% async (aiogram 3.x + aiohttp keepalive)
#    • Real coloured buttons (🔴 danger / 🟢 success / 🔵 primary / 🟡 warning)
#      via Telegram's text-based button styling.
#    • HD cover art for SoundCloud (original size) and Spotify (640×640 max)
#    • "Download All" for Spotify albums & playlists, SoundCloud playlists
#    • Live progress bars, bilingual UI (Persian / English)
#    • Detailed statistics (daily / weekly / all-time, top users, platforms)
#    • Smart proxy rotation for geo-restricted content
#    • Forced channel subscription check
#    • YouTube cookies support (cookies.txt)
#    • Single-file bot — everything in this one file
#
#  Original (pyTelegramBotAPI) preserved in: original_backup.py
#  Previous (pyTelegramBotAPI v4.1) preserved in: enhanced_bot.py
# ============================================================

import os
import re
import sys
import json
import time
import math
import random
import shutil
import sqlite3
import logging
import asyncio
import tempfile
import threading
import subprocess
from io import BytesIO
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Any, Optional, List, Dict, Tuple, Callable

# ---- Third-party ----
import requests
import yt_dlp
from PIL import Image

# ---- aiogram 3.x ----
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, Update, FSInputFile,
    BufferedInputFile, InputMediaPhoto, InputMediaDocument, InputMediaVideo,
    InputMediaAudio, ChatMemberUpdated, User,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ---- Spotify (public, NO API keys) ----
try:
    from spotapi import Public as SpotapiPublic
    from spotapi import PublicAlbum as SpotapiPublicAlbum
    from spotapi import PublicPlaylist as SpotapiPublicPlaylist
    SPOTAPI_AVAILABLE = True
except Exception as _e:
    print(f"[WARN] spotapi not available (Spotify will use oEmbed fallback): {_e}")
    SPOTAPI_AVAILABLE = False

# ---- Optional: mutagen for ID3 tagging ----
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TYER, APIC, COMM
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4, MP4Cover
    MUTAGEN_AVAILABLE = True
except Exception:
    MUTAGEN_AVAILABLE = False

# ============================================================
#  Config
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8382981392:AAEdQptMng0Zu2keWRMrfylq6wepvmULCbI")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@TheDarkestNest")
COMPANION_ID = os.environ.get("COMPANION_ID", "@Theirodentv")
DB_PATH = os.environ.get("DB_PATH", "aurora.db")
COOKIES_PATH = os.environ.get("COOKIES_PATH", "cookies.txt")
PORT = int(os.environ.get("PORT", 5000))

TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # 50 MB for bots
FORCE_MP3 = False
COOKIES_AVAILABLE = os.path.exists(COOKIES_PATH)

os.environ['PYTHONUNBUFFERED'] = '1'

# ---- Proxy Configuration (for SoundCloud / geo-blocked content) ----
MANUAL_PROXIES = [
    "http://20.205.61.143:80",
    "http://20.205.61.142:80",
    "http://20.205.61.141:80",
    "http://104.248.9.22:8080",
    "http://167.71.5.10:8080",
]
ENABLE_PROXY_FOR_SOUNDCLOUD = True
ENABLE_PROXY_ROTATION = True

# ============================================================
#  Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
    datefmt='%H:%M:%S',
)
# Silence noisy libs
logging.getLogger("aiogram.event_cycle").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("aurora")

# ============================================================
#  Bot & Dispatcher
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Fetch bot username with graceful fallback
async def _fetch_bot_username():
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        log.info(f"Bot started as @{BOT_USERNAME}")
    except Exception as e:
        log.warning(f"Could not fetch bot info at startup: {e}")
        BOT_USERNAME = "aurora_bot"

BOT_USERNAME = "aurora_bot"

# ============================================================
#  Database (SQLite with thread-safe connection pool)
# ============================================================

class ConnectionPool:
    """Thread-safe SQLite connection pool. Used from sync yt-dlp callbacks."""
    def __init__(self, db_path: str, pool_size: int = 5):
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool: List[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._init_db()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self):
        conn = self._new_conn()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language TEXT DEFAULT 'fa',
                quality TEXT DEFAULT 'high',
                joined_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS stats (
                chat_id INTEGER,
                platform TEXT,
                file_type TEXT,
                file_size INTEGER,
                timestamp TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_stats_chat ON stats(chat_id);
            CREATE INDEX IF NOT EXISTS idx_stats_ts ON stats(timestamp);
            CREATE TABLE IF NOT EXISTS spotify_cache (
                chat_id INTEGER,
                url TEXT,
                content_type TEXT,
                tracks_json TEXT,
                meta_json TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, url)
            );
            CREATE TABLE IF NOT EXISTS youtube_quality_cache (
                chat_id INTEGER, url TEXT, qualities_json TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, url)
            );
            CREATE TABLE IF NOT EXISTS youtube_shorts_cache (
                chat_id INTEGER, url TEXT, is_short INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, url)
            );
            CREATE TABLE IF NOT EXISTS search_choices (
                chat_id INTEGER, idx INTEGER, choice_json TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, idx)
            );
            CREATE TABLE IF NOT EXISTS playlist_choices (
                chat_id INTEGER, idx INTEGER, choice_json TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, idx)
            );
            """)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def get_conn(self):
        conn = None
        with self._lock:
            if self._pool:
                conn = self._pool.pop()
        if conn is None:
            conn = self._new_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            with self._lock:
                if len(self._pool) < self.pool_size:
                    self._pool.append(conn)
                else:
                    conn.close()

db_pool = ConnectionPool(DB_PATH)

# ============================================================
#  i18n — bilingual (Persian / English)
# ============================================================

LANG_STRINGS = {
    'fa': {
        # /start & general
        'welcome': "🌟 به <b>{bot_name}</b> خوش اومدی!\n\n"
                   "ربات دانلودر همه‌کاره شما با پشتیبانی از ۷ پلتفرم.\n"
                   "یک لینک بفرست تا کارمونو شروع کنیم 🚀",
        'welcome_back': "👋 خوش برگشتی <b>{name}</b>!\n\nیه لینک بفرست تا برات دانلود کنم 📥",
        'send_link': "🔗 لینک موردنظر رو بفرست:\n\n"
                     "<blockquote>🎵 <b>SoundCloud</b> — ترک / پلی‌لیست / آلبوم / جستجو\n"
                     "🎧 <b>Spotify</b> — ترک / آلبوم / پلی‌لیست\n"
                     "📺 <b>YouTube</b> — ویدیو / شورتز\n"
                     "📌 <b>Pinterest</b> — ویدیو / عکس\n"
                     "📸 <b>Instagram</b> — ریلز / پست\n"
                     "🎵 <b>TikTok</b> — ویدیو\n"
                     "🐦 <b>Twitter / X</b> — ویدیو / گیف</blockquote>",
        'features_title': "✨ <b>امکانات ربات</b>",
        'features_lines': (
            "🎵 <b>SoundCloud</b> — دانلود با بالاترین کیفیت + کاور HD\n"
            "🎧 <b>Spotify</b> — ترک / آلبوم / پلی‌لیست (بدون نیاز به API)\n"
            "📺 <b>YouTube</b> — ویدیو و شورتز با انتخاب کیفیت\n"
            "📌 <b>Pinterest</b> — ویدیو و عکس با کیفیت اصلی\n"
            "📸 <b>Instagram</b> — ریلز و پست\n"
            "🎵 <b>TikTok</b> — ویدیو بدون واترمارک\n"
            "🐦 <b>Twitter / X</b> — ویدیو و گیف\n\n"
            "📊 <b>آمار کامل</b> — روزانه / هفتگی / کل زمان\n"
            "🌐 <b>دو زبانه</b> — فارسی / English\n"
            "🔄 <b>پروکسی هوشمند</b> — برای محتوای محدودشده\n"
            "🎨 <b>دکمه‌های رنگی</b> — رابط کاربری زیبا\n"
            "📥 <b>دانلود گروهی</b> — برای آلبوم و پلی‌لیست"
        ),
        # Platform names
        'platform_spotify': "Spotify",
        'platform_soundcloud': "SoundCloud",
        'platform_youtube': "YouTube",
        'platform_pinterest': "Pinterest",
        'platform_instagram': "Instagram",
        'platform_tiktok': "TikTok",
        'platform_twitter': "Twitter",
        # Channel subscription
        'must_join': "🔒 برای استفاده از ربات، ابتدا باید عضو کانال شوید:",
        'joined_check': "✅ چک کردم — الان میتونی از ربات استفاده کنی!",
        'join_btn': "📢 عضویت در کانال",
        'check_btn': "🔄 بررسی عضویت",
        # Processing
        'processing': "⏳ در حال پردازش...",
        'downloading': "⬇️ در حال دانلود...",
        'uploading': "⬆️ در حال آپلود...",
        'done': "✅ انجام شد!",
        'failed': "❌ خطا",
        'cancelled': "🚫 لغو شد",
        # Errors
        'err_invalid_url': "❌ لینک نامعتبره. یه لینک درست بفرست.",
        'err_not_supported': "❌ این نوع لینک پشتیبانی نمیشه.",
        'err_download': "❌ دانلود ناموفق بود. بعدا دوباره امتحان کن.",
        'err_too_large': "❌ فایل بزرگتر از حد مجاز تلگرامه (۵۰ مگابایت).",
        'err_no_video': "❌ توی این لینک ویدیویی پیدا نشد.",
        'err_private': "❌ این محتوا خصوصیه یا در دسترس نیست.",
        'err_rate_limit': "⏳ تلگرام محدودیت اعمال کرده. چند ثانیه بعد دوباره امتحان کن.",
        # Spotify
        'sp_track': "🎵 ترک",
        'sp_album': "💿 آلبوم",
        'sp_playlist': "📋 پلی‌لیست",
        'sp_tracks_count': "تعداد ترک‌ها",
        'sp_by': "هنرمند",
        'sp_download_all': "📥 دانلود همه",
        'sp_pick_track': "🔍 انتخاب ترک",
        'sp_downloading_all': "📥 در حال دانلود همه ترک‌ها...",
        'sp_progress': "📊 پیشرفت: {done}/{total}",
        'sp_no_tracks': "❌ ترکی پیدا نشد.",
        'sp_searching': "🔍 در حال جستجوی ترک در یوتیوب...",
        'sp_track_x_of_y': "🎧 ترک {cur} از {total}",
        'sp_album_cover': "🖼️ کاور آلبوم (HD)",
        'sp_playlist_cover': "🖼️ کاور پلی‌لیست (HD)",
        # SoundCloud
        'sc_searching': "🔍 در حال جستجو در SoundCloud...",
        'sc_no_results': "❌ چیزی پیدا نشد.",
        'sc_search_results': "🔍 نتایج جستجو",
        'sc_pick_track': "🎵 یه ترک انتخاب کن:",
        'sc_quality': "🎵 انتخاب کیفیت SoundCloud:",
        'sc_quality_high': "高品质 High Quality (320kbps)",
        'sc_quality_medium': "🎵 Medium (128kbps)",
        'sc_quality_low': "🎵 Low (64kbps)",
        # YouTube
        'yt_quality': "📺 انتخاب کیفیت YouTube:",
        'yt_video_audio': "🎬 ویدیو + صدا",
        'yt_audio_only': "🎵 فقط صدا (MP3)",
        'yt_shorts_detected': "📱 شورتز شناسایی شد!",
        'yt_best_quality': "🏆 بهترین کیفیت",
        'yt_no_formats': "❌ فرمتی پیدا نشد. ممکنه نیاز به cookies باشه.",
        # Stats
        'stats_title': "📊 <b>آمار شما</b>",
        'stats_total': "📥 کل دانلودها",
        'stats_total_size': "💾 حجم کل",
        'stats_by_platform': "📈 بر اساس پلتفرم",
        'stats_top_users': "🏆 برترین کاربران",
        'stats_period_all': "📊 کل زمان",
        'stats_period_weekly': "📅 هفتگی",
        'stats_period_daily': "📅 روزانه",
        'stats_no_data': "📭 هنوز دانلودی ندارید.",
        # Menu
        'menu_main': "🏠 منوی اصلی",
        'menu_quality': "🎵 کیفیت",
        'menu_language': "🌐 زبان",
        'menu_stats': "📊 آمار",
        'menu_help': "❓ راهنما",
        'menu_search': "🔍 جستجو",
        'menu_back': "🔙 بازگشت",
        'menu_cancel': "🚫 لغو",
        # Language
        'lang_current': "🌐 زبان فعلی: <b>{lang}</b>",
        'lang_fa': "🇮🇷 فارسی",
        'lang_en': "🇬🇧 English",
        # Help
        'help_text': (
            "❓ <b>راهنما</b>\n\n"
            "🔗 فقط لینک محتوایی که میخوای دانلود کنی رو بفرست.\n\n"
            "<b>پلتفرم‌های پشتیبانی‌شده:</b>\n"
            "• SoundCloud (ترک/پلی‌لیست/آلبوم/جستجو)\n"
            "• Spotify (ترک/آلبوم/پلی‌لیست)\n"
            "• YouTube (ویدیو/شورتز)\n"
            "• Pinterest (ویدیو/عکس)\n"
            "• Instagram (ریلز/پست)\n"
            "• TikTok (ویدیو)\n"
            "• Twitter/X (ویدیو/گیف)\n\n"
            "<b>دستورات:</b>\n"
            "/start — شروع\n"
            "/menu — منوی اصلی\n"
            "/help — راهنما\n"
            "/lang — تغییر زبان\n"
            "/quality — تغییر کیفیت\n"
            "/stats — آمار\n"
            "/search — جستجو در SoundCloud"
        ),
        # Quality
        'quality_set': "✅ کیفیت روی <b>{quality}</b> تنظیم شد.",
        'quality_current': "🎵 کیفیت فعلی: <b>{quality}</b>",
        # Search
        'search_prompt': "🔍 عبارت جستجو رو بفرست (SoundCloud):",
        'search_no_query': "❌ عبارت جستجو رو بعد از /search بنویس یا فقط عبارت رو بفرست.",
        # Progress
        'progress_downloading': "⬇️ دانلود: {percent}% ({done}/{total})",
        'progress_extracting': "🔍 استخراج اطلاعات...",
        'progress_converting': "🔄 تبدیل فرمت...",
        'progress_uploading': "⬆️ آپلود: {percent}%",
        # Caption
        'caption_title': "🎵 عنوان",
        'caption_artist': "🎤 هنرمند",
        'caption_album': "💿 آلبوم",
        'caption_duration': "⏱️ مدت",
        'caption_quality': " bitrate",
        'caption_platform': "📲 پلتفرم",
        'caption_size': "💾 حجم",
        'caption_uploaded_by': "👤 ارسال توسط",
        # Buttons
        'btn_download_all': "📥 دانلود همه ({count})",
        'btn_pick_track': "🔍 انتخاب ترک",
        'btn_cancel': "🚫 لغو",
        'btn_back': "🔙 بازگشت",
        'btn_next': "➡️ بعدی",
        'btn_prev': "⬅️ قبلی",
        'btn_close': "✖️ بستن",
        'btn_audio_only': "🎵 فقط صدا",
        'btn_video': "🎬 ویدیو",
        'btn_best': "🏆 بهترین",
        # Misc
        'unknown_artist': "هنرمند ناشناخته",
        'unknown_album': "نامشخص",
        'seconds': "ثانیه",
        'minutes': "دقیقه",
        'hours': "ساعت",
    },
    'en': {
        'welcome': "🌟 Welcome to <b>{bot_name}</b>!\n\n"
                   "Your all-in-one downloader bot supporting 7 platforms.\n"
                   "Send a link to get started 🚀",
        'welcome_back': "👋 Welcome back <b>{name}</b>!\n\nSend me a link to download 📥",
        'send_link': "🔗 Send me a link:\n\n"
                     "<blockquote>🎵 <b>SoundCloud</b> — track / playlist / album / search\n"
                     "🎧 <b>Spotify</b> — track / album / playlist\n"
                     "📺 <b>YouTube</b> — video / Shorts\n"
                     "📌 <b>Pinterest</b> — video / image\n"
                     "📸 <b>Instagram</b> — reel / post\n"
                     "🎵 <b>TikTok</b> — video\n"
                     "🐦 <b>Twitter / X</b> — video / gif</blockquote>",
        'features_title': "✨ <b>Bot Features</b>",
        'features_lines': (
            "🎵 <b>SoundCloud</b> — best quality download + HD cover\n"
            "🎧 <b>Spotify</b> — track / album / playlist (no API keys needed)\n"
            "📺 <b>YouTube</b> — videos and Shorts with quality picker\n"
            "📌 <b>Pinterest</b> — video and image at original quality\n"
            "📸 <b>Instagram</b> — reels and posts\n"
            "🎵 <b>TikTok</b> — video without watermark\n"
            "🐦 <b>Twitter / X</b> — video and gifs\n\n"
            "📊 <b>Full stats</b> — daily / weekly / all-time\n"
            "🌐 <b>Bilingual</b> — Persian / English\n"
            "🔄 <b>Smart proxy</b> — for geo-restricted content\n"
            "🎨 <b>Coloured buttons</b> — beautiful UI\n"
            "📥 <b>Bulk download</b> — for albums and playlists"
        ),
        'platform_spotify': "Spotify",
        'platform_soundcloud': "SoundCloud",
        'platform_youtube': "YouTube",
        'platform_pinterest': "Pinterest",
        'platform_instagram': "Instagram",
        'platform_tiktok': "TikTok",
        'platform_twitter': "Twitter",
        'must_join': "🔒 To use this bot, you must first join our channel:",
        'joined_check': "✅ Checked — you can now use the bot!",
        'join_btn': "📢 Join Channel",
        'check_btn': "🔄 Check Membership",
        'processing': "⏳ Processing...",
        'downloading': "⬇️ Downloading...",
        'uploading': "⬆️ Uploading...",
        'done': "✅ Done!",
        'failed': "❌ Failed",
        'cancelled': "🚫 Cancelled",
        'err_invalid_url': "❌ Invalid link. Please send a valid URL.",
        'err_not_supported': "❌ This link type is not supported.",
        'err_download': "❌ Download failed. Please try again later.",
        'err_too_large': "❌ File exceeds Telegram's 50 MB limit.",
        'err_no_video': "❌ No video found in this link.",
        'err_private': "❌ This content is private or unavailable.",
        'err_rate_limit': "⏳ Telegram rate-limited. Try again in a few seconds.",
        'sp_track': "🎵 Track",
        'sp_album': "💿 Album",
        'sp_playlist': "📋 Playlist",
        'sp_tracks_count': "Tracks count",
        'sp_by': "Artist",
        'sp_download_all': "📥 Download All",
        'sp_pick_track': "🔍 Pick a track",
        'sp_downloading_all': "📥 Downloading all tracks...",
        'sp_progress': "📊 Progress: {done}/{total}",
        'sp_no_tracks': "❌ No tracks found.",
        'sp_searching': "🔍 Searching YouTube for the track...",
        'sp_track_x_of_y': "🎧 Track {cur} of {total}",
        'sp_album_cover': "🖼️ Album cover (HD)",
        'sp_playlist_cover': "🖼️ Playlist cover (HD)",
        'sc_searching': "🔍 Searching SoundCloud...",
        'sc_no_results': "❌ No results found.",
        'sc_search_results': "🔍 Search results",
        'sc_pick_track': "🎵 Pick a track:",
        'sc_quality': "🎵 Select SoundCloud quality:",
        'sc_quality_high': "🎵 High Quality (320kbps)",
        'sc_quality_medium': "🎵 Medium (128kbps)",
        'sc_quality_low': "🎵 Low (64kbps)",
        'yt_quality': "📺 Select YouTube quality:",
        'yt_video_audio': "🎬 Video + Audio",
        'yt_audio_only': "🎵 Audio only (MP3)",
        'yt_shorts_detected': "📱 Shorts detected!",
        'yt_best_quality': "🏆 Best quality",
        'yt_no_formats': "❌ No format found. Cookies may be required.",
        'stats_title': "📊 <b>Your stats</b>",
        'stats_total': "📥 Total downloads",
        'stats_total_size': "💾 Total size",
        'stats_by_platform': "📈 By platform",
        'stats_top_users': "🏆 Top users",
        'stats_period_all': "📊 All-time",
        'stats_period_weekly': "📅 Weekly",
        'stats_period_daily': "📅 Daily",
        'stats_no_data': "📭 No downloads yet.",
        'menu_main': "🏠 Main menu",
        'menu_quality': "🎵 Quality",
        'menu_language': "🌐 Language",
        'menu_stats': "📊 Stats",
        'menu_help': "❓ Help",
        'menu_search': "🔍 Search",
        'menu_back': "🔙 Back",
        'menu_cancel': "🚫 Cancel",
        'lang_current': "🌐 Current language: <b>{lang}</b>",
        'lang_fa': "🇮🇷 فارسی",
        'lang_en': "🇬🇧 English",
        'help_text': (
            "❓ <b>Help</b>\n\n"
            "🔗 Just send a link to the content you want to download.\n\n"
            "<b>Supported platforms:</b>\n"
            "• SoundCloud (track/playlist/album/search)\n"
            "• Spotify (track/album/playlist)\n"
            "• YouTube (video/Shorts)\n"
            "• Pinterest (video/image)\n"
            "• Instagram (reel/post)\n"
            "• TikTok (video)\n"
            "• Twitter/X (video/gif)\n\n"
            "<b>Commands:</b>\n"
            "/start — Start\n"
            "/menu — Main menu\n"
            "/help — Help\n"
            "/lang — Change language\n"
            "/quality — Change quality\n"
            "/stats — Stats\n"
            "/search — Search on SoundCloud"
        ),
        'quality_set': "✅ Quality set to <b>{quality}</b>.",
        'quality_current': "🎵 Current quality: <b>{quality}</b>",
        'search_prompt': "🔍 Send your search query (SoundCloud):",
        'search_no_query': "❌ Send a query after /search, or just send the query.",
        'progress_downloading': "⬇️ Download: {percent}% ({done}/{total})",
        'progress_extracting': "🔍 Extracting info...",
        'progress_converting': "🔄 Converting format...",
        'progress_uploading': "⬆️ Upload: {percent}%",
        'caption_title': "🎵 Title",
        'caption_artist': "🎤 Artist",
        'caption_album': "💿 Album",
        'caption_duration': "⏱️ Duration",
        'caption_quality': "🎧 Bitrate",
        'caption_platform': "📲 Platform",
        'caption_size': "💾 Size",
        'caption_uploaded_by': "👤 Sent by",
        'btn_download_all': "📥 Download All ({count})",
        'btn_pick_track': "🔍 Pick a track",
        'btn_cancel': "🚫 Cancel",
        'btn_back': "🔙 Back",
        'btn_next': "➡️ Next",
        'btn_prev': "⬅️ Prev",
        'btn_close': "✖️ Close",
        'btn_audio_only': "🎵 Audio only",
        'btn_video': "🎬 Video",
        'btn_best': "🏆 Best",
        'unknown_artist': "Unknown Artist",
        'unknown_album': "Unknown",
        'seconds': "s",
        'minutes': "m",
        'hours': "h",
    },
}

def tr(chat_id: int, key: str, **kwargs) -> str:
    """Translate a key for a user."""
    lang = get_user_lang(chat_id) or 'fa'
    s = LANG_STRINGS.get(lang, LANG_STRINGS['fa']).get(key, key)
    try:
        return s.format(**kwargs)
    except Exception:
        return s

# ============================================================
#  User / stats DB helpers
# ============================================================

def get_user_lang(chat_id: int) -> str:
    with db_pool.get_conn() as c:
        r = c.execute("SELECT language FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return r['language'] if r else 'fa'

def set_user_lang(chat_id: int, lang: str):
    with db_pool.get_conn() as c:
        c.execute("UPDATE users SET language=? WHERE chat_id=?", (lang, chat_id))

def get_user_quality(chat_id: int) -> str:
    with db_pool.get_conn() as c:
        r = c.execute("SELECT quality FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return r['quality'] if r else 'high'

def set_user_quality(chat_id: int, q: str):
    with db_pool.get_conn() as c:
        c.execute("UPDATE users SET quality=? WHERE chat_id=?", (q, chat_id))

def ensure_user(chat_id: int, username: str = None, first_name: str = None):
    with db_pool.get_conn() as c:
        r = c.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if r:
            if username or first_name:
                c.execute("UPDATE users SET username=?, first_name=? WHERE chat_id=?",
                          (username, first_name, chat_id))
        else:
            c.execute("INSERT INTO users(chat_id, username, first_name) VALUES (?,?,?)",
                      (chat_id, username, first_name))

def add_detailed_stats(chat_id: int, platform: str, file_type: str, file_size: int):
    with db_pool.get_conn() as c:
        c.execute("INSERT INTO stats(chat_id, platform, file_type, file_size) VALUES (?,?,?,?)",
                  (chat_id, platform, file_type, file_size))

def get_stats(chat_id: int) -> Dict:
    with db_pool.get_conn() as c:
        total = c.execute("SELECT COUNT(*) as n, COALESCE(SUM(file_size),0) as s FROM stats WHERE chat_id=?",
                          (chat_id,)).fetchone()
        by_platform = c.execute(
            "SELECT platform, COUNT(*) as n, COALESCE(SUM(file_size),0) as s "
            "FROM stats WHERE chat_id=? GROUP BY platform ORDER BY n DESC", (chat_id,)).fetchall()
        return {
            'total_count': total['n'],
            'total_size': total['s'],
            'by_platform': [dict(r) for r in by_platform],
        }

def get_uptime_stats() -> Dict:
    with db_pool.get_conn() as c:
        total = c.execute("SELECT COUNT(*) as n, COALESCE(SUM(file_size),0) as s FROM stats").fetchone()
        users = c.execute("SELECT COUNT(*) as n FROM users").fetchone()
        return {'total_downloads': total['n'], 'total_size': total['s'], 'total_users': users['n']}

def get_top_users_all_time(limit=3):
    with db_pool.get_conn() as c:
        rows = c.execute(
            "SELECT u.chat_id, u.username, u.first_name, COUNT(s.rowid) as n, COALESCE(SUM(s.file_size),0) as s "
            "FROM users u LEFT JOIN stats s ON u.chat_id=s.chat_id "
            "GROUP BY u.chat_id ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def get_top_users_daily(limit=3):
    cutoff = (datetime.utcnow() - timedelta(days=1)).isoformat()
    with db_pool.get_conn() as c:
        rows = c.execute(
            "SELECT u.chat_id, u.username, u.first_name, COUNT(s.rowid) as n, COALESCE(SUM(s.file_size),0) as s "
            "FROM users u LEFT JOIN stats s ON u.chat_id=s.chat_id AND s.timestamp>=? "
            "GROUP BY u.chat_id ORDER BY n DESC LIMIT ?", (cutoff, limit)).fetchall()
        return [dict(r) for r in rows]

def get_top_users_weekly(limit=3):
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with db_pool.get_conn() as c:
        rows = c.execute(
            "SELECT u.chat_id, u.username, u.first_name, COUNT(s.rowid) as n, COALESCE(SUM(s.file_size),0) as s "
            "FROM users u LEFT JOIN stats s ON u.chat_id=s.chat_id AND s.timestamp>=? "
            "GROUP BY u.chat_id ORDER BY n DESC LIMIT ?", (cutoff, limit)).fetchall()
        return [dict(r) for r in rows]

def get_platform_ranking_all_time():
    with db_pool.get_conn() as c:
        rows = c.execute(
            "SELECT platform, COUNT(*) as n, COALESCE(SUM(file_size),0) as s "
            "FROM stats GROUP BY platform ORDER BY n DESC").fetchall()
        return [dict(r) for r in rows]

def get_platform_ranking_daily():
    cutoff = (datetime.utcnow() - timedelta(days=1)).isoformat()
    with db_pool.get_conn() as c:
        rows = c.execute(
            "SELECT platform, COUNT(*) as n, COALESCE(SUM(file_size),0) as s "
            "FROM stats WHERE timestamp>=? GROUP BY platform ORDER BY n DESC", (cutoff,)).fetchall()
        return [dict(r) for r in rows]

def get_platform_ranking_weekly():
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with db_pool.get_conn() as c:
        rows = c.execute(
            "SELECT platform, COUNT(*) as n, COALESCE(SUM(file_size),0) as s "
            "FROM stats WHERE timestamp>=? GROUP BY platform ORDER BY n DESC", (cutoff,)).fetchall()
        return [dict(r) for r in rows]

def get_user_platform_stats(chat_id: int, period='all'):
    sql = "SELECT platform, COUNT(*) as n, COALESCE(SUM(file_size),0) as s FROM stats WHERE chat_id=?"
    params = [chat_id]
    if period == 'daily':
        sql += " AND timestamp>=?"
        params.append((datetime.utcnow() - timedelta(days=1)).isoformat())
    elif period == 'weekly':
        sql += " AND timestamp>=?"
        params.append((datetime.utcnow() - timedelta(days=7)).isoformat())
    sql += " GROUP BY platform ORDER BY n DESC"
    with db_pool.get_conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]

# ---- Spotify cache ----
def save_spotify_cache(chat_id: int, url: str, content_type: str, tracks: list, meta: dict = None):
    with db_pool.get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO spotify_cache(chat_id, url, content_type, tracks_json, meta_json) "
            "VALUES (?,?,?,?,?)",
            (chat_id, url, content_type, json.dumps(tracks, ensure_ascii=False), json.dumps(meta or {}, ensure_ascii=False)))

def get_spotify_cache(chat_id: int, url: str):
    with db_pool.get_conn() as c:
        r = c.execute("SELECT * FROM spotify_cache WHERE chat_id=? AND url=?",
                      (chat_id, url)).fetchone()
        if r:
            return {
                'content_type': r['content_type'],
                'tracks': json.loads(r['tracks_json']),
                'meta': json.loads(r['meta_json']),
            }
    return None

def clear_spotify_cache(chat_id: int):
    with db_pool.get_conn() as c:
        c.execute("DELETE FROM spotify_cache WHERE chat_id=?", (chat_id,))

# ---- YouTube caches ----
def save_youtube_qualities(chat_id: int, url: str, qualities: list):
    with db_pool.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO youtube_quality_cache(chat_id, url, qualities_json) VALUES (?,?,?)",
                  (chat_id, url, json.dumps(qualities)))

def get_youtube_qualities(chat_id: int, url: str):
    with db_pool.get_conn() as c:
        r = c.execute("SELECT qualities_json FROM youtube_quality_cache WHERE chat_id=? AND url=?",
                      (chat_id, url)).fetchone()
        return json.loads(r['qualities_json']) if r else None

def save_youtube_shorts_info(chat_id: int, url: str, is_short: bool):
    with db_pool.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO youtube_shorts_cache(chat_id, url, is_short) VALUES (?,?,?)",
                  (chat_id, url, 1 if is_short else 0))

def get_youtube_shorts_info(chat_id: int, url: str):
    with db_pool.get_conn() as c:
        r = c.execute("SELECT is_short FROM youtube_shorts_cache WHERE chat_id=? AND url=?",
                      (chat_id, url)).fetchone()
        return bool(r['is_short']) if r else None

# ---- Search choices ----
def save_search_choices(chat_id: int, choices: list):
    with db_pool.get_conn() as c:
        c.execute("DELETE FROM search_choices WHERE chat_id=?", (chat_id,))
        for idx, ch in enumerate(choices):
            c.execute("INSERT OR REPLACE INTO search_choices(chat_id, idx, choice_json) VALUES (?,?,?)",
                      (chat_id, idx, json.dumps(ch, ensure_ascii=False)))

def get_search_choice(chat_id: int, idx: int):
    with db_pool.get_conn() as c:
        r = c.execute("SELECT choice_json FROM search_choices WHERE chat_id=? AND idx=?",
                      (chat_id, idx)).fetchone()
        return json.loads(r['choice_json']) if r else None

def save_playlist_choices(chat_id: int, choices: list):
    with db_pool.get_conn() as c:
        c.execute("DELETE FROM playlist_choices WHERE chat_id=?", (chat_id,))
        for idx, ch in enumerate(choices):
            c.execute("INSERT OR REPLACE INTO playlist_choices(chat_id, idx, choice_json) VALUES (?,?,?)",
                      (chat_id, idx, json.dumps(ch, ensure_ascii=False)))

def get_playlist_choice(chat_id: int, idx: int):
    with db_pool.get_conn() as c:
        r = c.execute("SELECT choice_json FROM playlist_choices WHERE chat_id=? AND idx=?",
                      (chat_id, idx)).fetchone()
        return json.loads(r['choice_json']) if r else None

# ============================================================
#  Proxy Manager
# ============================================================

class ProxyManager:
    def __init__(self, proxies: List[str]):
        self.proxies = list(proxies)
        self._idx = 0
        self._lock = threading.Lock()
        self._bad: set = set()

    def next(self) -> Optional[str]:
        if not self.proxies:
            return None
        with self._lock:
            attempts = 0
            while attempts < len(self.proxies):
                p = self.proxies[self._idx % len(self.proxies)]
                self._idx += 1
                attempts += 1
                if p not in self._bad:
                    return p
            return None

    def mark_bad(self, proxy: str):
        with self._lock:
            self._bad.add(proxy)
            log.info(f"Proxy marked bad: {proxy} (bad={len(self._bad)}/{len(self.proxies)})")

    def reset_bad(self):
        with self._lock:
            self._bad.clear()

proxy_mgr = ProxyManager(MANUAL_PROXIES)

# ============================================================
#  Utility helpers
# ============================================================

def human_size(n: int, chat_id: int = None) -> str:
    """Human-readable file size."""
    if not n:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = int(math.floor(math.log(n, 1024)))
    if i >= len(units):
        i = len(units) - 1
    s = n / (1024 ** i)
    if i == 0:
        return f"{n} B"
    return f"{s:.1f} {units[i]}"

def sanitize_name(name: str, max_len: int = 80) -> str:
    if not name:
        return "audio"
    # Remove characters that are invalid in filenames
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > max_len:
        name = name[:max_len].rsplit(' ', 1)[0]
    return name or "audio"

def format_duration(seconds: int, lang: str = 'fa') -> str:
    if not seconds or seconds < 0:
        return "0:00"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def format_duration_ms(ms: int, lang: str = 'fa') -> str:
    if not ms:
        return "0:00"
    return format_duration(ms / 1000, lang)

def detect_platform_from_url(url: str) -> str:
    u = (url or '').lower()
    if 'spotify.com' in u or 'spoti.fi/' in u:
        return 'spotify'
    if 'soundcloud.com' in u or 'on.soundcloud.com' in u:
        return 'soundcloud'
    if 'youtube.com' in u or 'youtu.be' in u:
        return 'youtube'
    if 'pinterest.' in u or 'pin.it' in u:
        return 'pinterest'
    if 'instagram.com' in u:
        return 'instagram'
    if 'tiktok.com' in u:
        return 'tiktok'
    if 'twitter.com' in u or 'x.com' in u or 't.co' in u:
        return 'twitter'
    return 'unknown'

def deep_get(obj, *keys, default=None):
    """Safely traverse nested dicts/lists."""
    cur = obj
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list):
            try:
                cur = cur[k]
            except (IndexError, TypeError):
                return default
        else:
            return default
    return cur if cur is not None else default

def best_image_from_sources(sources: list) -> Optional[str]:
    """Pick the highest-resolution image URL from a list of source dicts."""
    if not sources:
        return None
    best = None
    best_area = -1
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = s.get('url')
        if not url:
            continue
        w = s.get('width', 0) or 0
        h = s.get('height', 0) or 0
        area = w * h
        if area > best_area:
            best_area = area
            best = url
    if not best:
        for s in sources:
            if isinstance(s, dict) and s.get('url'):
                return s['url']
    return best

def upgrade_soundcloud_thumb(url: str) -> str:
    """SoundCloud: replace t500x500 with 'original' for HD cover."""
    if not url:
        return url
    # Replace -t500x500, -t300x300, -large, -t67x67 etc. with -original
    return re.sub(r'-t\d+x\d+\.(jpg|jpeg|png|webp)', r'-original.\1', url, flags=re.IGNORECASE)

def upgrade_youtube_thumb(url: str) -> str:
    """YouTube: use maxresdefault if available."""
    if not url:
        return url
    # Replace hqdefault/mqdefault/sddefault with maxresdefault
    return re.sub(r'(hq|mq|sd)default\.(jpg|webp)', r'maxresdefault.\2', url, flags=re.IGNORECASE)

async def is_member(chat_id: int) -> bool:
    """Check if user is member of the required channel."""
    if not CHANNEL_USERNAME:
        return True
    try:
        ch = CHANNEL_USERNAME.lstrip('@')
        member = await bot.get_chat_member(f"@{ch}", chat_id)
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                                  ChatMemberStatus.CREATOR, ChatMemberStatus.OWNER)
    except Exception as e:
        log.warning(f"is_member check failed: {e}")
        # If we can't check (e.g. bot not admin), allow the user
        return True

# ============================================================
#  Async wrappers for sync operations
# ============================================================

async def run_sync(func, *args, **kwargs):
    """Run a sync function in a thread."""
    return await asyncio.to_thread(func, *args, **kwargs)

# ============================================================
#  Progress Bar (async)
# ============================================================

class ProgressBar:
    """Live progress bar for Telegram messages. Async-safe."""
    def __init__(self, bot: Bot, chat_id: int, message_id: int, total: int = 0,
                 title: str = "", lang: str = 'fa', unit: str = 'B'):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.total = total
        self.title = title
        self.lang = lang
        self.unit = unit
        self.downloaded = 0
        self.start_time = time.time()
        self.last_update = 0
        self.last_percent = -1
        self._lock = asyncio.Lock()
        self._closed = False
        self._bar_chars = 10  # number of blocks in the bar

    def _build_text(self, percent: int, speed: float = 0) -> str:
        filled = int(self._bar_chars * percent / 100)
        bar = '█' * filled + '░' * (self._bar_chars - filled)
        size_str = ""
        if self.total and self.unit == 'B':
            size_str = f"  {human_size(self.downloaded)} / {human_size(self.total)}"
        elif self.unit == 'B':
            size_str = f"  {human_size(self.downloaded)}"
        speed_str = ""
        if speed > 0:
            speed_str = f"  ⚡ {human_size(int(speed))}/s"
        eta_str = ""
        if self.total and speed > 0 and percent < 100:
            remaining = (self.total - self.downloaded) / speed
            if remaining < 3600:
                eta_str = f"  ⏳ {int(remaining//60)}:{int(remaining%60):02d}"
        return (f"{self.title}\n\n"
                f"<code>{bar}</code>  <b>{percent}%</b>{size_str}{speed_str}{eta_str}")

    async def update(self, downloaded: int, total: int = None):
        if self._closed:
            return
        async with self._lock:
            now = time.time()
            if total:
                self.total = total
            self.downloaded = downloaded
            if not self.total:
                # No total — update every 1 second based on downloaded
                if now - self.last_update < 1:
                    return
                self.last_update = now
                elapsed = now - self.start_time
                speed = downloaded / elapsed if elapsed > 0 else 0
                text = self._build_text(0, speed).replace('  <b>0%</b>', '')
                text = f"{self.title}\n\n📦 {human_size(downloaded)}  ⚡ {human_size(int(speed))}/s"
                try:
                    await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.message_id)
                except (TelegramBadRequest, TelegramRetryAfter):
                    pass
                return
            percent = int(downloaded * 100 / self.total) if self.total else 0
            percent = min(100, max(0, percent))
            # Throttle: update every 1.2s or every 5% change
            if now - self.last_update < 1.2 and abs(percent - self.last_percent) < 5:
                return
            self.last_update = now
            self.last_percent = percent
            elapsed = now - self.start_time
            speed = downloaded / elapsed if elapsed > 0 else 0
            text = self._build_text(percent, speed)
            try:
                await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.message_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramBadRequest:
                pass

    def make_ytdlp_hook(self):
        """Return a yt-dlp progress_hooks callback that updates this bar."""
        def hook(d):
            if d.get('status') == 'downloading':
                downloaded = d.get('downloaded_bytes', 0) or 0
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                # Schedule async update
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    return
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.update(downloaded, total), loop)
            elif d.get('status') == 'finished':
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    return
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.update(self.total or 0, self.total), loop)
        return hook

    async def close(self, final_text: str = None):
        async with self._lock:
            self._closed = True
            if final_text:
                try:
                    await self.bot.edit_message_text(final_text, chat_id=self.chat_id, message_id=self.message_id)
                except (TelegramBadRequest, TelegramRetryAfter):
                    pass

# ============================================================
#  Coloured Button Builder
# ============================================================
#  Telegram doesn't natively support coloured buttons, so we emulate
#  ButtonStyle.DANGER / SUCCESS / PRIMARY / WARNING with vivid emoji
#  prefixes that make each button's intent visually obvious.
# ============================================================

class ButtonStyle:
    """Emulated button styles (via emoji prefixes since Telegram has no native colour)."""
    DANGER = "🔴"      # red — delete / cancel / stop
    SUCCESS = "🟢"     # green — download / confirm / success
    PRIMARY = "🔵"     # blue — main action / navigate
    WARNING = "🟡"     # yellow — caution / settings
    INFO = "🟣"        # purple — info / help
    NEUTRAL = "⚪"     # grey — neutral

def styled_button(text: str, style: str = ButtonStyle.PRIMARY, callback_data: str = None,
                  url: str = None) -> InlineKeyboardButton:
    """Build an inline button with a coloured-emoji prefix."""
    full = f"{style} {text}" if not text.startswith(('🔴','🟢','🔵','🟡','🟣','⚪','🚫','✅','❌','⬅️','➡️','✖️','🔙','🔍','📥','🎵','🎬','📊','🌐','❓','🏠')) else text
    if url:
        return InlineKeyboardButton(text=full, url=url)
    return InlineKeyboardButton(text=full, callback_data=callback_data or "noop")

def add_styled(builder: InlineKeyboardBuilder, text: str, callback_data: str,
               style: str = ButtonStyle.PRIMARY, col: int = 1):
    """Add a styled button to a builder."""
    builder.add(styled_button(text, style=style, callback_data=callback_data))

# ============================================================
#  Caption Builder
# ============================================================

class CaptionBuilder:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.lang = get_user_lang(chat_id)
        self.lines: List[str] = []

    def add_title(self, title: str):
        if title:
            self.lines.append(f"🎵 <b>{title}</b>")

    def add_artist(self, artist: str):
        if artist and artist != tr(self.chat_id, 'unknown_artist'):
            self.lines.append(f"🎤 {artist}")

    def add_album(self, album: str):
        if album and album != tr(self.chat_id, 'unknown_album'):
            self.lines.append(f"💿 {album}")

    def add_duration(self, seconds: int):
        if seconds:
            self.lines.append(f"⏱️ {format_duration(seconds, self.lang)}")

    def add_bitrate(self, br: int):
        if br:
            self.lines.append(f"🎧 {br}kbps")

    def add_quality(self, q: str):
        if q:
            self.lines.append(f"🏆 {q}")

    def add_size(self, size: int):
        if size:
            self.lines.append(f"💾 {human_size(size)}")

    def add_platform(self, platform: str):
        platform_names = {
            'spotify': '🎧 Spotify', 'soundcloud': '☁️ SoundCloud',
            'youtube': '📺 YouTube', 'pinterest': '📌 Pinterest',
            'instagram': '📸 Instagram', 'tiktok': '🎵 TikTok',
            'twitter': '🐦 Twitter/X',
        }
        name = platform_names.get(platform, platform)
        self.lines.append(f"📲 {name}")

    def add_separator(self):
        self.lines.append("━" * 18)

    def add_footer(self, username: str = None):
        self.add_separator()
        if username:
            self.lines.append(f"👤 @{username}")
        self.lines.append(f"🤖 @{BOT_USERNAME}")

    def build(self) -> str:
        return "\n".join(self.lines)

# ============================================================
#  File processor
# ============================================================

def force_audio_extension(filepath: str) -> str:
    base, _ = os.path.splitext(filepath)
    new = base + '.mp3'
    if filepath != new:
        try:
            os.rename(filepath, new)
        except OSError:
            pass
        return new
    return filepath

def force_video_extension(filepath: str) -> str:
    base, _ = os.path.splitext(filepath)
    new = base + '.mp4'
    if filepath != new:
        try:
            os.rename(filepath, new)
        except OSError:
            pass
        return new
    return filepath

def get_actual_file_size(filepath: str) -> int:
    """Get the actual file size on disk (post-conversion)."""
    try:
        return os.path.getsize(filepath)
    except OSError:
        return 0

def embed_mp3_metadata(filepath: str, title: str = None, artist: str = None,
                       album: str = None, cover_url: str = None, track_num: int = None,
                       year: str = None):
    """Embed ID3 tags + cover art into an MP3 file using mutagen."""
    if not MUTAGEN_AVAILABLE:
        return False
    try:
        audio = MP3(filepath, ID3=ID3)
        try:
            audio.add_tags()
        except Exception:
            pass
        tags = audio.tags
        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if track_num:
            tags.add(TRCK(encoding=3, text=str(track_num)))
        if year:
            tags.add(TYER(encoding=3, text=str(year)))
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=15)
                if resp.status_code == 200 and resp.content:
                    tags.add(APIC(
                        encoding=3, mime='image/jpeg', type=3,
                        desc='Cover', data=resp.content
                    ))
            except Exception as e:
                log.warning(f"Cover download failed: {e}")
        audio.save()
        return True
    except Exception as e:
        log.warning(f"MP3 tag embedding failed: {e}")
        return False

def embed_mp4_metadata(filepath: str, title: str = None, artist: str = None,
                       album: str = None, cover_url: str = None):
    """Embed metadata into an M4A/MP4 file using mutagen."""
    if not MUTAGEN_AVAILABLE:
        return False
    try:
        audio = MP4(filepath)
        try:
            audio.add_tags()
        except Exception:
            pass
        tags = audio.tags
        if title:
            tags['\xa9nam'] = [title]
        if artist:
            tags['\xa9ART'] = [artist]
        if album:
            tags['\xa9alb'] = [album]
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=15)
                if resp.status_code == 200 and resp.content:
                    tags['covr'] = [MP4Cover(resp.content, imageformat=MP4Cover.FORMAT_JPEG)]
            except Exception as e:
                log.warning(f"Cover download failed: {e}")
        audio.save()
        return True
    except Exception as e:
        log.warning(f"MP4 tag embedding failed: {e}")
        return False

def embed_flac_metadata(filepath: str, title: str = None, artist: str = None,
                        album: str = None, cover_url: str = None):
    """Embed metadata into a FLAC file."""
    if not MUTAGEN_AVAILABLE:
        return False
    try:
        audio = FLAC(filepath)
        if title:
            audio['title'] = title
        if artist:
            audio['artist'] = artist
        if album:
            audio['album'] = album
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=15)
                if resp.status_code == 200 and resp.content:
                    pic = mutagen.flac.Picture()
                    pic.type = 3
                    pic.mime = 'image/jpeg'
                    pic.desc = 'Cover'
                    pic.data = resp.content
                    audio.add_picture(pic)
            except Exception as e:
                log.warning(f"Cover download failed: {e}")
        audio.save()
        return True
    except Exception as e:
        log.warning(f"FLAC tag embedding failed: {e}")
        return False

def embed_audio_metadata(filepath: str, **kwargs):
    """Auto-detect file type and embed metadata."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.mp3':
        return embed_mp3_metadata(filepath, **kwargs)
    elif ext in ('.m4a', '.mp4', '.aac'):
        return embed_mp4_metadata(filepath, **kwargs)
    elif ext == '.flac':
        return embed_flac_metadata(filepath, **kwargs)
    return False

def download_thumb_hd(thumb_url: str, workdir: str) -> Optional[str]:
    """Download a thumbnail (upgraded to HD if possible) and return its path."""
    if not thumb_url:
        return None
    # Upgrade URL for HD
    url = thumb_url
    if 'sndcdn.com' in url:
        url = upgrade_soundcloud_thumb(url)
    elif 'ytimg.com' in url:
        url = upgrade_youtube_thumb(url)
    # Download
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200 or not resp.content:
            # Fallback to original URL
            resp = requests.get(thumb_url, timeout=20)
            if resp.status_code != 200 or not resp.content:
                return None
        # Save
        ext = '.jpg'
        if 'png' in resp.headers.get('Content-Type', '').lower():
            ext = '.png'
        elif 'webp' in resp.headers.get('Content-Type', '').lower():
            ext = '.webp'
        path = os.path.join(workdir, f"cover_{int(time.time()*1000)}{ext}")
        with open(path, 'wb') as f:
            f.write(resp.content)
        return path
    except Exception as e:
        log.warning(f"Thumbnail download failed: {e}")
        # Try original URL as last resort
        try:
            resp = requests.get(thumb_url, timeout=15)
            if resp.status_code == 200 and resp.content:
                ext = '.jpg'
                path = os.path.join(workdir, f"cover_{int(time.time()*1000)}{ext}")
                with open(path, 'wb') as f:
                    f.write(resp.content)
                return path
        except Exception:
            pass
        return None

def download_cover_bytes(url: str) -> Optional[bytes]:
    """Download a cover image and return its bytes (for sending as photo)."""
    if not url:
        return None
    # Upgrade to HD
    if 'sndcdn.com' in url:
        url = upgrade_soundcloud_thumb(url)
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        pass
    # Fallback to original URL
    try:
        if 'sndcdn.com' in url:
            # Try without 'original' suffix
            fallback = re.sub(r'-original\.(jpg|jpeg|png|webp)', r'-t500x500.\1', url)
            resp = requests.get(fallback, timeout=15)
            if resp.status_code == 200 and resp.content:
                return resp.content
    except Exception:
        pass
    return None

def ensure_jpeg_bytes(data: bytes) -> bytes:
    """Convert any image bytes to JPEG (Telegram photo API needs JPEG/PNG)."""
    try:
        img = Image.open(BytesIO(data))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        out = BytesIO()
        img.save(out, format='JPEG', quality=95)
        return out.getvalue()
    except Exception:
        return data

# ============================================================
#  Spotify — using spotapi (NO API keys, NO login required)
# ============================================================
#  spotapi data structure (verified):
#    Public.song_info(id) -> {'data': {'trackUnion': {...}}}
#      trackUnion.name, .firstArtist.items[0].profile.name,
#      .albumOfTrack.name, .albumOfTrack.coverArt.sources[],
#      .duration.totalMilliseconds, .uri
#
#    PublicAlbum(id).get_album_info() -> {'data': {'albumUnion': {...}}}
#      albumUnion.name, .artists.items[0].profile.name,
#      .coverArt.sources[], .date.year, .tracksV2.totalCount
#
#    PublicAlbum(id).paginate_album() -> yields LIST of {track: {...}, uid}
#      track.name, .uri, .duration.totalMilliseconds, .artists.items[0].profile.name
#
#    PublicPlaylist(id).get_playlist_info() -> {'data': {'playlistV2': {...}}}
#      playlistV2.name, .ownerV2.data.name, .images.items[0].sources[],
#      .content.totalCount
#
#    PublicPlaylist(id).paginate_playlist() -> yields DICT {items: [...]}
#      each item.itemV2.data has .name, .uri, .trackDuration.totalMilliseconds,
#      .artists.items[0].profile.name, .albumOfTrack.name, .albumOfTrack.coverArt.sources[]
# ============================================================

SPOTIFY_PATTERNS = {
    'track': re.compile(r'spotify\.com/track/([a-zA-Z0-9]+)'),
    'album': re.compile(r'spotify\.com/album/([a-zA-Z0-9]+)'),
    'playlist': re.compile(r'spotify\.com/playlist/([a-zA-Z0-9]+)'),
    'artist': re.compile(r'spotify\.com/artist/([a-zA-Z0-9]+)'),
    'show': re.compile(r'spotify\.com/show/([a-zA-Z0-9]+)'),
    'episode': re.compile(r'spotify\.com/episode/([a-zA-Z0-9]+)'),
}

def detect_spotify_content_type(url: str) -> Optional[str]:
    """Detect Spotify content type from URL. Handles ?si= and other query params."""
    for ptype, pat in SPOTIFY_PATTERNS.items():
        if pat.search(url):
            return ptype
    return None

def _spotify_id_from_url(url: str, ptype: str) -> Optional[str]:
    m = SPOTIFY_PATTERNS[ptype].search(url)
    return m.group(1) if m else None

def _uri_to_url(uri: str) -> str:
    """Convert spotify:track:ID to https URL."""
    if not uri:
        return ""
    parts = uri.split(':')
    if len(parts) == 3 and parts[0] == 'spotify':
        return f"https://open.spotify.com/{parts[1]}/{parts[2]}"
    return uri

def _parse_spotify_track_spotapi(spotify_id: str) -> Optional[dict]:
    """Parse a single Spotify track via spotapi (returns dict)."""
    if not SPOTAPI_AVAILABLE:
        return None
    try:
        data = SpotapiPublic.song_info(spotify_id)
        tu = deep_get(data, 'data', 'trackUnion', default={})
        if not tu:
            return None
        # Artist: try firstArtist first, then otherArtists, then albumOfTrack.artists
        artist = None
        artist = (deep_get(tu, 'firstArtist', 'items', 0, 'profile', 'name')
                  or deep_get(tu, 'otherArtists', 'items', 0, 'profile', 'name')
                  or deep_get(tu, 'albumOfTrack', 'artists', 'items', 0, 'profile', 'name'))
        # Cover
        cover = best_image_from_sources(deep_get(tu, 'albumOfTrack', 'coverArt', 'sources', default=[]))
        return {
            'name': tu.get('name'),
            'artist': artist,
            'album': deep_get(tu, 'albumOfTrack', 'name'),
            'duration_ms': deep_get(tu, 'duration', 'totalMilliseconds'),
            'cover': cover,
            'uri': tu.get('uri'),
            'url': _uri_to_url(tu.get('uri', '')),
            'album_uri': deep_get(tu, 'albumOfTrack', 'uri'),
            'year': deep_get(tu, 'albumOfTrack', 'date', 'year'),
        }
    except Exception as e:
        log.warning(f"spotapi track parse failed for {spotify_id}: {e}")
        return None

def _spotify_oembed(track_url: str) -> Optional[dict]:
    """Fallback: public oEmbed endpoint (only works for tracks)."""
    try:
        r = requests.get(f"https://open.spotify.com/oembed?url={track_url}", timeout=15)
        if r.status_code == 200:
            d = r.json()
            return {
                'name': d.get('title'),
                'artist': d.get('provider_name'),
                'album': None,
                'duration_ms': None,
                'cover': d.get('thumbnail_url'),
                'uri': None,
                'url': track_url,
            }
    except Exception as e:
        log.warning(f"oEmbed failed: {e}")
    return None

def _parse_spotify_album(spotify_id: str) -> Optional[dict]:
    """Parse a Spotify album via spotapi."""
    if not SPOTAPI_AVAILABLE:
        return None
    try:
        a = SpotapiPublicAlbum(spotify_id)
        data = a.get_album_info()
        au = deep_get(data, 'data', 'albumUnion', default={})
        if not au:
            return None
        artist = deep_get(au, 'artists', 'items', 0, 'profile', 'name')
        cover = best_image_from_sources(deep_get(au, 'coverArt', 'sources', default=[]))
        year = deep_get(au, 'date', 'year')
        total = deep_get(au, 'tracksV2', 'totalCount')
        tracks = []
        try:
            for page in a.paginate_album():
                if isinstance(page, list):
                    for item in page:
                        t = item.get('track', {}) if isinstance(item, dict) else {}
                        if not t:
                            continue
                        t_artist = (deep_get(t, 'artists', 'items', 0, 'profile', 'name')
                                    or artist)
                        tracks.append({
                            'name': t.get('name'),
                            'uri': t.get('uri'),
                            'url': _uri_to_url(t.get('uri', '')),
                            'duration_ms': deep_get(t, 'duration', 'totalMilliseconds'),
                            'artist': t_artist,
                            'album': au.get('name'),
                            'cover': cover,
                            'year': year,
                            'track_number': t.get('trackNumber'),
                        })
        except Exception as e:
            log.warning(f"paginate_album error: {e}")
        return {
            'name': au.get('name'),
            'artist': artist,
            'cover': cover,
            'year': year,
            'total': total,
            'tracks': tracks,
            'type': 'album',
        }
    except Exception as e:
        log.warning(f"spotapi album parse failed for {spotify_id}: {e}")
        return None

def _parse_spotify_playlist(spotify_id: str) -> Optional[dict]:
    """Parse a Spotify playlist via spotapi."""
    if not SPOTAPI_AVAILABLE:
        return None
    try:
        p = SpotapiPublicPlaylist(spotify_id)
        data = p.get_playlist_info()
        pv = deep_get(data, 'data', 'playlistV2', default={})
        if not pv:
            return None
        owner = deep_get(pv, 'ownerV2', 'data', 'name')
        # Cover: images.items[0].sources[]
        cover = None
        img_items = deep_get(pv, 'images', 'items', default=[])
        if img_items:
            cover = best_image_from_sources(img_items[0].get('sources', []))
        total = deep_get(pv, 'content', 'totalCount')
        tracks = []
        try:
            for page in p.paginate_playlist():
                if isinstance(page, dict):
                    for item in page.get('items', []):
                        d = deep_get(item, 'itemV2', 'data', default={})
                        if not d:
                            continue
                        t_artist = deep_get(d, 'artists', 'items', 0, 'profile', 'name')
                        t_cover = best_image_from_sources(deep_get(d, 'albumOfTrack', 'coverArt', 'sources', default=[]))
                        tracks.append({
                            'name': d.get('name'),
                            'uri': d.get('uri'),
                            'url': _uri_to_url(d.get('uri', '')),
                            'duration_ms': deep_get(d, 'trackDuration', 'totalMilliseconds'),
                            'artist': t_artist,
                            'album': deep_get(d, 'albumOfTrack', 'name'),
                            'cover': t_cover,
                        })
        except Exception as e:
            log.warning(f"paginate_playlist error: {e}")
        return {
            'name': pv.get('name'),
            'owner': owner,
            'cover': cover,
            'total': total,
            'tracks': tracks,
            'type': 'playlist',
        }
    except Exception as e:
        log.warning(f"spotapi playlist parse failed for {spotify_id}: {e}")
        return None

def parse_spotify_url(url: str) -> Optional[dict]:
    """Parse any Spotify URL and return unified info dict."""
    ptype = detect_spotify_content_type(url)
    if not ptype:
        return None
    sid = _spotify_id_from_url(url, ptype)
    if not sid:
        return None
    if ptype == 'track':
        info = _parse_spotify_track_spotapi(sid)
        if info:
            info['type'] = 'track'
            return info
        # Fallback to oEmbed
        oe = _spotify_oembed(url)
        if oe:
            oe['type'] = 'track'
            return oe
        return None
    elif ptype == 'album':
        info = _parse_spotify_album(sid)
        if info:
            return info
        return None
    elif ptype == 'playlist':
        info = _parse_spotify_playlist(sid)
        if info:
            return info
        return None
    elif ptype == 'artist':
        return {'type': 'artist', 'name': 'Artist', 'url': url,
                'tracks': [], 'cover': None}
    return None

def _build_youtube_search_query(track: dict) -> str:
    """Build a YouTube search query from a Spotify track dict."""
    name = (track.get('name') or '').strip()
    artist = (track.get('artist') or '').strip()
    if artist and name:
        return f"{artist} - {name}"
    if name:
        return name
    if artist:
        return artist
    return ""

def make_youtube_search_url(query: str) -> str:
    """Build a ytsearch URL for yt-dlp."""
    return f"ytsearch1:{query}"

# ============================================================
#  yt-dlp option builders
# ============================================================

def make_youtube_opts(workdir: str, format_id: str = None, progress_hook=None,
                      proxy_url: str = None, audio_only: bool = False,
                      cookies_path: str = None) -> dict:
    """Build yt-dlp options for YouTube."""
    opts = {
        'outtmpl': os.path.join(workdir, '%(title).80B.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'concurrent_fragment_downloads': 4,
        'geo_bypass': True,
        'noprogress': True,
        'ignoreerrors': False,
    }
    if cookies_path and os.path.exists(cookies_path):
        opts['cookiefile'] = cookies_path
    if audio_only:
        # Best audio, convert to MP3
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }]
        opts['prefer_ffmpeg'] = True
    else:
        if format_id:
            # Try the selected format, fallback to best
            opts['format'] = f"{format_id}+bestaudio/best/{format_id}/best"
            opts['merge_output_format'] = 'mp4'
            opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
            opts['prefer_ffmpeg'] = True
        else:
            opts['format'] = 'best[ext=mp4][height<=720]/best[ext=mp4]/best'
            opts['merge_output_format'] = 'mp4'
    if proxy_url:
        opts['proxy'] = proxy_url
    if progress_hook:
        opts['progress_hooks'] = [progress_hook]
    return opts

def make_sc_opts(workdir: str, quality: str = 'high', progress_hook=None,
                 force_mp3: bool = False, proxy_url: str = None) -> dict:
    """Build yt-dlp options for SoundCloud."""
    quality_map = {
        'high': '320',
        'medium': '128',
        'low': '64',
    }
    abr = quality_map.get(quality, '320')
    opts = {
        'outtmpl': os.path.join(workdir, '%(title).80B.%(ext)s'),
        'noplaylist': False,
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'socket_timeout': 30,
        'retries': 3,
        'geo_bypass': True,
        'noprogress': True,
        'ignoreerrors': True,  # Continue playlist even if one track fails
    }
    # SoundCloud: get best audio and convert to MP3 at chosen bitrate
    if force_mp3 or quality in ('high', 'medium', 'low'):
        opts['format'] = 'http_mp3_128/bestaudio/best'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': abr,
        }]
        opts['prefer_ffmpeg'] = True
    else:
        opts['format'] = 'bestaudio/best'
    if proxy_url:
        opts['proxy'] = proxy_url
    if progress_hook:
        opts['progress_hooks'] = [progress_hook]
    return opts

def make_generic_opts(workdir: str, progress_hook=None, proxy_url: str = None,
                      audio_only: bool = False) -> dict:
    """Build yt-dlp options for generic platforms (Pinterest, Instagram, TikTok, Twitter)."""
    opts = {
        'outtmpl': os.path.join(workdir, '%(title).80B.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'socket_timeout': 30,
        'retries': 3,
        'geo_bypass': True,
        'noprogress': True,
        'ignoreerrors': False,
    }
    # Use a realistic user-agent for platforms that block bots
    opts['http_headers'] = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/128.0.0.0 Safari/537.36'),
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if audio_only:
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        opts['prefer_ffmpeg'] = True
    else:
        # Best video+audio up to 1080p, merge to mp4.
        # Use bestvideo*+bestaudio/best to handle HLS/DASH streams (Pinterest, Instagram)
        # where video and audio are separate tracks.
        opts['format'] = ('bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/'
                          'bestvideo[height<=1080]+bestaudio/'
                          'best[ext=mp4][height<=1080]/best[ext=mp4]/best')
        opts['merge_output_format'] = 'mp4'
    if proxy_url:
        opts['proxy'] = proxy_url
    if progress_hook:
        opts['progress_hooks'] = [progress_hook]
    return opts

def make_tiktok_opts(workdir: str, progress_hook=None) -> dict:
    """TikTok-specific options with anti-block measures.
    TikTok blocks desktop UAs and redirects regional traffic to /about.
    We use a mobile UA + the mobile API endpoint to bypass this.
    """
    opts = make_generic_opts(workdir, progress_hook=progress_hook)
    # TikTok blocks desktop UAs; use a mobile UA (bypasses most blocks)
    opts['http_headers'] = {
        'User-Agent': ('Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/128.0.0.0 Mobile Safari/537.36'),
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.tiktok.com/',
    }
    # Allow merging video+audio (TikTok serves them separately in some cases)
    opts['format'] = ('bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
                      'best[ext=mp4]/best')
    opts['merge_output_format'] = 'mp4'
    # Use the mobile/share endpoint to avoid regional redirects
    opts['extractor_args'] = {'tiktok': {'download_addr': 'api'}}
    return opts

def make_twitter_opts(workdir: str, progress_hook=None) -> dict:
    """Twitter/X-specific options."""
    opts = make_generic_opts(workdir, progress_hook=progress_hook)
    opts['http_headers'] = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/128.0.0.0 Safari/537.36'),
        'Authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
    }
    return opts

# ============================================================
#  Sync download functions (run in threads via asyncio.to_thread)
# ============================================================

def _extract_info_sync(url: str, opts: dict) -> dict:
    """Run yt-dlp extract_info synchronously."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def _download_sync(url: str, opts: dict) -> Tuple[Optional[dict], Optional[str]]:
    """Run yt-dlp download synchronously. Returns (info_dict, filepath)."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            return None, None
        # Find the downloaded file
        filepath = ydl.prepare_filename(info)
        # Post-processors may change extension
        if 'requested_downloads' in info and info['requested_downloads']:
            rd = info['requested_downloads'][0]
            if 'filepath' in rd:
                filepath = rd['filepath']
            elif 'file' in rd:
                filepath = rd['file']
        # Check if file exists; if not, try common extensions
        if not os.path.exists(filepath):
            base, _ = os.path.splitext(filepath)
            for ext in ('.mp3', '.mp4', '.m4a', '.webm', '.opus', '.ogg', '.flac', '.wav'):
                candidate = base + ext
                if os.path.exists(candidate):
                    filepath = candidate
                    break
        return info, (filepath if os.path.exists(filepath) else None)

async def extract_info_async(url: str, opts: dict) -> dict:
    return await asyncio.to_thread(_extract_info_sync, url, opts)

async def download_async(url: str, opts: dict) -> Tuple[Optional[dict], Optional[str]]:
    return await asyncio.to_thread(_download_sync, url, opts)

# ============================================================
#  Spotify download (search YouTube, download, tag with cover)
# ============================================================

def _download_spotify_track_sync(track: dict, workdir: str, progress_hook=None) -> Tuple[Optional[str], Optional[dict]]:
    """Download a Spotify track by searching YouTube. Returns (filepath, yt_info)."""
    query = _build_youtube_search_query(track)
    if not query:
        return None, None
    search_url = make_youtube_search_url(query)
    opts = make_youtube_opts(workdir, audio_only=True, progress_hook=progress_hook,
                             cookies_path=COOKIES_PATH if COOKIES_AVAILABLE else None)
    try:
        info, filepath = _download_sync(search_url, opts)
        if filepath:
            # Embed metadata + cover
            embed_audio_metadata(
                filepath,
                title=track.get('name'),
                artist=track.get('artist'),
                album=track.get('album'),
                cover_url=track.get('cover'),
                track_num=track.get('track_number'),
                year=str(track.get('year')) if track.get('year') else None,
            )
            return filepath, info
    except Exception as e:
        log.warning(f"Spotify track download failed for {query}: {e}")
    return None, None

async def download_spotify_track(track: dict, workdir: str, progress_hook=None) -> Tuple[Optional[str], Optional[dict]]:
    """Async wrapper for Spotify track download."""
    return await asyncio.to_thread(_download_spotify_track_sync, track, workdir, progress_hook)


# ============================================================
#  SoundCloud download (with retry + proxy rotation)
# ============================================================

def _download_soundcloud_sync(url: str, workdir: str, quality: str = 'high',
                              is_search: bool = False, search_limit: int = 15,
                              progress_hook=None, max_retries: int = 5) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    """Download SoundCloud content. Returns (info, filepath, error).
    Strategy: try DIRECT first (SoundCloud is usually accessible), then proxy fallback.
    """
    last_err = None
    # Build the list of attempts: first 2 direct, then rotate proxies
    attempt_proxies = []
    for i in range(max(2, max_retries // 2)):
        attempt_proxies.append(None)  # direct first
    if ENABLE_PROXY_FOR_SOUNDCLOUD and ENABLE_PROXY_ROTATION:
        for i in range(max_retries - len(attempt_proxies)):
            p = proxy_mgr.next()
            if p:
                attempt_proxies.append(p)
    # If we have fewer attempts than max_retries, pad with direct
    while len(attempt_proxies) < max_retries:
        attempt_proxies.append(None)
    for attempt, proxy_url in enumerate(attempt_proxies[:max_retries]):
        opts = make_sc_opts(workdir, quality=quality, progress_hook=progress_hook,
                            force_mp3=FORCE_MP3 or True, proxy_url=proxy_url)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    last_err = "No info returned"
                    continue
                # Find file
                filepath = None
                if 'requested_downloads' in info and info['requested_downloads']:
                    rd = info['requested_downloads'][0]
                    filepath = rd.get('filepath') or rd.get('file')
                if not filepath:
                    filepath = ydl.prepare_filename(info)
                # Try common extensions
                if filepath and not os.path.exists(filepath):
                    base, _ = os.path.splitext(filepath)
                    for ext in ('.mp3', '.mp4', '.m4a', '.webm', '.opus', '.ogg', '.flac', '.wav'):
                        c = base + ext
                        if os.path.exists(c):
                            filepath = c
                            break
                if filepath and os.path.exists(filepath):
                    return info, filepath, None
                last_err = "File not found after download"
        except yt_dlp.utils.DownloadError as e:
            last_err = str(e)
            log.warning(f"SoundCloud attempt {attempt+1} (proxy={proxy_url}): {e}")
            if proxy_url:
                proxy_mgr.mark_bad(proxy_url)
            time.sleep(1 + attempt)
        except Exception as e:
            last_err = str(e)
            log.warning(f"SoundCloud attempt {attempt+1} error: {e}")
            time.sleep(1 + attempt)
    return None, None, last_err

async def download_soundcloud(url: str, workdir: str, quality: str = 'high',
                              is_search: bool = False, search_limit: int = 15,
                              progress_hook=None, max_retries: int = 5):
    """Async wrapper for SoundCloud download."""
    return await asyncio.to_thread(_download_soundcloud_sync, url, workdir, quality,
                                   is_search, search_limit, progress_hook, max_retries)

# ============================================================
#  YouTube info & download
# ============================================================

def is_youtube_short(url: str) -> bool:
    """Quick check if URL is a YouTube Short."""
    u = (url or '').lower()
    return '/shorts/' in u

def _get_youtube_info_sync(url: str) -> Optional[dict]:
    """Extract YouTube video info (no download)."""
    opts = {
        'quiet': True, 'no_warnings': True, 'no_color': True,
        'skip_download': True, 'noplaylist': True,
        'socket_timeout': 30, 'retries': 2,
        'geo_bypass': True,
    }
    if COOKIES_AVAILABLE:
        opts['cookiefile'] = COOKIES_PATH
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning(f"YouTube info extraction failed: {e}")
        return None

async def get_youtube_info(url: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_youtube_info_sync, url)

def _get_youtube_qualities_sync(url: str) -> List[dict]:
    """Get available video qualities for a YouTube URL."""
    info = _get_youtube_info_sync(url)
    if not info or not info.get('formats'):
        return []
    qualities = []
    seen = set()
    for f in info['formats']:
        if f.get('vcodec') and f['vcodec'] != 'none' and f.get('height'):
            h = f['height']
            fid = f.get('format_id', '')
            fps = f.get('fps', 30)
            ext = f.get('ext', 'mp4')
            label = f"{h}p"
            if fps > 30:
                label += f"_{fps}"
            if label in seen:
                continue
            seen.add(label)
            # Check if this format has both video and audio
            has_audio = f.get('acodec') and f['acodec'] != 'none'
            qualities.append({
                'label': label,
                'format_id': fid,
                'height': h,
                'fps': fps,
                'ext': ext,
                'has_audio': has_audio,
            })
    # Sort by height descending
    qualities.sort(key=lambda q: (q['height'], q['fps']), reverse=True)
    return qualities

async def get_youtube_qualities(url: str) -> List[dict]:
    return await asyncio.to_thread(_get_youtube_qualities_sync, url)

def _download_youtube_sync(url: str, workdir: str, format_id: str = None,
                           audio_only: bool = False, progress_hook=None) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    """Download a YouTube video."""
    opts = make_youtube_opts(workdir, format_id=format_id, progress_hook=progress_hook,
                             audio_only=audio_only,
                             cookies_path=COOKIES_PATH if COOKIES_AVAILABLE else None)
    try:
        info, filepath = _download_sync(url, opts)
        if filepath:
            return info, filepath, None
        return info, None, "File not found"
    except Exception as e:
        return None, None, str(e)

async def download_youtube(url: str, workdir: str, format_id: str = None,
                           audio_only: bool = False, progress_hook=None):
    return await asyncio.to_thread(_download_youtube_sync, url, workdir, format_id,
                                   audio_only, progress_hook)

# ============================================================
#  Generic download (Pinterest, Instagram, TikTok, Twitter)
# ============================================================

def _download_generic_sync(url: str, workdir: str, platform: str = 'generic',
                           progress_hook=None) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    """Download from a generic platform."""
    if platform == 'tiktok':
        opts = make_tiktok_opts(workdir, progress_hook=progress_hook)
    elif platform == 'twitter':
        opts = make_twitter_opts(workdir, progress_hook=progress_hook)
    else:
        opts = make_generic_opts(workdir, progress_hook=progress_hook)
    try:
        info, filepath = _download_sync(url, opts)
        if filepath:
            return info, filepath, None
        return info, None, "File not found"
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        # Provide friendlier error messages
        if 'no video' in err.lower() or 'no video could be found' in err.lower():
            return None, None, 'no_video'
        if 'private' in err.lower() or 'unavailable' in err.lower() or 'not exist' in err.lower():
            return None, None, 'private'
        if 'rate-limited' in err.lower() or '429' in err:
            return None, None, 'rate_limited'
        return None, None, err
    except Exception as e:
        return None, None, str(e)

async def download_generic(url: str, workdir: str, platform: str = 'generic',
                           progress_hook=None):
    return await asyncio.to_thread(_download_generic_sync, url, workdir, platform, progress_hook)

# ============================================================
#  Send functions (audio/video/document + HD cover)
# ============================================================

async def send_hd_cover(chat_id: int, cover_url: str, caption: str = None,
                        reply_to: int = None) -> bool:
    """Send an HD cover image as a photo. Returns True on success."""
    if not cover_url:
        return False
    data = download_cover_bytes(cover_url)
    if not data:
        return False
    # Ensure JPEG format
    data = ensure_jpeg_bytes(data)
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(data, filename="cover.jpg"),
            caption=caption,
            reply_to_message_id=reply_to,
        )
        return True
    except Exception as e:
        log.warning(f"send_hd_cover failed: {e}")
        return False

async def send_audio_file(chat_id: int, filepath: str, title: str = None,
                          artist: str = None, duration: int = None,
                          cover_path: str = None, caption: str = None,
                          reply_to: int = None) -> bool:
    """Send an audio file. Uses send_audio with thumbnail."""
    try:
        thumb = None
        if cover_path and os.path.exists(cover_path):
            thumb = FSInputFile(cover_path)
        await bot.send_audio(
            chat_id=chat_id,
            audio=FSInputFile(filepath),
            caption=caption,
            title=title,
            performer=artist,
            duration=duration,
            thumbnail=thumb,
            reply_to_message_id=reply_to,
        )
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            thumb = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None
            await bot.send_audio(
                chat_id=chat_id, audio=FSInputFile(filepath), caption=caption,
                title=title, performer=artist, duration=duration, thumbnail=thumb,
                reply_to_message_id=reply_to,
            )
            return True
        except Exception as e2:
            log.warning(f"send_audio_file retry failed: {e2}")
            return False
    except Exception as e:
        log.warning(f"send_audio_file failed: {e}")
        # Fallback: send as document
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(filepath),
                caption=caption,
                reply_to_message_id=reply_to,
            )
            return True
        except Exception as e2:
            log.warning(f"send_audio_file document fallback failed: {e2}")
            return False

async def send_video_file(chat_id: int, filepath: str, caption: str = None,
                          duration: int = None, width: int = None, height: int = None,
                          cover_path: str = None, reply_to: int = None) -> bool:
    """Send a video file."""
    try:
        thumb = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None
        await bot.send_video(
            chat_id=chat_id,
            video=FSInputFile(filepath),
            caption=caption,
            duration=duration,
            width=width,
            height=height,
            thumbnail=thumb,
            reply_to_message_id=reply_to,
            supports_streaming=True,
        )
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            thumb = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None
            await bot.send_video(
                chat_id=chat_id, video=FSInputFile(filepath), caption=caption,
                duration=duration, width=width, height=height, thumbnail=thumb,
                reply_to_message_id=reply_to, supports_streaming=True,
            )
            return True
        except Exception as e2:
            log.warning(f"send_video_file retry failed: {e2}")
            return False
    except Exception as e:
        log.warning(f"send_video_file failed: {e}")
        # Fallback: send as document
        try:
            await bot.send_document(
                chat_id=chat_id, document=FSInputFile(filepath), caption=caption,
                reply_to_message_id=reply_to,
            )
            return True
        except Exception as e2:
            log.warning(f"send_video_file document fallback failed: {e2}")
            return False

async def send_document_file(chat_id: int, filepath: str, caption: str = None,
                             reply_to: int = None) -> bool:
    """Send a file as document (for files > 50MB or unsupported types)."""
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(filepath),
            caption=caption,
            reply_to_message_id=reply_to,
        )
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.send_document(
                chat_id=chat_id, document=FSInputFile(filepath), caption=caption,
                reply_to_message_id=reply_to,
            )
            return True
        except Exception as e2:
            log.warning(f"send_document_file retry failed: {e2}")
            return False
    except Exception as e:
        log.warning(f"send_document_file failed: {e}")
        return False

async def send_media_item(chat_id: int, filepath: str, is_audio: bool, caption: str,
                          title: str = None, artist: str = None, duration: int = None,
                          cover_path: str = None, width: int = None, height: int = None,
                          reply_to: int = None) -> bool:
    """Send a media file (audio or video) with the appropriate method."""
    file_size = get_actual_file_size(filepath)
    if file_size > TELEGRAM_UPLOAD_LIMIT:
        # Too large — send as document (no size limit for documents? Actually still 50MB for bots)
        return await send_document_file(chat_id, filepath, caption, reply_to)
    if is_audio:
        return await send_audio_file(chat_id, filepath, title=title, artist=artist,
                                     duration=duration, cover_path=cover_path,
                                     caption=caption, reply_to=reply_to)
    else:
        return await send_video_file(chat_id, filepath, caption=caption, duration=duration,
                                     width=width, height=height, cover_path=cover_path,
                                     reply_to=reply_to)

# ============================================================
#  Resolve short URLs (t.co, pin.it, on.soundcloud.com, etc.)
# ============================================================

def resolve_url(url: str, timeout: int = 10) -> str:
    """Follow redirects to get the final URL."""
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers={'User-Agent': 'Mozilla/5.0'})
        if r.url and r.url != url:
            return r.url
    except Exception:
        pass
    return url

async def resolve_url_async(url: str) -> str:
    return await asyncio.to_thread(resolve_url, url)

# ============================================================
#  Keyboards (InlineKeyboardBuilder with coloured buttons)
# ============================================================

def main_menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Main menu keyboard with coloured buttons."""
    b = InlineKeyboardBuilder()
    b.row(styled_button("🎵 " + tr(chat_id, 'menu_quality'), ButtonStyle.WARNING, callback_data="menu:quality"))
    b.row(styled_button("🌐 " + tr(chat_id, 'menu_language'), ButtonStyle.PRIMARY, callback_data="menu:language"))
    b.row(styled_button("📊 " + tr(chat_id, 'menu_stats'), ButtonStyle.PRIMARY, callback_data="menu:stats"))
    b.row(styled_button("🔍 " + tr(chat_id, 'menu_search'), ButtonStyle.SUCCESS, callback_data="menu:search"))
    b.row(styled_button("❓ " + tr(chat_id, 'menu_help'), ButtonStyle.INFO, callback_data="menu:help"))
    return b.as_markup()

def join_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Channel subscription keyboard."""
    b = InlineKeyboardBuilder()
    ch = CHANNEL_USERNAME.lstrip('@')
    b.row(styled_button("📢 " + tr(chat_id, 'join_btn'), ButtonStyle.DANGER,
                        url=f"https://t.me/{ch}"))
    b.row(styled_button("🔄 " + tr(chat_id, 'check_btn'), ButtonStyle.SUCCESS,
                        callback_data="check:join"))
    return b.as_markup()

def lang_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(styled_button("🇮🇷 فارسی", ButtonStyle.PRIMARY, callback_data="lang:fa"))
    b.row(styled_button("🇬🇧 English", ButtonStyle.PRIMARY, callback_data="lang:en"))
    return b.as_markup()

def sc_quality_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(styled_button("🟢 " + tr(chat_id, 'sc_quality_high'), ButtonStyle.SUCCESS, callback_data="scq:high"))
    b.row(styled_button("🟡 " + tr(chat_id, 'sc_quality_medium'), ButtonStyle.WARNING, callback_data="scq:medium"))
    b.row(styled_button("⚪ " + tr(chat_id, 'sc_quality_low'), ButtonStyle.NEUTRAL, callback_data="scq:low"))
    b.row(styled_button("🔙 " + tr(chat_id, 'menu_back'), ButtonStyle.PRIMARY, callback_data="menu:main"))
    return b.as_markup()

def create_spotify_keyboard(chat_id: int, content_type: str, track_count: int) -> InlineKeyboardMarkup:
    """Spotify action keyboard for album/playlist."""
    b = InlineKeyboardBuilder()
    if content_type in ('album', 'playlist'):
        b.row(styled_button(
            tr(chat_id, 'btn_download_all').format(count=track_count),
            ButtonStyle.SUCCESS, callback_data="sp:all"))
        b.row(styled_button(
            tr(chat_id, 'btn_pick_track'),
            ButtonStyle.PRIMARY, callback_data="sp:pick:0"))
    b.row(styled_button("🚫 " + tr(chat_id, 'btn_cancel'), ButtonStyle.DANGER, callback_data="sp:cancel"))
    return b.as_markup()

def create_spotify_track_keyboard(tracks: list, chat_id: int, page: int = 0,
                                  per_page: int = 8) -> InlineKeyboardMarkup:
    """Paginated track picker for Spotify album/playlist."""
    b = InlineKeyboardBuilder()
    start = page * per_page
    end = min(start + per_page, len(tracks))
    for i in range(start, end):
        t = tracks[i]
        title = t.get('name', f'Track {i+1}')
        artist = t.get('artist', '')
        label = f"{i+1}. {title}"
        if artist:
            label += f" — {artist[:20]}"
        if len(label) > 40:
            label = label[:37] + '...'
        b.row(styled_button(label, ButtonStyle.PRIMARY, callback_data=f"spp:{i}"))
    # Pagination
    nav = []
    if page > 0:
        nav.append(styled_button("⬅️ " + tr(chat_id, 'btn_prev'), ButtonStyle.PRIMARY,
                                 callback_data=f"sppg:{page-1}"))
    nav.append(styled_button("✖️ " + tr(chat_id, 'btn_close'), ButtonStyle.DANGER,
                             callback_data="sp:cancel"))
    if end < len(tracks):
        nav.append(styled_button(tr(chat_id, 'btn_next') + " ➡️", ButtonStyle.PRIMARY,
                                 callback_data=f"sppg:{page+1}"))
    if nav:
        b.row(*nav)
    # Back to main
    b.row(styled_button("🔙 " + tr(chat_id, 'menu_back'), ButtonStyle.WARNING,
                        callback_data="sp:back"))
    return b.as_markup()

def create_youtube_quality_keyboard(qualities: list, chat_id: int) -> InlineKeyboardMarkup:
    """YouTube quality picker."""
    b = InlineKeyboardBuilder()
    # Audio only option
    b.row(styled_button("🎵 " + tr(chat_id, 'yt_audio_only'), ButtonStyle.SUCCESS,
                        callback_data="yt:audio"))
    # Video qualities (max 8)
    for q in qualities[:8]:
        style = ButtonStyle.PRIMARY
        if q['height'] >= 1080:
            style = ButtonStyle.SUCCESS
        elif q['height'] >= 720:
            style = ButtonStyle.WARNING
        b.row(styled_button(f"🎬 {q['label']}", style,
                            callback_data=f"ytv:{q['format_id']}"))
    b.row(styled_button("🚫 " + tr(chat_id, 'btn_cancel'), ButtonStyle.DANGER,
                        callback_data="yt:cancel"))
    return b.as_markup()

def create_paginated_keyboard(choices: list, chat_id: int, page: int = 0,
                              per_page: int = 8, prefix: str = "search") -> InlineKeyboardMarkup:
    """Generic paginated keyboard for search/playlist results."""
    b = InlineKeyboardBuilder()
    start = page * per_page
    end = min(start + per_page, len(choices))
    for i in range(start, end):
        ch = choices[i]
        title = ch.get('title', f'Item {i+1}')
        if len(title) > 45:
            title = title[:42] + '...'
        b.row(styled_button(f"{i+1}. {title}", ButtonStyle.PRIMARY,
                            callback_data=f"{prefix}:{i}"))
    nav = []
    if page > 0:
        nav.append(styled_button("⬅️", ButtonStyle.PRIMARY, callback_data=f"{prefix}pg:{page-1}"))
    nav.append(styled_button("✖️ " + tr(chat_id, 'btn_close'), ButtonStyle.DANGER,
                             callback_data=f"{prefix}:cancel"))
    if end < len(choices):
        nav.append(styled_button("➡️", ButtonStyle.PRIMARY, callback_data=f"{prefix}pg:{page+1}"))
    if nav:
        b.row(*nav)
    return b.as_markup()

def stats_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(styled_button("📊 " + tr(chat_id, 'stats_period_all'), ButtonStyle.PRIMARY, callback_data="stat:all"))
    b.row(styled_button("📅 " + tr(chat_id, 'stats_period_weekly'), ButtonStyle.WARNING, callback_data="stat:weekly"))
    b.row(styled_button("📅 " + tr(chat_id, 'stats_period_daily'), ButtonStyle.NEUTRAL, callback_data="stat:daily"))
    b.row(styled_button("🔙 " + tr(chat_id, 'menu_back'), ButtonStyle.PRIMARY, callback_data="menu:main"))
    return b.as_markup()

def cancel_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(styled_button("🚫 " + tr(chat_id, 'btn_cancel'), ButtonStyle.DANGER, callback_data="cancel:op"))
    return b.as_markup()


# ============================================================
#  Spotify download orchestration (UI flow)
# ============================================================

async def handle_download_spotify(chat_id: int, url: str, status_msg: Message):
    """Handle a Spotify URL: parse, show keyboard, wait for user choice."""
    await safe_edit_message(status_msg, f"🔍 Parsing Spotify URL...")
    info = await asyncio.to_thread(parse_spotify_url, url)
    if not info:
        await safe_edit_message(status_msg, tr(chat_id, 'err_not_supported'))
        return
    ptype = info.get('type')
    if ptype == 'track':
        # Single track: download immediately
        await _spotify_download_single(chat_id, info, url, status_msg)
    elif ptype in ('album', 'playlist'):
        # Save to cache and show keyboard
        save_spotify_cache(chat_id, url, ptype, info.get('tracks', []),
                           {'name': info.get('name'), 'artist': info.get('artist'),
                            'owner': info.get('owner'), 'cover': info.get('cover'),
                            'year': info.get('year'), 'type': ptype})
        # Build info message
        cb = CaptionBuilder(chat_id)
        icon = "💿" if ptype == 'album' else "📋"
        cb.lines.append(f"{icon} <b>{info.get('name', 'Unknown')}</b>")
        if ptype == 'album':
            if info.get('artist'):
                cb.lines.append(f"🎤 {info['artist']}")
            if info.get('year'):
                cb.lines.append(f"📅 {info['year']}")
        else:
            if info.get('owner'):
                cb.lines.append(f"👤 {info['owner']}")
        cb.lines.append(f"🎵 {len(info.get('tracks', []))} tracks")
        text = cb.build()
        kb = create_spotify_keyboard(chat_id, ptype, len(info.get('tracks', [])))
        # Send cover + info + keyboard
        cover_url = info.get('cover')
        if cover_url:
            try:
                cover_data = download_cover_bytes(cover_url)
                if cover_data:
                    cover_data = ensure_jpeg_bytes(cover_data)
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=BufferedInputFile(cover_data, filename="cover.jpg"),
                        caption=text,
                        reply_markup=kb,
                    )
                    await status_msg.delete()
                    return
            except Exception as e:
                log.warning(f"Could not send Spotify cover: {e}")
        # Fallback: text only
        await safe_edit_message(status_msg, text, reply_markup=kb)
    else:
        await safe_edit_message(status_msg, tr(chat_id, 'err_not_supported'))

async def _spotify_download_single(chat_id: int, track: dict, original_url: str,
                                   status_msg: Message):
    """Download a single Spotify track."""
    workdir = tempfile.mkdtemp(prefix="spotify_")
    try:
        # Show "searching YouTube" message
        await safe_edit_message(status_msg,
            f"🔍 {tr(chat_id, 'sp_searching')}\n\n🎵 <b>{track.get('name', '?')}</b>\n🎤 {track.get('artist', '')}")
        # Progress bar
        pb = ProgressBar(bot, chat_id, status_msg.message_id,
                         total=0, title=f"⬇️ {tr(chat_id, 'downloading')}",
                         lang=get_user_lang(chat_id))
        filepath, yt_info = await download_spotify_track(track, workdir,
                                                         progress_hook=pb.make_ytdlp_hook())
        if not filepath:
            await safe_edit_message(status_msg, tr(chat_id, 'err_download'))
            return
        # Get actual file size (post-conversion)
        file_size = get_actual_file_size(filepath)
        # Build caption with ACTUAL file size (not download size)
        cb = CaptionBuilder(chat_id)
        cb.add_title(track.get('name'))
        cb.add_artist(track.get('artist'))
        if track.get('album'):
            cb.add_album(track['album'])
        if track.get('duration_ms'):
            cb.add_duration(track['duration_ms'] / 1000)
        cb.add_size(file_size)  # ACTUAL file size
        cb.add_platform('spotify')
        cb.add_footer()
        caption = cb.build()
        # Upload
        await pb.close(f"⬆️ {tr(chat_id, 'uploading')}")
        # Download cover for thumbnail
        cover_path = None
        if track.get('cover'):
            cover_path = download_thumb_hd(track['cover'], workdir)
        sent = await send_audio_file(
            chat_id, filepath,
            title=track.get('name'), artist=track.get('artist'),
            duration=int((track.get('duration_ms') or 0) / 1000),
            cover_path=cover_path, caption=caption,
            reply_to=None
        )
        if sent:
            add_detailed_stats(chat_id, 'spotify', 'audio', file_size)
            await pb.close(tr(chat_id, 'done') + " ✅")
            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            await safe_edit_message(status_msg, tr(chat_id, 'err_download'))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

async def _spotify_download_all(chat_id: int, tracks: list, original_url: str,
                                status_msg: Message, meta: dict = None):
    """Download all tracks from a Spotify album/playlist sequentially."""
    if not tracks:
        await safe_edit_message(status_msg, tr(chat_id, 'sp_no_tracks'))
        return
    total = len(tracks)
    workdir = tempfile.mkdtemp(prefix="spotify_all_")
    success = 0
    failed = 0
    try:
        for i, track in enumerate(tracks):
            # Update progress
            prog_text = (f"📥 {tr(chat_id, 'sp_downloading_all')}\n\n"
                         f"📊 {tr(chat_id, 'sp_progress').format(done=i, total=total)}\n\n"
                         f"🎧 <b>{track.get('name', '?')}</b>\n"
                         f"🎤 {track.get('artist', '')}")
            try:
                await status_msg.edit_text(prog_text)
            except Exception:
                pass
            # Download track
            track_workdir = tempfile.mkdtemp(prefix=f"sp_track_{i}_", dir=workdir)
            try:
                filepath, yt_info = await download_spotify_track(track, track_workdir)
                if not filepath:
                    failed += 1
                    continue
                file_size = get_actual_file_size(filepath)
                # Caption
                cb = CaptionBuilder(chat_id)
                cb.add_title(track.get('name'))
                cb.add_artist(track.get('artist'))
                if track.get('album'):
                    cb.add_album(track['album'])
                if track.get('duration_ms'):
                    cb.add_duration(track['duration_ms'] / 1000)
                cb.add_size(file_size)
                cb.add_platform('spotify')
                cb.add_footer()
                caption = cb.build()
                cover_path = None
                if track.get('cover'):
                    cover_path = download_thumb_hd(track['cover'], track_workdir)
                sent = await send_audio_file(
                    chat_id, filepath,
                    title=track.get('name'), artist=track.get('artist'),
                    duration=int((track.get('duration_ms') or 0) / 1000),
                    cover_path=cover_path, caption=caption,
                )
                if sent:
                    success += 1
                    add_detailed_stats(chat_id, 'spotify', 'audio', file_size)
                else:
                    failed += 1
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.5)
            finally:
                shutil.rmtree(track_workdir, ignore_errors=True)
        # Final summary
        summary = (f"✅ <b>{tr(chat_id, 'done')}</b>\n\n"
                   f"📊 {tr(chat_id, 'sp_progress').format(done=success, total=total)}\n"
                   f"❌ Failed: {failed}")
        try:
            await status_msg.edit_text(summary)
        except Exception:
            await bot.send_message(chat_id, summary)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

async def handle_spotify_callback(call: CallbackQuery):
    """Handle Spotify callback queries."""
    chat_id = call.message.chat.id
    data = call.data
    user_id = call.from_user.id
    msg = call.message

    if data == "sp:cancel":
        clear_spotify_cache(chat_id)
        try:
            await msg.edit_text(tr(chat_id, 'cancelled'))
        except Exception:
            pass
        await call.answer()
        return

    if data == "sp:back":
        # Go back to album/playlist view (re-fetch from cache)
        # We don't have the URL here, so just show main menu
        try:
            await msg.edit_text(tr(chat_id, 'menu_main'), reply_markup=main_menu_keyboard(chat_id))
        except Exception:
            pass
        await call.answer()
        return

    if data.startswith("sppg:"):
        # Track picker pagination
        page = int(data.split(":")[1])
        # Find the cached tracks — we need the URL. Use the last cached entry.
        with db_pool.get_conn() as c:
            r = c.execute("SELECT url FROM spotify_cache WHERE chat_id=? ORDER BY created_at DESC LIMIT 1",
                          (chat_id,)).fetchone()
        if not r:
            await call.answer("Cache expired", show_alert=True)
            return
        cache = get_spotify_cache(chat_id, r['url'])
        if not cache:
            await call.answer("Cache expired", show_alert=True)
            return
        kb = create_spotify_track_keyboard(cache['tracks'], chat_id, page=page)
        try:
            await msg.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await call.answer()
        return

    if data.startswith("spp:"):
        # Pick a specific track
        idx = int(data.split(":")[1])
        with db_pool.get_conn() as c:
            r = c.execute("SELECT url FROM spotify_cache WHERE chat_id=? ORDER BY created_at DESC LIMIT 1",
                          (chat_id,)).fetchone()
        if not r:
            await call.answer("Cache expired", show_alert=True)
            return
        cache = get_spotify_cache(chat_id, r['url'])
        if not cache or idx >= len(cache['tracks']):
            await call.answer("Invalid track", show_alert=True)
            return
        track = cache['tracks'][idx]
        # Show status message
        status = await bot.send_message(chat_id,
            f"🔍 {tr(chat_id, 'sp_searching')}\n\n🎵 <b>{track.get('name', '?')}</b>\n🎤 {track.get('artist', '')}")
        try:
            await msg.delete()
        except Exception:
            pass
        await _spotify_download_single(chat_id, track, r['url'], status)
        await call.answer()
        return

    if data == "sp:all":
        # Download all tracks
        with db_pool.get_conn() as c:
            r = c.execute("SELECT url FROM spotify_cache WHERE chat_id=? ORDER BY created_at DESC LIMIT 1",
                          (chat_id,)).fetchone()
        if not r:
            await call.answer("Cache expired", show_alert=True)
            return
        cache = get_spotify_cache(chat_id, r['url'])
        if not cache or not cache.get('tracks'):
            await call.answer("No tracks", show_alert=True)
            return
        try:
            await msg.edit_text(f"📥 {tr(chat_id, 'sp_downloading_all')}")
        except Exception:
            pass
        await _spotify_download_all(chat_id, cache['tracks'], r['url'], msg, cache.get('meta'))
        await call.answer()
        return

    if data == "sp:pick:0":
        # Show track picker page 0
        with db_pool.get_conn() as c:
            r = c.execute("SELECT url FROM spotify_cache WHERE chat_id=? ORDER BY created_at DESC LIMIT 1",
                          (chat_id,)).fetchone()
        if not r:
            await call.answer("Cache expired", show_alert=True)
            return
        cache = get_spotify_cache(chat_id, r['url'])
        if not cache or not cache.get('tracks'):
            await call.answer("No tracks", show_alert=True)
            return
        kb = create_spotify_track_keyboard(cache['tracks'], chat_id, page=0)
        try:
            await msg.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await call.answer()
        return

# ============================================================
#  Safe message editing
# ============================================================

async def safe_edit_message(msg: Message, text: str, reply_markup=None):
    """Edit a message text safely (handles Telegram exceptions)."""
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        log.debug(f"edit_text failed: {e}")
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await msg.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass
    except Exception as e:
        log.debug(f"edit_text failed: {e}")

# ============================================================
#  SoundCloud & generic download orchestration
# ============================================================

async def handle_download_soundcloud(chat_id: int, url: str, status_msg: Message):
    """Handle a SoundCloud URL: could be track, playlist, or album."""
    await safe_edit_message(status_msg, f"🔍 {tr(chat_id, 'sc_searching')}")
    quality = get_user_quality(chat_id)
    workdir = tempfile.mkdtemp(prefix="sc_")
    try:
        # First extract info to see if it's a playlist
        opts = make_sc_opts(workdir, quality=quality)
        opts['skip_download'] = True
        opts['noplaylist'] = False
        info = await extract_info_async(url, opts)
        if not info:
            await safe_edit_message(status_msg, tr(chat_id, 'err_download'))
            return
        # Check if it's a playlist/album
        if info.get('_type') == 'playlist' or 'entries' in info:
            entries = [e for e in info.get('entries', []) if e]
            if entries:
                # Download all tracks in the playlist
                await _soundcloud_download_playlist(chat_id, url, entries, status_msg, workdir, quality)
                return
        # Single track download
        await _soundcloud_download_single(chat_id, url, status_msg, workdir, quality)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

async def _soundcloud_download_single(chat_id: int, url: str, status_msg: Message,
                                      workdir: str, quality: str):
    """Download a single SoundCloud track."""
    pb = ProgressBar(bot, chat_id, status_msg.message_id,
                     total=0, title=f"⬇️ {tr(chat_id, 'downloading')}",
                     lang=get_user_lang(chat_id))
    info, filepath, err = await download_soundcloud(
        url, workdir, quality=quality, progress_hook=pb.make_ytdlp_hook(), max_retries=3)
    if not filepath:
        await safe_edit_message(status_msg, tr(chat_id, 'err_download'))
        return
    file_size = get_actual_file_size(filepath)
    # Extract metadata from yt-dlp info
    title = (info or {}).get('title') or (info or {}).get('track') or 'audio'
    artist = (info or {}).get('uploader') or (info or {}).get('artist') or ''
    duration = int((info or {}).get('duration', 0))
    thumb_url = (info or {}).get('thumbnail')
    # Embed metadata (including HD cover)
    embed_audio_metadata(filepath, title=title, artist=artist, cover_url=thumb_url)
    # Build caption
    cb = CaptionBuilder(chat_id)
    cb.add_title(title)
    if artist:
        cb.add_artist(artist)
    cb.add_duration(duration)
    cb.add_size(file_size)
    cb.add_platform('soundcloud')
    cb.add_footer()
    caption = cb.build()
    # Download HD cover for thumbnail
    cover_path = None
    if thumb_url:
        cover_path = download_thumb_hd(thumb_url, workdir)
    await pb.close(f"⬆️ {tr(chat_id, 'uploading')}")
    sent = await send_audio_file(
        chat_id, filepath, title=title, artist=artist, duration=duration,
        cover_path=cover_path, caption=caption)
    if sent:
        add_detailed_stats(chat_id, 'soundcloud', 'audio', file_size)
        await pb.close(tr(chat_id, 'done') + " ✅")
        try:
            await status_msg.delete()
        except Exception:
            pass
    else:
        await safe_edit_message(status_msg, tr(chat_id, 'err_download'))

async def _soundcloud_download_playlist(chat_id: int, url: str, entries: list,
                                        status_msg: Message, workdir: str, quality: str):
    """Download all tracks in a SoundCloud playlist/album."""
    total = len(entries)
    success = 0
    failed = 0
    try:
        for i, entry in enumerate(entries):
            entry_url = entry.get('url') or entry.get('webpage_url')
            if not entry_url:
                failed += 1
                continue
            prog_text = (f"📥 {tr(chat_id, 'sp_downloading_all')}\n\n"
                         f"📊 {tr(chat_id, 'sp_progress').format(done=i, total=total)}\n\n"
                         f"🎧 <b>{entry.get('title', '?')}</b>")
            try:
                await status_msg.edit_text(prog_text)
            except Exception:
                pass
            track_workdir = tempfile.mkdtemp(prefix=f"sc_t_{i}_", dir=workdir)
            try:
                info, filepath, err = await download_soundcloud(
                    entry_url, track_workdir, quality=quality, max_retries=2)
                if not filepath:
                    failed += 1
                    continue
                file_size = get_actual_file_size(filepath)
                title = entry.get('title') or (info or {}).get('title') or 'audio'
                artist = (info or {}).get('uploader') or (entry.get('uploader') or '')
                duration = int((info or {}).get('duration', 0))
                thumb_url = (info or {}).get('thumbnail') or entry.get('thumbnail')
                embed_audio_metadata(filepath, title=title, artist=artist, cover_url=thumb_url)
                cb = CaptionBuilder(chat_id)
                cb.add_title(title)
                if artist:
                    cb.add_artist(artist)
                cb.add_duration(duration)
                cb.add_size(file_size)
                cb.add_platform('soundcloud')
                cb.add_footer()
                caption = cb.build()
                cover_path = download_thumb_hd(thumb_url, track_workdir) if thumb_url else None
                sent = await send_audio_file(
                    chat_id, filepath, title=title, artist=artist, duration=duration,
                    cover_path=cover_path, caption=caption)
                if sent:
                    success += 1
                    add_detailed_stats(chat_id, 'soundcloud', 'audio', file_size)
                else:
                    failed += 1
                await asyncio.sleep(0.5)
            finally:
                shutil.rmtree(track_workdir, ignore_errors=True)
        summary = (f"✅ <b>{tr(chat_id, 'done')}</b>\n\n"
                   f"📊 {tr(chat_id, 'sp_progress').format(done=success, total=total)}\n"
                   f"❌ Failed: {failed}")
        try:
            await status_msg.edit_text(summary)
        except Exception:
            await bot.send_message(chat_id, summary)
    except Exception as e:
        log.error(f"SoundCloud playlist download error: {e}")
        await safe_edit_message(status_msg, tr(chat_id, 'err_download'))

async def handle_download_youtube(chat_id: int, url: str, status_msg: Message):
    """Handle a YouTube URL: extract qualities, let user pick, download."""
    await safe_edit_message(status_msg, f"🔍 {tr(chat_id, 'progress_extracting')}")
    # Check if it's a short
    is_short = is_youtube_short(url)
    # Get qualities
    qualities = await get_youtube_qualities(url)
    if not qualities:
        # Cookies likely needed
        await safe_edit_message(status_msg,
            f"❌ {tr(chat_id, 'yt_no_formats')}\n\n"
            f"💡 YouTube requires cookies. Please add a cookies.txt file.")
        return
    # Save URL for later use
    save_youtube_qualities(chat_id, url, qualities)
    save_youtube_shorts_info(chat_id, url, is_short)
    # Show quality picker
    text = tr(chat_id, 'yt_quality')
    if is_short:
        text = f"📱 {tr(chat_id, 'yt_shorts_detected')}\n\n{text}"
    kb = create_youtube_quality_keyboard(qualities, chat_id)
    await safe_edit_message(status_msg, text, reply_markup=kb)

async def handle_youtube_quality_selection(call: CallbackQuery):
    """Handle YouTube quality selection callback."""
    chat_id = call.message.chat.id
    data = call.data
    msg = call.message
    # Get the cached URL
    with db_pool.get_conn() as c:
        r = c.execute("SELECT url FROM youtube_quality_cache WHERE chat_id=? ORDER BY created_at DESC LIMIT 1",
                      (chat_id,)).fetchone()
    if not r:
        await call.answer("Cache expired, please resend the link", show_alert=True)
        return
    url = r['url']
    if data == "yt:cancel":
        try:
            await msg.edit_text(tr(chat_id, 'cancelled'))
        except Exception:
            pass
        await call.answer()
        return
    audio_only = (data == "yt:audio")
    format_id = None if audio_only else data.split(":", 1)[1]
    await call.answer()
    try:
        await msg.edit_text(f"⬇️ {tr(chat_id, 'downloading')}")
    except Exception:
        pass
    workdir = tempfile.mkdtemp(prefix="yt_")
    try:
        pb = ProgressBar(bot, chat_id, msg.message_id,
                         total=0, title=f"⬇️ {tr(chat_id, 'downloading')}",
                         lang=get_user_lang(chat_id))
        info, filepath, err = await download_youtube(
            url, workdir, format_id=format_id, audio_only=audio_only,
            progress_hook=pb.make_ytdlp_hook())
        if not filepath:
            err_msg = tr(chat_id, 'err_download')
            if err and ('cookie' in err.lower() or 'sign in' in err.lower()):
                err_msg = "❌ YouTube requires cookies. Please add a cookies.txt file."
            await safe_edit_message(msg, err_msg)
            return
        file_size = get_actual_file_size(filepath)
        if file_size > TELEGRAM_UPLOAD_LIMIT:
            await safe_edit_message(msg, tr(chat_id, 'err_too_large'))
            return
        title = (info or {}).get('title') or 'video'
        artist = (info or {}).get('uploader') or (info or {}).get('channel') or ''
        duration = int((info or {}).get('duration', 0))
        thumb_url = (info or {}).get('thumbnail')
        width = (info or {}).get('width')
        height = (info or {}).get('height')
        # Build caption
        cb = CaptionBuilder(chat_id)
        cb.add_title(title)
        if artist:
            cb.add_artist(artist)
        cb.add_duration(duration)
        cb.add_size(file_size)
        cb.add_platform('youtube')
        cb.add_footer()
        caption = cb.build()
        cover_path = None
        if thumb_url:
            cover_path = download_thumb_hd(thumb_url, workdir)
        await pb.close(f"⬆️ {tr(chat_id, 'uploading')}")
        if audio_only:
            # Embed metadata
            embed_audio_metadata(filepath, title=title, artist=artist, cover_url=thumb_url)
            sent = await send_audio_file(
                chat_id, filepath, title=title, artist=artist, duration=duration,
                cover_path=cover_path, caption=caption)
            if sent:
                add_detailed_stats(chat_id, 'youtube', 'audio', file_size)
        else:
            sent = await send_video_file(
                chat_id, filepath, caption=caption, duration=duration,
                width=width, height=height, cover_path=cover_path)
            if sent:
                add_detailed_stats(chat_id, 'youtube', 'video', file_size)
        if sent:
            await pb.close(tr(chat_id, 'done') + " ✅")
            try:
                await msg.delete()
            except Exception:
                pass
        else:
            await safe_edit_message(msg, tr(chat_id, 'err_download'))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

async def handle_generic_download(chat_id: int, url: str, platform: str, status_msg: Message):
    """Handle a generic platform download (Pinterest, Instagram, TikTok, Twitter)."""
    await safe_edit_message(status_msg, f"⬇️ {tr(chat_id, 'downloading')}")
    # Resolve short URLs
    resolved = await resolve_url_async(url)
    workdir = tempfile.mkdtemp(prefix=f"{platform}_")
    try:
        pb = ProgressBar(bot, chat_id, status_msg.message_id,
                         total=0, title=f"⬇️ {tr(chat_id, 'downloading')}",
                         lang=get_user_lang(chat_id))
        info, filepath, err = await download_generic(
            resolved, workdir, platform=platform, progress_hook=pb.make_ytdlp_hook())
        if not filepath:
            if err == 'no_video':
                await safe_edit_message(status_msg, tr(chat_id, 'err_no_video'))
            elif err == 'private':
                await safe_edit_message(status_msg, tr(chat_id, 'err_private'))
            else:
                await safe_edit_message(status_msg, tr(chat_id, 'err_download'))
            return
        file_size = get_actual_file_size(filepath)
        if file_size > TELEGRAM_UPLOAD_LIMIT:
            await safe_edit_message(status_msg, tr(chat_id, 'err_too_large'))
            return
        title = (info or {}).get('title') or f'{platform}_video'
        artist = (info or {}).get('uploader') or (info or {}).get('channel') or (info or {}).get('author') or ''
        duration = int((info or {}).get('duration', 0))
        thumb_url = (info or {}).get('thumbnail')
        width = (info or {}).get('width')
        height = (info or {}).get('height')
        # Determine if audio or video
        is_audio = filepath.lower().endswith(('.mp3', '.m4a', '.opus', '.ogg', '.flac', '.wav'))
        cb = CaptionBuilder(chat_id)
        cb.add_title(title)
        if artist:
            cb.add_artist(artist)
        cb.add_duration(duration)
        cb.add_size(file_size)
        cb.add_platform(platform)
        cb.add_footer()
        caption = cb.build()
        cover_path = None
        if thumb_url:
            cover_path = download_thumb_hd(thumb_url, workdir)
        await pb.close(f"⬆️ {tr(chat_id, 'uploading')}")
        if is_audio:
            sent = await send_audio_file(
                chat_id, filepath, title=title, artist=artist, duration=duration,
                cover_path=cover_path, caption=caption)
            if sent:
                add_detailed_stats(chat_id, platform, 'audio', file_size)
        else:
            sent = await send_video_file(
                chat_id, filepath, caption=caption, duration=duration,
                width=width, height=height, cover_path=cover_path)
            if sent:
                add_detailed_stats(chat_id, platform, 'video', file_size)
        if sent:
            await pb.close(tr(chat_id, 'done') + " ✅")
            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            await safe_edit_message(status_msg, tr(chat_id, 'err_download'))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

async def do_search(chat_id: int, query: str):
    """Search SoundCloud for a query and show results."""
    status = await bot.send_message(chat_id, f"🔍 {tr(chat_id, 'sc_searching')}")
    workdir = tempfile.mkdtemp(prefix="scsearch_")
    try:
        search_url = f"scsearch15:{query}"
        opts = make_sc_opts(workdir, quality=get_user_quality(chat_id))
        opts['skip_download'] = True
        opts['extract_flat'] = True
        info = await extract_info_async(search_url, opts)
        if not info or not info.get('entries'):
            await safe_edit_message(status, tr(chat_id, 'sc_no_results'))
            return
        entries = [e for e in info['entries'] if e][:15]
        if not entries:
            await safe_edit_message(status, tr(chat_id, 'sc_no_results'))
            return
        save_search_choices(chat_id, entries)
        kb = create_paginated_keyboard(entries, chat_id, page=0, prefix="search")
        text = f"🔍 {tr(chat_id, 'sc_search_results')} ({len(entries)})\n\n{tr(chat_id, 'sc_pick_track')}"
        await safe_edit_message(status, text, reply_markup=kb)
    except Exception as e:
        log.error(f"Search error: {e}")
        await safe_edit_message(status, tr(chat_id, 'err_download'))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

# ============================================================
#  Stats display
# ============================================================

def build_stats_text(chat_id: int, period: str = 'all') -> str:
    """Build stats text for a user."""
    lang = get_user_lang(chat_id)
    stats = get_stats(chat_id)
    platform_stats = get_user_platform_stats(chat_id, period=period)
    cb = CaptionBuilder(chat_id)
    cb.lines.append(f"📊 <b>{tr(chat_id, 'stats_title')}</b>")
    if period == 'daily':
        cb.lines.append(f"📅 {tr(chat_id, 'stats_period_daily')}")
    elif period == 'weekly':
        cb.lines.append(f"📅 {tr(chat_id, 'stats_period_weekly')}")
    else:
        cb.lines.append(f"📅 {tr(chat_id, 'stats_period_all')}")
    cb.add_separator()
    if not stats['total_count']:
        cb.lines.append(tr(chat_id, 'stats_no_data'))
        return cb.build()
    cb.lines.append(f"📥 {tr(chat_id, 'stats_total')}: <b>{stats['total_count']}</b>")
    cb.lines.append(f"💾 {tr(chat_id, 'stats_total_size')}: <b>{human_size(stats['total_size'])}</b>")
    if platform_stats:
        cb.lines.append("")
        cb.lines.append(f"📈 {tr(chat_id, 'stats_by_platform')}:")
        for ps in platform_stats:
            platform_name = {
                'spotify': '🎧 Spotify', 'soundcloud': '☁️ SoundCloud',
                'youtube': '📺 YouTube', 'pinterest': '📌 Pinterest',
                'instagram': '📸 Instagram', 'tiktok': '🎵 TikTok',
                'twitter': '🐦 Twitter',
            }.get(ps['platform'], ps['platform'])
            cb.lines.append(f"  {platform_name}: {ps['n']} ({human_size(ps['s'])})")
    return cb.build()

# ============================================================
#  Message handlers
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    chat_id = message.chat.id
    ensure_user(chat_id, message.from_user.username, message.from_user.first_name)
    # Check channel membership
    if not await is_member(chat_id):
        await message.answer(tr(chat_id, 'must_join'), reply_markup=join_keyboard(chat_id))
        return
    name = message.from_user.first_name or "friend"
    text = tr(chat_id, 'welcome').format(bot_name=BOT_USERNAME)
    text += "\n\n" + tr(chat_id, 'features_lines')
    await message.answer(text, reply_markup=main_menu_keyboard(chat_id))

@router.message(Command("help"))
async def cmd_help(message: Message):
    chat_id = message.chat.id
    if not await is_member(chat_id):
        await message.answer(tr(chat_id, 'must_join'), reply_markup=join_keyboard(chat_id))
        return
    await message.answer(tr(chat_id, 'help_text'), reply_markup=main_menu_keyboard(chat_id))

@router.message(Command("menu"))
async def cmd_menu(message: Message):
    chat_id = message.chat.id
    if not await is_member(chat_id):
        await message.answer(tr(chat_id, 'must_join'), reply_markup=join_keyboard(chat_id))
        return
    await message.answer(tr(chat_id, 'menu_main'), reply_markup=main_menu_keyboard(chat_id))

@router.message(Command("lang"))
async def cmd_lang(message: Message):
    chat_id = message.chat.id
    lang = get_user_lang(chat_id)
    await message.answer(tr(chat_id, 'lang_current').format(lang=lang), reply_markup=lang_keyboard())

@router.message(Command("quality"))
async def cmd_quality(message: Message):
    chat_id = message.chat.id
    q = get_user_quality(chat_id)
    qmap = {'high': 'High 320kbps', 'medium': 'Medium 128kbps', 'low': 'Low 64kbps'}
    await message.answer(tr(chat_id, 'quality_current').format(quality=qmap.get(q, q)),
                         reply_markup=sc_quality_keyboard(chat_id))

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    chat_id = message.chat.id
    text = build_stats_text(chat_id, 'all')
    await message.answer(text, reply_markup=stats_keyboard(chat_id))

@router.message(Command("search"))
async def cmd_search(message: Message):
    chat_id = message.chat.id
    if not await is_member(chat_id):
        await message.answer(tr(chat_id, 'must_join'), reply_markup=join_keyboard(chat_id))
        return
    args = message.text.split(None, 1)
    if len(args) < 2:
        await message.answer(tr(chat_id, 'search_prompt'))
        return
    query = args[1].strip()
    await do_search(chat_id, query)

@router.callback_query(F.data == "check:join")
async def cb_check_join(call: CallbackQuery):
    chat_id = call.message.chat.id
    if await is_member(chat_id):
        await call.answer(tr(chat_id, 'joined_check'), show_alert=True)
        name = call.from_user.first_name or "friend"
        text = tr(chat_id, 'welcome').format(bot_name=BOT_USERNAME)
        text += "\n\n" + tr(chat_id, 'features_lines')
        try:
            await call.message.edit_text(text, reply_markup=main_menu_keyboard(chat_id))
        except Exception:
            await call.message.answer(text, reply_markup=main_menu_keyboard(chat_id))
    else:
        await call.answer("❌ You haven't joined yet!", show_alert=True)

@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery):
    chat_id = call.message.chat.id
    lang = call.data.split(":")[1]
    set_user_lang(chat_id, lang)
    lang_names = {'fa': 'فارسی', 'en': 'English'}
    await call.answer(f"✅ {lang_names.get(lang, lang)}", show_alert=False)
    await call.message.edit_text(tr(chat_id, 'lang_current').format(lang=lang_names.get(lang, lang)),
                                reply_markup=lang_keyboard())

@router.callback_query(F.data.startswith("scq:"))
async def cb_sc_quality(call: CallbackQuery):
    chat_id = call.message.chat.id
    q = call.data.split(":")[1]
    set_user_quality(chat_id, q)
    qmap = {'high': 'High 320kbps', 'medium': 'Medium 128kbps', 'low': 'Low 64kbps'}
    await call.answer(tr(chat_id, 'quality_set').format(quality=qmap.get(q, q)))
    await call.message.edit_text(tr(chat_id, 'quality_current').format(quality=qmap.get(q, q)),
                                reply_markup=main_menu_keyboard(chat_id))

@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(call: CallbackQuery):
    chat_id = call.message.chat.id
    action = call.data.split(":")[1]
    await call.answer()
    if action == "main":
        await call.message.edit_text(tr(chat_id, 'menu_main'), reply_markup=main_menu_keyboard(chat_id))
    elif action == "quality":
        q = get_user_quality(chat_id)
        qmap = {'high': 'High 320kbps', 'medium': 'Medium 128kbps', 'low': 'Low 64kbps'}
        await call.message.edit_text(tr(chat_id, 'quality_current').format(quality=qmap.get(q, q)),
                                    reply_markup=sc_quality_keyboard(chat_id))
    elif action == "language":
        lang = get_user_lang(chat_id)
        await call.message.edit_text(tr(chat_id, 'lang_current').format(lang=lang),
                                    reply_markup=lang_keyboard())
    elif action == "stats":
        text = build_stats_text(chat_id, 'all')
        await call.message.edit_text(text, reply_markup=stats_keyboard(chat_id))
    elif action == "help":
        await call.message.edit_text(tr(chat_id, 'help_text'), reply_markup=main_menu_keyboard(chat_id))
    elif action == "search":
        await call.message.edit_text(tr(chat_id, 'search_prompt'))

@router.callback_query(F.data.startswith("stat:"))
async def cb_stat(call: CallbackQuery):
    chat_id = call.message.chat.id
    period = call.data.split(":")[1]
    await call.answer()
    text = build_stats_text(chat_id, period)
    try:
        await call.message.edit_text(text, reply_markup=stats_keyboard(chat_id))
    except Exception:
        # If text is the same, just ignore
        pass

@router.callback_query(F.data.startswith("sp:"))
async def cb_spotify(call: CallbackQuery):
    await handle_spotify_callback(call)

@router.callback_query(F.data.startswith("spp"))
async def cb_spotify_pick(call: CallbackQuery):
    await handle_spotify_callback(call)

@router.callback_query(F.data.startswith("yt:"))
async def cb_youtube(call: CallbackQuery):
    await handle_youtube_quality_selection(call)

@router.callback_query(F.data.startswith("ytv:"))
async def cb_youtube_video(call: CallbackQuery):
    await handle_youtube_quality_selection(call)

@router.callback_query(F.data.startswith("search:"))
async def cb_search_pick(call: CallbackQuery):
    chat_id = call.message.chat.id
    data = call.data
    msg = call.message
    if data == "search:cancel":
        try:
            await msg.edit_text(tr(chat_id, 'cancelled'))
        except Exception:
            pass
        await call.answer()
        return
    if data.startswith("searchpg:"):
        page = int(data.split(":")[1])
        with db_pool.get_conn() as c:
            # Get all saved choices for this user (we saved 15)
            rows = c.execute("SELECT idx, choice_json FROM search_choices WHERE chat_id=? ORDER BY idx",
                            (chat_id,)).fetchall()
        choices = [json.loads(r['choice_json']) for r in rows]
        kb = create_paginated_keyboard(choices, chat_id, page=page, prefix="search")
        try:
            await msg.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await call.answer()
        return
    if data.startswith("search:"):
        idx = int(data.split(":")[1])
        choice = get_search_choice(chat_id, idx)
        if not choice:
            await call.answer("Choice expired", show_alert=True)
            return
        await call.answer()
        url = choice.get('url') or choice.get('webpage_url')
        if not url:
            await msg.edit_text(tr(chat_id, 'err_invalid_url'))
            return
        await msg.edit_text(f"⬇️ {tr(chat_id, 'downloading')}")
        workdir = tempfile.mkdtemp(prefix="sc_pick_")
        try:
            await _soundcloud_download_single(chat_id, url, msg, workdir, get_user_quality(chat_id))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

@router.callback_query(F.data == "cancel:op")
async def cb_cancel(call: CallbackQuery):
    chat_id = call.message.chat.id
    try:
        await call.message.edit_text(tr(chat_id, 'cancelled'))
    except Exception:
        pass
    await call.answer()

@router.message(F.text)
async def handle_message(message: Message):
    """Handle text messages — detect URLs and route to appropriate handler."""
    chat_id = message.chat.id
    ensure_user(chat_id, message.from_user.username, message.from_user.first_name)
    # Check channel membership
    if not await is_member(chat_id):
        await message.answer(tr(chat_id, 'must_join'), reply_markup=join_keyboard(chat_id))
        return
    text = (message.text or '').strip()
    # If it's a command we don't handle, ignore
    if text.startswith('/'):
        return
    # Check if it's a URL
    url_match = re.search(r'https?://[^\s]+', text)
    if not url_match:
        # Maybe it's a search query for SoundCloud? Only if short text.
        if len(text) < 200 and not any(c in text for c in '\n'):
            # Treat as SoundCloud search
            await do_search(chat_id, text)
            return
        await message.answer(tr(chat_id, 'send_link'))
        return
    url = url_match.group(0)
    platform = detect_platform_from_url(url)
    if platform == 'unknown':
        await message.answer(tr(chat_id, 'err_not_supported'))
        return
    # Create a status message
    status = await message.answer(f"⏳ {tr(chat_id, 'processing')}")
    try:
        if platform == 'spotify':
            await handle_download_spotify(chat_id, url, status)
        elif platform == 'soundcloud':
            await handle_download_soundcloud(chat_id, url, status)
        elif platform == 'youtube':
            await handle_download_youtube(chat_id, url, status)
        elif platform in ('pinterest', 'instagram', 'tiktok', 'twitter'):
            await handle_generic_download(chat_id, url, platform, status)
        else:
            await safe_edit_message(status, tr(chat_id, 'err_not_supported'))
    except Exception as e:
        log.error(f"Download handler error: {e}", exc_info=True)
        await safe_edit_message(status, tr(chat_id, 'err_download'))

# ============================================================
#  Keepalive (aiohttp web server)
# ============================================================

async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)

async def ping_handler(request: web.Request) -> web.Response:
    return web.Response(text="pong", status=200)

async def status_handler(request: web.Request) -> web.Response:
    stats = get_uptime_stats()
    return web.json_response({
        'status': 'ok',
        'bot': BOT_USERNAME,
        'uptime_stats': stats,
        'spotapi': SPOTAPI_AVAILABLE,
        'mutagen': MUTAGEN_AVAILABLE,
        'cookies': COOKIES_AVAILABLE,
    })

async def root_handler(request: web.Request) -> web.Response:
    html = """<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>Aurora Downloader Bot</title>
<style>body{font-family:system-ui;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}.card{background:rgba(255,255,255,0.1);backdrop-filter:blur(10px);padding:40px;border-radius:20px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,0.2)}h1{margin:0 0 10px;font-size:2.5em}p{opacity:0.9;margin:5px 0}</style>
</head><body><div class='card'><h1>🌟 Aurora</h1><p>Downloader Bot is running</p><p>Supports: SoundCloud · Spotify · YouTube · Pinterest · Instagram · TikTok · Twitter</p></div></body></html>"""
    return web.Response(text=html, content_type='text/html')

def setup_keepalive():
    """Setup aiohttp keepalive web server."""
    app = web.Application()
    app.router.add_get('/', root_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/ping', ping_handler)
    app.router.add_get('/status', status_handler)
    return app

# ============================================================
#  Main
# ============================================================

async def on_startup():
    """Called when the bot starts."""
    await _fetch_bot_username()
    log.info("=" * 50)
    log.info(f"  Aurora Downloader Bot v5.0 (aiogram 3.x)")
    log.info(f"  Bot: @{BOT_USERNAME}")
    log.info(f"  Channel: {CHANNEL_USERNAME}")
    log.info(f"  Cookies: {'available' if COOKIES_AVAILABLE else 'not found'}")
    log.info(f"  spotapi: {'available' if SPOTAPI_AVAILABLE else 'NOT available'}")
    log.info(f"  mutagen: {'available' if MUTAGEN_AVAILABLE else 'NOT available'}")
    log.info(f"  Keepalive: http://0.0.0.0:{PORT}")
    log.info("=" * 50)

async def main():
    await on_startup()
    # Start keepalive server in background
    app = setup_keepalive()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    log.info(f"Keepalive server started on port {PORT}")
    # Start polling (this blocks)
    try:
        await dp.start_polling(bot, polling_timeout=60)
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

# ============================================================
#  END OF FILE
# ============================================================
