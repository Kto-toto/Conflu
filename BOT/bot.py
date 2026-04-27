import os
import re
import json
import hashlib
import sqlite3
import asyncio
import logging
import calendar
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from html import escape as html_escape

import requests
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== ENV/.env ==================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

PPLX_API_KEY = os.getenv("PPLX_API_KEY", "").strip()
PPLX_API_URL = os.getenv("PPLX_API_URL", "https://api.perplexity.ai/chat/completions").strip()
PPLX_MODEL = os.getenv("PPLX_MODEL", "sonar-pro").strip()

DB_FILE = str((BASE_DIR / os.getenv("DB_FILE", "bot.db")).resolve())
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300").strip())
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20").strip())
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (sozd-monitor-bot)").strip()

HEADERS = {"User-Agent": USER_AGENT}

SOZD_HOME_URL = "https://sozd.duma.gov.ru/oz"
SOZD_BILL_URL = "https://sozd.duma.gov.ru/bill/{bill_number}"
SOZD_RSS_URL = "https://sozd.duma.gov.ru/bill/{bill_number}/rss"
SOZD_CALENDAR_RANGE_URL = "https://sozd.duma.gov.ru/calendar/b/day/{d1}/{d2}/1.1"

MSK = ZoneInfo("Europe/Moscow")

