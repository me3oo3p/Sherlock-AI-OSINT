
import os
import re
import html
import sqlite3
import logging
import ipaddress
import socket
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import requests
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================= НАСТРОЙКИ =================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DB_FILE = "reports.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("sherlock")

app = Flask(__name__)
HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
    )
})


# ================= БАЗА ДАННЫХ =================

def init_db():
    with sqlite3.connect(DB_FILE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                created_at TEXT NOT NULL,
                result TEXT NOT NULL
            )
        """)


def save_report(user_id, query, result):
    with sqlite3.connect(DB_FILE) as db:
        cur = db.execute(
            "INSERT INTO reports (user_id, query, created_at, result) "
            "VALUES (?, ?, ?, ?)",
            (
                user_id,
                query,
                datetime.now(timezone.utc).isoformat(),
                result,
            ),
        )
        return cur.lastrowid


def get_report(user_id, report_id):
    with sqlite3.connect(DB_FILE) as db:
        return db.execute(
            "SELECT query, created_at, result FROM reports "
            "WHERE id = ? AND user_id = ?",
            (report_id, user_id),
        ).fetchone()


def clear_reports(user_id):
    with sqlite3.connect(DB_FILE) as db:
        db.execute("DELETE FROM reports WHERE user_id = ?", (user_id,))


# ================= ОТКРЫТЫЙ ПОИСК =================

def public_search(query, limit=7):
    """Ищет страницы, опубликованные в открытом интернете."""
    url = "https://html.duckduckgo.com/html/"
    try:
        response = HTTP.post(
            url,
            data={"q": query},
            timeout=12,
        )
        response.raise_for_status()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        for item in soup.select(".result"):
            link = item.select_one(".result__a")
            snippet = item.select_one(".result__snippet")

            if not link:
                continue

            href = link.get("href", "")
            title = link.get_text(" ", strip=True)
            description = (
                snippet.get_text(" ", strip=True) if snippet else ""
            )

            if not href.startswith(("http://", "https://")):
                continue

            results.append({
                "title": title[:250],
                "url": href,
                "description": description[:500],
            })

            if len(results) >= limit:
                break

        return results

    except requests.RequestException:
        log.warning("Public search request failed")
        return []
    except Exception:
        log.exception("Search parsing failed")
        return []


def format_results(query, results, report_id):
    lines = [
        "🕵️ <b>SHERLOCK AI OSINT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🔎 Запрос: <code>{html.escape(query)}</code>",
        "🌐 Тип: публичный поиск",
        f"📊 Результатов: {len(results)}",
        "",
    ]

    if results:
        for i, item in enumerate(results, 1):
            title = html.escape(item["title"])
            description = html.escape(item["description"])
            url = html.escape(item["url"], quote=True)

            lines.append(f"🔗 <b>{i}. {title}</b>")
            if description:
                lines.append(description)
            lines.append(f'<a href="{url}">Открыть источник</a>')
            lines.append("")
    else:
        lines.extend([
            "😕 Публичных результатов не найдено.",
            "Возможные причины:",
            "├ Страница не индексируется поисковиком.",
            "├ Поисковик временно ограничил запросы.",
            "└ Попробуй изменить запрос.",
            "",
        ])

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        f"📄 Отчёт: <code>#{report_id}</code>",
        "ℹ️ Результаты поиска не подтверждают личность человека.",
    ])
    return "\n".join(lines)


# ================= TELEGRAM =================

START_TEXT = (
    "🕵️ <b>SHERLOCK AI OSINT</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🔍 Поиск по открытым источникам\n\n"
    "ℹ️ <b>Примеры команд</b>\n\n"
    "💬 <b>Общий поиск:</b>\n"
    "<code>/search игровой сервер Rust</code>\n\n"
    "🔎 <b>Username:</b>\n"
    "<code>/search @username</code>\n\n"
    "📲 <b>Публичные упоминания телефона:</b>\n"
    "<code>/search +79990000000</code>\n\n"
    "🏷 <b>Поиск по словам:</b>\n"
    "<code>/tag профессия город</code>\n\n"
    "🌐 <b>Домен:</b>\n"
    "<code>/domain example.com</code>\n\n"
    "🖥 <b>IP-адрес:</b>\n"
    "<code>/ip 8.8.8.8</code>\n\n"
    "🏢 <b>Организация / ИНН юрлица:</b>\n"
    "<code>/inn 7707083893</code>\n\n"
    "📄 <b>Отчёт:</b>\n"
    "<code>/report 1</code>\n\n"
    "🗑 <b>Удалить свои отчёты:</b>\n"
    "<code>/clear</code>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Работа только с открытыми источниками."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🔎 Поиск", callback_data="help_search"),
            InlineKeyboardButton("🌐 Домен", callback_data="help_domain"),
        ],
        [
            InlineKeyboardButton("🖥 IP", callback_data="help_ip"),
            InlineKeyboardButton("📄 Отчёты", callback_data="help_report"),
        ],
    ]
    await update.effective_message.reply_text(
        START_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        START_TEXT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    texts = {
        "help_search": "🔎 Используй: /search твой запрос",
        "help_domain": "🌐 Используй: /domain example.com",
        "help_ip": "🖥 Используй: /ip 8.8.8.8",
        "help_report": "📄 Используй: /report номер_отчёта",
    }
    await query.message.reply_text(texts.get(query.data, START_TEXT))


async def run_search(update, query_text):
    query_text = query_text.strip()

    if not query_text:
        await update.effective_message.reply_text(
            "Укажи запрос.\nПример: /search @username"
        )
        return

    if len(query_text) > 180:
        await update.effective_message.reply_text("Запрос слишком длинный.")
        return

    results = public_search(query_text)
    report_id = save_report(
        update.effective_user.id,
        query_text,
        str(len(results)),
    )
    message = format_results(query_text, results, report_id)

    await update.effective_message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_search(update, " ".join(context.args))


async def tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.effective_message.reply_text(
            "Пример: /tag профессия город"
        )
        return
    await run_search(update, query)


async def domain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Пример: /domain example.com"
        )
        return

    domain = context.args[0].strip().lower()
    if "://" in domain:
        domain = urlparse(domain).hostname or ""

    if (
        not domain
        or len(domain) > 253
        or not re.fullmatch(r"[a-z0-9.-]+", domain)
        or "." not in domain
        or ".." in domain
    ):
        await update.effective_message.reply_text("Некорректный домен.")
        return

    lines = [
        "🌐 <b>АНАЛИЗ ДОМЕНА</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"├ Домен: <code>{html.escape(domain)}</code>",
    ]

    try:
        addresses = sorted({
            item[4][0]
            for item in socket.getaddrinfo(domain, None)
        })
        lines.append("├ IP-адреса:")
        for address in addresses[:8]:
            lines.append(f"│ • <code>{html.escape(address)}</code>")
    except (socket.gaierror, OSError):
        lines.append("├ DNS: адрес не найден")

    lines.extend([
        "└ Поиск упоминаний:",
        "",
    ])

    results = public_search(f'site:{domain} OR "{domain}"', limit=5)
    for i, item in enumerate(results, 1):
        lines.append(
            f'{i}. <a href="{html.escape(item["url"], quote=True)}">'
            f'{html.escape(item["title"])}</a>'
        )

    if not results:
        lines.append("Публичные страницы не найдены.")

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def ip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Пример: /ip 8.8.8.8"
        )
        return

    try:
        address = ipaddress.ip_address(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Некорректный IP-адрес.")
        return

    lines = [
        "🖥 <b>АНАЛИЗ IP</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"├ IP: <code>{address}</code>",
        f"├ Версия: IPv{address.version}",
        f"├ Глобальный адрес: {'да' if address.is_global else 'нет'}",
    ]

    try:
        hostname = socket.gethostbyaddr(str(address))[0]
        lines.append(f"└ Обратный DNS: <code>{html.escape(hostname)}</code>")
    except (socket.herror, socket.gaierror, OSError):
        lines.append("└ Обратный DNS: не найден")

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def inn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Укажи ИНН организации.\nПример: /inn 7707083893"
        )
        return

    inn = context.args[0].strip()
    if not re.fullmatch(r"\d{10}|\d{12}", inn):
        await update.effective_message.reply_text(
            "ИНН должен содержать 10 или 12 цифр."
        )
        return

    # Поиск только открытых сведений об организациях.
    results = public_search(f'"{inn}" организация ИНН', limit=6)
    lines = [
        "🏢 <b>ПОИСК ПО ИНН</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"├ ИНН: <code>{html.escape(inn)}</code>",
        "├ Источник: открытый интернет",
        "",
    ]

    if results:
        for i, item in enumerate(results, 1):
            lines.append(
                f'{i}. <a href="{html.escape(item["url"], quote=True)}">'
                f'{html.escape(item["title"])}</a>'
            )
            if item["description"]:
                lines.append(html.escape(item["description"]))
            lines.append("")
    else:
        lines.append("└ Публичные результаты не найдены.")

    lines.append("ℹ️ Результаты требуют проверки по официальным реестрам.")

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Пример: /report 1"
        )
        return

    record = get_report(
        update.effective_user.id,
        int(context.args[0]),
    )

    if not record:
        await update.effective_message.reply_text(
            "Отчёт не найден. Проверь номер."
        )
        return

    query, created_at, count = record
    await update.effective_message.reply_text(
        "📄 <b>СОХРАНЁННЫЙ ОТЧЁТ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"├ Запрос: <code>{html.escape(query)}</code>\n"
        f"├ Дата: <code>{html.escape(created_at)}</code>\n"
        f"└ Результатов при поиске: <code>{html.escape(count)}</code>\n\n"
        "Для просмотра актуальных страниц запусти /search ещё раз.",
        parse_mode=ParseMode.HTML,
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_reports(update.effective_user.id)
    await update.effective_message.reply_text(
        "🗑 Твои сохранённые отчёты удалены."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🟢 <b>SHERLOCK AI OSINT</b>\n"
        "├ Бот: работает\n"
        "├ Поиск: открытые источники\n"
        "└ База отчётов: подключена",
        parse_mode=ParseMode.HTML,
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if text and not text.startswith("/"):
        await run_search(update, text)


# ================= ВЕБ-СТАТУС =================

@app.get("/")
def index():
    return "Sherlock AI OSINT is running"


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Sherlock AI OSINT"})


# ================= ЗАПУСК =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. "
            "Добавь токен в переменные окружения хостинга."
        )

    init_db()

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("search", search_command))
    telegram_app.add_handler(CommandHandler("tag", tag_command))
    telegram_app.add_handler(CommandHandler("domain", domain_command))
    telegram_app.add_handler(CommandHandler("ip", ip_command))
    telegram_app.add_handler(CommandHandler("inn", inn_command))
    telegram_app.add_handler(CommandHandler("report", report_command))
    telegram_app.add_handler(CommandHandler("clear", clear_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    log.info("Sherlock AI OSINT starting")

    # Flask и Telegram polling работают в одном процессе.
    from threading import Thread
    Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=PORT,
            use_reloader=False,
        ),
        daemon=True,
    ).start()

    telegram_app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