ADD_NUM, ADD_TITLE = range(2)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ================== DB ==================
def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_init():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_bills (
            user_id INTEGER NOT NULL,
            bill_number TEXT NOT NULL,
            official_title TEXT NOT NULL,
            custom_title TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (user_id, bill_number)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_bill_last (
            user_id INTEGER NOT NULL,
            bill_number TEXT NOT NULL,
            last_event_hash TEXT,
            last_event_json TEXT,
            last_sent_at TEXT,
            PRIMARY KEY (user_id, bill_number)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_recent_seen (
            user_id INTEGER NOT NULL,
            bill_number TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_shown_at TEXT NOT NULL,
            PRIMARY KEY (user_id, bill_number)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bill_note_cache (
            bill_number TEXT PRIMARY KEY,
            note_hash TEXT NOT NULL,
            note_text TEXT NOT NULL,
            note_summary TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def db_upsert_user(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
        (user_id, now_utc_iso()),
    )
    conn.commit()
    conn.close()


def db_user_has_bill(user_id: int, bill_number: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM user_bills WHERE user_id=? AND bill_number=? LIMIT 1",
        (user_id, bill_number),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def db_add_bill(user_id: int, bill_number: str, official_title: str, custom_title: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO user_bills(user_id, bill_number, official_title, custom_title, added_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, bill_number, official_title, custom_title, now_utc_iso()),
    )
    inserted = cur.rowcount > 0

    if inserted:
        cur.execute(
            """
            INSERT OR IGNORE INTO user_bill_last(user_id, bill_number, last_event_hash, last_event_json, last_sent_at)
            VALUES (?, ?, NULL, NULL, NULL)
            """,
            (user_id, bill_number),
        )

    conn.commit()
    conn.close()
    return inserted


def db_remove_bill(user_id: int, bill_number: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_bills WHERE user_id=? AND bill_number=?", (user_id, bill_number))
    removed = cur.rowcount > 0
    cur.execute("DELETE FROM user_bill_last WHERE user_id=? AND bill_number=?", (user_id, bill_number))
    conn.commit()
    conn.close()
    return removed


def db_get_user_bills(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT bill_number, official_title, custom_title, added_at
        FROM user_bills
        WHERE user_id=?
        ORDER BY added_at DESC
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def db_get_users_tracking_bill(bill_number: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT user_id, custom_title
        FROM user_bills
        WHERE bill_number=?
        """,
        (bill_number,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def db_get_distinct_bill_numbers():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT bill_number FROM user_bills")
    rows = [r["bill_number"] for r in cur.fetchall()]
    conn.close()
    return rows


def db_get_user_last_event(user_id: int, bill_number: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT last_event_hash, last_event_json, last_sent_at
        FROM user_bill_last
        WHERE user_id=? AND bill_number=?
        """,
        (user_id, bill_number),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    last_json = row["last_event_json"]
    return {
        "hash": row["last_event_hash"],
        "event": json.loads(last_json) if last_json else None,
        "sent_at": row["last_sent_at"],
    }


def db_set_user_last_event(user_id: int, bill_number: str, event_hash: str, event: dict):
    conn = db_connect()
    cur = conn.cursor()
    now = now_utc_iso()
    cur.execute(
        """
        INSERT OR REPLACE INTO user_bill_last(user_id, bill_number, last_event_hash, last_event_json, last_sent_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, bill_number, event_hash, json.dumps(event, ensure_ascii=False), now),
    )
    conn.commit()
    conn.close()


def db_user_recent_seen_has(user_id: int, bill_number: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM user_recent_seen WHERE user_id=? AND bill_number=? LIMIT 1",
        (user_id, bill_number),
    )
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def db_user_recent_seen_mark(user_id: int, bill_number: str):
    conn = db_connect()
    cur = conn.cursor()
    now = now_utc_iso()
    cur.execute(
        """
        INSERT INTO user_recent_seen(user_id, bill_number, first_seen_at, last_shown_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, bill_number) DO UPDATE SET last_shown_at=excluded.last_shown_at
        """,
        (user_id, bill_number, now, now),
    )
    conn.commit()
    conn.close()


def db_get_note_cache(bill_number: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT note_hash, note_text, note_summary, updated_at
        FROM bill_note_cache
        WHERE bill_number=?
        """,
        (bill_number,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def db_set_note_cache(bill_number: str, note_hash: str, note_text: str, note_summary: str):
    conn = db_connect()
    cur = conn.cursor()
    now = now_utc_iso()
    cur.execute(
        """
        INSERT INTO bill_note_cache(bill_number, note_hash, note_text, note_summary, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(bill_number) DO UPDATE SET
            note_hash=excluded.note_hash,
            note_text=excluded.note_text,
            note_summary=excluded.note_summary,
            updated_at=excluded.updated_at
        """,
        (bill_number, note_hash, note_text, note_summary, now),
    )
    conn.commit()
    conn.close()


# ================== HTML helpers ==================
def h(text: str) -> str:
    return html_escape(text or "", quote=False)


def bill_link(bill_number: str) -> str:
    url = SOZD_BILL_URL.format(bill_number=bill_number)
    return f'<a href="{url}">{h(bill_number)}</a>'


def quote_title(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return ""
    if (t.startswith("«") and t.endswith("»")) or (t.startswith('"') and t.endswith('"')):
        return t
    return f"«{t}»"


def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def normalize_bill_number(text: str) -> str | None:
    s = (text or "").strip()
    if not re.fullmatch(r"\d+-\d+", s):
        return None
    return s


def make_event_hash(event: dict) -> str:
    base = (event.get("title", "") + "||" + event.get("description", "")).encode("utf-8")
    return hashlib.sha1(base).hexdigest()


# ================== Keyboards ==================
def reply_menu_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("/add"), KeyboardButton("/my"), KeyboardButton("/help")],
            [KeyboardButton("/menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def inline_main_menu_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить проект", callback_data="add")],
            [InlineKeyboardButton("📋 Мои проекты", callback_data="my")],
            [InlineKeyboardButton("🆕 Недавно внесённые", callback_data="recent")],
            [InlineKeyboardButton("🗞 Сводка за сутки", callback_data="digest_yesterday")],
            [InlineKeyboardButton("🌐 Поиск на сайте Госдумы", url=SOZD_HOME_URL)],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        ]
    )


def help_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="back")]])


def add_step1_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌐 Найти законопроект на сайте", url=SOZD_HOME_URL)],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ]
    )


def add_step2_kb(bill_number: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌐 Открыть законопроект", url=SOZD_BILL_URL.format(bill_number=bill_number)),
                InlineKeyboardButton("✅ Оставить официальное", callback_data="keep_official"),
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ]
    )


def my_projects_list_kb(bills_rows):
    keyboard = []
    for r in bills_rows[:50]:
        bn = r["bill_number"]
        title = (r["custom_title"] or "").strip()
        keyboard.append([InlineKeyboardButton(f"📌 {title} — {bn}", callback_data=f"proj:{bn}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(keyboard)


def project_actions_kb(bill_number: str):
    url = SOZD_BILL_URL.format(bill_number=bill_number)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("ℹ️ Посмотреть текущий статус", callback_data=f"status:{bill_number}")],
            [InlineKeyboardButton("🧾 Краткая суть (ПЗ)", callback_data=f"note:{bill_number}")],
            [InlineKeyboardButton("🌐 Открыть на сайте Госдумы", url=url)],
            [InlineKeyboardButton("❌ Удалить из мониторинга", callback_data=f"del:{bill_number}")],
            [InlineKeyboardButton("⬅️ Назад к списку", callback_data="my")],
            [InlineKeyboardButton("🏠 В главное меню", callback_data="back")],
        ]
    )


# ================== Texts ==================
def welcome_text(bills_count: int) -> str:
    return (
        "Привет! Это бот для <b>отслеживания законопроектов</b> на сайте Госдумы.\n"
        "Он проверяет изменения статуса и присылает уведомления.\n\n"
        "Как пользоваться:\n"
        "• Нажмите «➕ Добавить проект» или отправьте /add\n"
        "• Введите номер вида <b>1124826-8</b>\n"
        "• (Опционально) задайте своё короткое название\n\n"
        f"<b>Ваши проекты:</b> {bills_count}"
    )


def main_menu_text(bills_count: int) -> str:
    return (
        "🏠 <b>Главное меню</b>\n\n"
        f"📌 <b>Ваши проекты:</b> {bills_count}\n\n"
        "Быстрые действия:\n"
        "• ➕ Добавить проект\n"
        "• 📋 Мои проекты\n"
        "• 🆕 Недавно внесённые\n"
        "• 🗞 Сводка за сутки\n"
    )


# ================== RSS ==================
def entry_dt_utc(entry) -> datetime | None:
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    ts = calendar.timegm(t)
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def get_latest_rss_event_sync(bill_number: str):
    rss_url = SOZD_RSS_URL.format(bill_number=bill_number)
    feed = feedparser.parse(rss_url)
    if not feed.entries:
        return None

    entry = feed.entries[0]
    title = clean_spaces(getattr(entry, "title", "") or "")
    description = clean_spaces(getattr(entry, "description", "") or "")

    date_match = re.search(r"\((\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})\)", title)
    dt = date_match.group(1) if date_match else ""

    clean_title = re.sub(r"\)\s*\d+\.\d+\s*", ") ", title)
    clean_title = re.sub(r"^\(.*?\)\s*", "", clean_title)

    return {"date": dt, "title": clean_title.strip(), "description": description}


async def get_latest_rss_event(bill_number: str):
    return await asyncio.to_thread(get_latest_rss_event_sync, bill_number)


def get_rss_events_in_window_sync(bill_number: str, start_msk: datetime, end_msk: datetime, limit: int = 30) -> list[dict]:
    rss_url = SOZD_RSS_URL.format(bill_number=bill_number)
    feed = feedparser.parse(rss_url)
    if not feed.entries:
        return []

    out = []
    for entry in feed.entries[:limit]:
        title = clean_spaces(getattr(entry, "title", "") or "")
        description = clean_spaces(getattr(entry, "description", "") or "")

        dt_utc = entry_dt_utc(entry)
        if dt_utc is None:
            m = re.search(r"\((\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})\)", title)
            if m:
                try:
                    dt_msk = datetime.strptime(m.group(1), "%d.%m.%Y %H:%M:%S").replace(tzinfo=MSK)
                    dt_utc = dt_msk.astimezone(timezone.utc)
                except Exception:
                    dt_utc = None

        if dt_utc is None:
            continue

        dt_msk = dt_utc.astimezone(MSK)
        if start_msk <= dt_msk <= end_msk:
            clean_title = re.sub(r"\)\s*\d+\.\d+\s*", ") ", title)
            clean_title = re.sub(r"^\(.*?\)\s*", "", clean_title)
            out.append(
                {
                    "dt_msk": dt_msk,
                    "date": dt_msk.strftime("%d.%m.%Y %H:%M:%S"),
                    "title": clean_title.strip(),
                    "description": description,
                }
            )

    out.sort(key=lambda x: x["dt_msk"])
    return out


async def get_rss_events_in_window(bill_number: str, start_msk: datetime, end_msk: datetime, limit: int = 30) -> list[dict]:
    return await asyncio.to_thread(get_rss_events_in_window_sync, bill_number, start_msk, end_msk, limit)


# ================== SOZD parsing ==================
def fetch_official_title_sync(bill_number: str) -> str | None:
    url = SOZD_BILL_URL.format(bill_number=bill_number)
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    raw_text = soup.get_text("\n", strip=True)

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(О|Об)\s", line) and len(line) > 10:
            return line

    og = soup.find("meta", property="og:title")
    raw = og["content"].strip() if og and og.get("content") else ""
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"\s*::\s*Система обеспечения законодательной деятельности\s*$", "", raw).strip()
    raw = re.sub(r"\s*\|\s*Система обеспечения законодательной деятельности.*$", "", raw).strip()
    return raw or None


async def fetch_official_title(bill_number: str):
    return await asyncio.to_thread(fetch_official_title_sync, bill_number)


def extract_deadlines_sync(bill_number: str):
    url = SOZD_BILL_URL.format(bill_number=bill_number)
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
    except Exception:
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    deadlines = {}

    for tag in soup.find_all(string=True):
        text = (tag or "").strip()
        if not text:
            continue

        text_lower = text.lower()
        date_matches = re.findall(r"\b(\d{2}\.\d{2}\.\d{4})\b", text)
        if not date_matches:
            continue

        if any(k in text_lower for k in ["представить", "срок представления", "предлагаемый срок"]):
            if any(ignore in text_lower for ignore in ["дата рассмотрения", "включить в порядок", "рассмотрения государственной думой"]):
                continue

            if "поправ" in text_lower:
                deadlines["Поправки"] = date_matches[0]
            elif any(w in text_lower for w in ["отзыв", "предлож", "замеч"]):
                deadlines["Отзывы и предложения"] = date_matches[0]

    return deadlines


async def extract_deadlines(bill_number: str):
    return await asyncio.to_thread(extract_deadlines_sync, bill_number)


def extract_note_text_sync(bill_number: str) -> str | None:
    url = SOZD_BILL_URL.format(bill_number=bill_number) + "#bh_note"
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    anchor = soup.find(id="bh_note")
    if anchor:
        container = anchor
        for _ in range(3):
            if container and container.parent:
                container = container.parent
        t = container.get_text("\n", strip=True) if container else ""
        t = t.strip()
        if len(t) >= 200:
            return t

    raw = soup.get_text("\n", strip=True)
    raw = raw.strip()
    return raw if len(raw) >= 200 else None


async def extract_note_text(bill_number: str):
    return await asyncio.to_thread(extract_note_text_sync, bill_number)


def parse_calendar_bills_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/bill/(\d+-\d+)", href)
        if not m:
            continue
        bn = m.group(1)
        title = clean_spaces(a.get_text(" ", strip=True))
        url = "https://sozd.duma.gov.ru" + href if href.startswith("/") else href
        items.append({"bill_number": bn, "title": title or bn, "url": url})

    uniq = {}
    for it in items:
        uniq[it["bill_number"]] = it
    return list(uniq.values())


def fetch_recent_bills_sync(d1: date, d2: date) -> list[dict]:
    url = SOZD_CALENDAR_RANGE_URL.format(d1=d1.isoformat(), d2=d2.isoformat())
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception:
        return []
    return parse_calendar_bills_html(r.text)


async def fetch_recent_bills(d1: date, d2: date) -> list[dict]:
    return await asyncio.to_thread(fetch_recent_bills_sync, d1, d2)


# ================== Formatting (old-style) ==================
def extract_decision(description: str) -> str | None:
    if not description:
        return None
    if "Принято решение:" not in description:
        return None
    decision = description.split("Принято решение:", 1)[1].strip()
    return decision or None


def format_status(event: dict) -> str:
    title = h(event.get("title", ""))
    descr = event.get("description", "") or ""
    lines = []

    if event.get("date"):
        lines.append(f"📌 <b>{h(event['date'])}</b> {title}")
    else:
        lines.append(title)

    decision = extract_decision(descr)
    if decision:
        lines.append("")
        lines.append(f"📝 <b>Принято решение:</b> {h(decision)}")

    return "\n".join(lines).strip()


def format_deadlines(deadlines: dict) -> str:
    if not deadlines:
        return ""
    line = "────────────"
    lines = [f"\n{line}\n⏰ Сроки:"]
    for k, v in deadlines.items():
        lines.append(f"• {h(k)}: {h(v)}")
    return "\n".join(lines)


# ================== Send helpers ==================
async def safe_send_html(bot, chat_id: int, text: str, reply_markup=None):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("send_message failed")


async def safe_edit_html(query_message, text: str, reply_markup=None):
    try:
        await query_message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("edit_text failed")


def find_custom_title(bills_rows, bill_number: str) -> str:
    for r in bills_rows:
        if r["bill_number"] == bill_number:
            return r["custom_title"] or bill_number
    return bill_number


# ================== LLM ==================
def pplx_summarize_note_sync(note_text: str) -> str:
    if not PPLX_API_KEY:
        raise RuntimeError("PPLX_API_KEY не задан в .env")

    system = (
        "Ты аналитик по законодательству (GR/Regulatory). "
        "Суммируй пояснительную записку законопроекта на русском, для чтения в Telegram. \n\n"
        "Правила:\n"
        "1) Не цитируй длинные фрагменты дословно — только перефразирование.Не вставляй в текст маркеры цитирования вида, и т.п.\n"
        "2) Пиши конкретно: что вводится/меняется/запрещается/разрешается, кто обязан, какие сроки/порог/штрафы (если есть).\n"
        "3) Никаких оценочных суждений, рекомендаций и 'кому важно'.\n"
        "4) Формат вывода (plain text, без Markdown/HTML):\n"
        "Суть:\n"
        "<3–10 строк связного текста>\n\n"
        "Ключевые изменения:\n"
        "• ... (4–10 пунктов)\n"
    )

    payload = {
        "model": PPLX_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": note_text[:25000]},
        ],
        "temperature": 0.2,
    }

    r = requests.post(
        PPLX_API_URL,
        headers={"Authorization": f"Bearer {PPLX_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()
    text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("Perplexity вернул пустой ответ.")
    return text


async def pplx_summarize_note(note_text: str) -> str:
    return await asyncio.to_thread(pplx_summarize_note_sync, note_text)


async def get_note_summary_cached_llm(bill_number: str) -> dict | None:
    note_url = SOZD_BILL_URL.format(bill_number=bill_number) + "#bh_note"
    note_text = await extract_note_text(bill_number)
    if not note_text:
        return None

    note_text_norm = clean_spaces(note_text)
    note_hash = hashlib.sha1(note_text_norm.encode("utf-8")).hexdigest()

    cached = db_get_note_cache(bill_number)
    if cached and cached.get("note_hash") == note_hash and (cached.get("note_summary") or "").strip():
        return {"note_summary": cached["note_summary"], "note_url": note_url, "cached": True}

    summary = await pplx_summarize_note(note_text_norm)
    db_set_note_cache(bill_number, note_hash, note_text_norm, summary)
    return {"note_summary": summary, "note_url": note_url, "cached": False}


# ================== Commands ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    db_upsert_user(user_id)
    bills = db_get_user_bills(user_id)
    await safe_send_html(
        context.bot,
        user_id,
        welcome_text(len(bills)),
        reply_markup=inline_main_menu_kb(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    bills = db_get_user_bills(user_id)
    await safe_send_html(
        context.bot,
        user_id,
        main_menu_text(len(bills)),
        reply_markup=inline_main_menu_kb(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    txt = (
        "<b>Помощь</b>\n\n"
	"<b>Помощь</b>\n\n"
        "Команды:\n"
        "• /add — добавить законопроект\n"
        "• /my — мои проекты\n"
        "• /menu — меню\n\n"
        "LLM используется только по кнопке «Краткая суть (ПЗ)»."
    )
    await safe_send_html(context.bot, user_id, txt, reply_markup=help_kb())


async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    bills = db_get_user_bills(user_id)
    if not bills:
        await safe_send_html(
            context.bot,
            user_id,
            "Ваш список пуст. Добавьте через /add.",
            reply_markup=inline_main_menu_kb(),
        )
        return
    await safe_send_html(
        context.bot,
        user_id,
        f"<b>Ваши проекты:</b> {len(bills)}\n\nВыберите проект:",
        reply_markup=my_projects_list_kb(bills),
    )


# ================== Inline callbacks ==================
async def project_open_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.message.chat_id
    bill_number = q.data.split("proj:", 1)[1].strip()

    bills = db_get_user_bills(user_id)
    title = find_custom_title(bills, bill_number)

    txt = (
        "📌 <b>Проект</b>\n\n"
        f"Номер: <b>{h(bill_number)}</b>\n"
        f"Название: {h(quote_title(title))}\n\n"
        "Выберите действие:"
    )
    await safe_edit_html(q.message, txt, reply_markup=project_actions_kb(bill_number))


async def project_status_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.message.chat_id
    bill_number = q.data.split("status:", 1)[1].strip()

    bills = db_get_user_bills(user_id)
    title = find_custom_title(bills, bill_number)

    event = await get_latest_rss_event(bill_number)
    deadlines = await extract_deadlines(bill_number)
    if not event:
        await safe_send_html(context.bot, user_id, "Не удалось получить статус. Попробуйте позже.", reply_markup=inline_main_menu_kb())
        return

    msg = (
        f"ℹ️ Законопроект № {bill_link(bill_number)} {h(quote_title(title))}\n\n"
        f"Текущий статус:\n{format_status(event)}"
        f"{format_deadlines(deadlines)}"
    )
    await safe_send_html(context.bot, user_id, msg, reply_markup=inline_main_menu_kb())


async def project_note_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.message.chat_id
    bill_number = q.data.split("note:", 1)[1].strip()

    try:
        res = await get_note_summary_cached_llm(bill_number)
    except Exception as e:
        await safe_send_html(context.bot, user_id, f"LLM ошибка: {h(str(e))}", reply_markup=inline_main_menu_kb())
        return

    if not res:
        await safe_send_html(context.bot, user_id, "Не удалось извлечь ПЗ.", reply_markup=inline_main_menu_kb())
        return

    txt = h(res["note_summary"])
    txt = txt.replace("Суть:", "<b>Суть:</b>")
    txt = txt.replace("Ключевые изменения:", "<b>Ключевые изменения:</b>")

    msg = (
        f"🧾 <b>Краткая суть (LLM)</b>\n"
        f"Законопроект № {bill_link(bill_number)}\n\n"
        f"{txt}\n\n"
        f"Источник: <a href=\"{h(res['note_url'])}\">пояснительная записка</a>"
    )
    await safe_send_html(context.bot, user_id, msg, reply_markup=inline_main_menu_kb())


async def project_delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.message.chat_id
    bill_number = q.data.split("del:", 1)[1].strip()

    removed = db_remove_bill(user_id, bill_number)
    if removed:
        await safe_edit_html(q.message, f"✅ {h(bill_number)} удалён.", reply_markup=inline_main_menu_kb())
    else:
        await safe_send_html(context.bot, user_id, "Не найдено.", reply_markup=inline_main_menu_kb())


async def show_recent_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.message.chat_id

    today = date.today()
    yesterday = today - timedelta(days=1)

    bills = await fetch_recent_bills(yesterday, today)
    if not bills:
        await safe_send_html(context.bot, user_id, "Не удалось получить список недавно внесённых.", reply_markup=inline_main_menu_kb())
        return

    bills = sorted(bills, key=lambda x: x["bill_number"])
    new_items, seen_items = [], []
    for b in bills:
        if db_user_recent_seen_has(user_id, b["bill_number"]):
            seen_items.append(b)
        else:
            new_items.append(b)

    lines = [f"🆕 <b>Законопроекты, внесенные в Госдуму за последние два дня:</b> {len(bills)}", ""]
    lines.append(f"✨ <b>Новые (не отображались в подборке):</b> {len(new_items)}")
    for it in new_items:
        bn = it["bill_number"]
        ttl = it.get("title") or bn
        url = SOZD_BILL_URL.format(bill_number=bn)
        lines.append(f"• <a href=\"{h(url)}\">{h(bn)}</a> — {h(clean_spaces(ttl))}")

    lines.append("")
    lines.append(f"🗂 <b>Ранее показанные:</b> {len(seen_items)}")
    for it in seen_items[:120]:
        bn = it["bill_number"]
        ttl = it.get("title") or bn
        url = SOZD_BILL_URL.format(bill_number=bn)
        lines.append(f"• <a href=\"{h(url)}\">{h(bn)}</a> — {h(clean_spaces(ttl))}")

    for it in bills:
        db_user_recent_seen_mark(user_id, it["bill_number"])

    await safe_send_html(context.bot, user_id, "\n".join(lines), reply_markup=inline_main_menu_kb())


async def show_digest_yesterday_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.message.chat_id

    bills = db_get_user_bills(user_id)
    if not bills:
        await safe_send_html(context.bot, user_id, "Нет отслеживаемых проектов.", reply_markup=inline_main_menu_kb())
        return

    now_msk = datetime.now(MSK)
    y = now_msk.date() - timedelta(days=1)
    start_msk = datetime(y.year, y.month, y.day, 0, 0, 0, tzinfo=MSK)
    end_msk = datetime(y.year, y.month, y.day, 23, 59, 59, tzinfo=MSK)

    header = "🗞 <b>Сводка за сутки</b>\n<i>(события по вашим проектам за вчерашний день)</i>"
    sep = "\n────────────\n"

    changed_blocks = []
    unchanged_lines = []

    for r in bills[:50]:
        bn = r["bill_number"]
        title = r["custom_title"] or bn

        events = await get_rss_events_in_window(bn, start_msk, end_msk, limit=30)
        if not events:
            unchanged_lines.append(f"🟢 {bill_link(bn)} — {h(quote_title(title))}")
            continue

        block = []
        # FIX: ссылка на карточку ГД для проектов с событиями
        block.append(f"📌 {bill_link(bn)} — <b>{h(quote_title(title))}</b>")
        block.append(f"События за сутки: <b>{len(events)}</b>")

        for ev in events[:6]:
            try:
                dt = datetime.strptime(ev["date"], "%d.%m.%Y %H:%M:%S")
                dt_s = dt.strftime("%d.%m %H:%M")
            except Exception:
                dt_s = ev["date"]
            block.append(f"• <b>{h(dt_s)}</b> {h(ev['title'])}")

        if len(events) > 6:
            block.append(f"• … ещё {len(events) - 6}")

        changed_blocks.append("\n".join(block))

    out_parts = [header]
    out_parts.append(sep.join(changed_blocks) if changed_blocks else "За сутки по отслеживаемым проектам изменений не обнаружено.")
    if unchanged_lines:
        out_parts.append("\n<b>Без изменений:</b>\n" + "\n".join(unchanged_lines))

    await safe_send_html(context.bot, user_id, "\n\n".join(out_parts), reply_markup=inline_main_menu_kb())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "add":
        return await add_entry_from_inline(update, context)
    if data == "my":
        return await cmd_my(update, context)
    if data == "recent":
        return await show_recent_cb(update, context)
   
    if data == "digest_yesterday":
        return await show_digest_yesterday_cb(update, context)
    if data == "help":
        return await cmd_help(update, context)
    if data in ("back", "cancel"):
        return await cmd_menu(update, context)

    if data.startswith("proj:"):
        return await project_open_cb(update, context)
    if data.startswith("status:"):
        return await project_status_cb(update, context)
    if data.startswith("note:"):
        return await project_note_cb(update, context)
    if data.startswith("del:"):
        return await project_delete_cb(update, context)


# ================== ADD conversation ==================
async def add_entry_from_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.message.chat_id

    context.user_data.pop("pending_bill_number", None)
    context.user_data.pop("pending_official_title", None)

    await safe_send_html(
        context.bot,
        user_id,
        "Введите <b>номер законопроекта</b> (например: 1124826-8):",
        reply_markup=add_step1_kb(),
    )
    return ADD_NUM


async def add_entry_from_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id

    context.user_data.pop("pending_bill_number", None)
    context.user_data.pop("pending_official_title", None)

    await safe_send_html(
        context.bot,
        user_id,
        "Введите <b>номер законопроекта</b> (например: 1124826-8):",
        reply_markup=add_step1_kb(),
    )
    return ADD_NUM


async def add_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    raw = update.effective_message.text or ""
    bill_number = normalize_bill_number(raw)

    if not bill_number:
        await safe_send_html(
            context.bot,
            user_id,
            "Не понял номер.\n"
            "Нужен формат <b>цифры-дефис-цифра</b>, например: <b>1124826-8</b>.\n\n"
            "Попробуйте ещё раз:",
            reply_markup=add_step1_kb(),
        )
        return ADD_NUM

    if db_user_has_bill(user_id, bill_number):
        await safe_send_html(
            context.bot,
            user_id,
            f"⚠️ Законопроект <b>{h(bill_number)}</b> уже есть в вашем списке.",
            reply_markup=inline_main_menu_kb(),
        )
        return ConversationHandler.END

    official_title = await fetch_official_title(bill_number)
    if not official_title:
        await safe_send_html(
            context.bot,
            user_id,
            f"❌ Законопроект <b>{h(bill_number)}</b> не найден. Введите номер ещё раз:",
            reply_markup=add_step1_kb(),
        )
        return ADD_NUM

    context.user_data["pending_bill_number"] = bill_number
    context.user_data["pending_official_title"] = official_title

    txt = (
        f"✅ Номер: <b>{h(bill_number)}</b>\n\n"
        f"📄 Официальное наименование:\n{h(quote_title(official_title))}\n\n"
        "Введите своё короткое название (или отправьте '-' для официального)."
    )
    await safe_send_html(context.bot, user_id, txt, reply_markup=add_step2_kb(bill_number))
    return ADD_TITLE


async def keep_official_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.message.chat_id

    bill_number = context.user_data.get("pending_bill_number")
    official_title = context.user_data.get("pending_official_title")
    if not bill_number or not official_title:
        await safe_send_html(context.bot, user_id, "Сессия потеряна. Нажмите /add.", reply_markup=inline_main_menu_kb())
        return ConversationHandler.END

    custom_title = official_title
    inserted = db_add_bill(user_id, bill_number, official_title, custom_title)
    if not inserted:
        await safe_send_html(context.bot, user_id, "Уже было добавлено.", reply_markup=inline_main_menu_kb())
        return ConversationHandler.END

    # Сообщение 1: карточка + текущий статус (как раньше)
    event = await get_latest_rss_event(bill_number)
    deadlines = await extract_deadlines(bill_number)
    if event:
        event_hash = make_event_hash(event)
        message = (
            f"🆕 Законопроект № {bill_link(bill_number)} {h(quote_title(custom_title))} добавлен в мониторинг.\n\n"
            f"Текущий статус:\n{format_status(event)}{format_deadlines(deadlines)}"
        )
        await safe_send_html(context.bot, user_id, message, reply_markup=inline_main_menu_kb())
        db_set_user_last_event(user_id, bill_number, event_hash, event)

    # Сообщение 2: “Добавлено … / Ваши проекты: N” (как раньше)
    bills = db_get_user_bills(user_id)
    await safe_send_html(
        context.bot,
        user_id,
        f"✅ Добавлено: № {bill_link(bill_number)} {h(quote_title(custom_title))}\n\n"
        f"Ваши проекты: {len(bills)}",
        reply_markup=inline_main_menu_kb(),
    )
    return ConversationHandler.END


async def add_title_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    bill_number = context.user_data.get("pending_bill_number")
    official_title = context.user_data.get("pending_official_title")

    if not bill_number or not official_title:
        await safe_send_html(context.bot, user_id, "Сессия потеряна. Нажмите /add.", reply_markup=inline_main_menu_kb())
        return ConversationHandler.END

    custom = (update.effective_message.text or "").strip()
    if custom == "-" or not custom:
        custom = official_title

    inserted = db_add_bill(user_id, bill_number, official_title, custom)
    if not inserted:
        await safe_send_html(context.bot, user_id, "Уже было добавлено.", reply_markup=inline_main_menu_kb())
        return ConversationHandler.END

    # Сообщение 1: карточка + текущий статус (как раньше)
    event = await get_latest_rss_event(bill_number)
    deadlines = await extract_deadlines(bill_number)
    if event:
        event_hash = make_event_hash(event)
        message = (
            f"🆕 Законопроект № {bill_link(bill_number)} {h(quote_title(custom))} добавлен в мониторинг.\n\n"
            f"Текущий статус:\n{format_status(event)}{format_deadlines(deadlines)}"
        )
        await safe_send_html(context.bot, user_id, message, reply_markup=inline_main_menu_kb())
        db_set_user_last_event(user_id, bill_number, event_hash, event)

    # Сообщение 2: “Добавлено … / Ваши проекты: N” (как раньше)
    bills = db_get_user_bills(user_id)
    await safe_send_html(
        context.bot,
        user_id,
        f"✅ Добавлено: № {bill_link(bill_number)} {h(quote_title(custom))}\n\n"
        f"Ваши проекты: {len(bills)}",
        reply_markup=inline_main_menu_kb(),
    )
    return ConversationHandler.END


# ================== Monitor ==================
async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    bill_numbers = db_get_distinct_bill_numbers()
    if not bill_numbers:
        return

    for bill_number in bill_numbers:
        event = await get_latest_rss_event(bill_number)
        if not event:
            continue

        deadlines = await extract_deadlines(bill_number)
        event_hash = make_event_hash(event)
        users = db_get_users_tracking_bill(bill_number)

        for row in users:
            user_id = int(row["user_id"])
            custom_title = row["custom_title"]

            last = db_get_user_last_event(user_id, bill_number)
            last_hash = last["hash"] if last else None
            last_event = last["event"] if last else None

            if last_hash == event_hash:
                continue

            if not last_event:
                message = (
                    f"📢 Новое событие по законопроекту № {bill_link(bill_number)} {h(quote_title(custom_title))}\n\n"
                    f"Текущий статус:\n{format_status(event)}{format_deadlines(deadlines)}"
                )
            else:
                message = (
                    f"📢 Новое событие по законопроекту № {bill_link(bill_number)} {h(quote_title(custom_title))}\n\n"
                    f"Предыдущий статус:\n{format_status(last_event)}\n\n"
                    f"Текущий статус:\n{format_status(event)}{format_deadlines(deadlines)}"
                )

            await safe_send_html(bot, user_id, message, reply_markup=None)
            db_set_user_last_event(user_id, bill_number, event_hash, event)

        await asyncio.sleep(0.3)


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    txt = (update.effective_message.text or "").strip()
    if not txt:
        return
    await safe_send_html(context.bot, user_id, "Используйте /menu или кнопки.", reply_markup=inline_main_menu_kb())


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling update", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пустой. Проверьте .env рядом с bot.py")

    db_init()
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_error_handler(on_error)

    # commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("my", cmd_my))
    application.add_handler(CommandHandler("add", add_entry_from_cmd))

    # add conversation
    conv_add = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_entry_from_inline, pattern="^add$"),
            CommandHandler("add", add_entry_from_cmd),
        ],
        states={
            ADD_NUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_number_input),
                CallbackQueryHandler(menu_callback, pattern="^cancel$"),
            ],
            ADD_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_title_input),
                CallbackQueryHandler(keep_official_cb, pattern="^keep_official$"),
                CallbackQueryHandler(menu_callback, pattern="^cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(menu_callback, pattern="^(cancel|back)$")],
    )
    application.add_handler(conv_add)

    application.add_handler(CallbackQueryHandler(menu_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    if application.job_queue is None:
        raise RuntimeError('JobQueue не доступен. Установите: pip install "python-telegram-bot[job-queue]"')

    application.job_queue.run_repeating(monitor_job, interval=CHECK_INTERVAL, first=10)

    print("Бот запущен. Ctrl+C для остановки.")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

