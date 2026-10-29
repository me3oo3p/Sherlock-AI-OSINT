
import os
import re
import html
import json
import uuid
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==================================================
# CONFIG
# ==================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "sherlock_reports.db")
PORT = int(os.getenv("PORT", "8080"))

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

# ==================================================
# DATABASE
# ==================================================

def db():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
                query TEXT,
                category TEXT,
                results TEXT,
                created_at TEXT
            )
        """)


def save_report(user_id, query, category, results):
    report_id = uuid.uuid4().hex[:10]
    created = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        conn.execute("""
            INSERT INTO reports
            (id, user_id, query, category, results, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            report_id,
            user_id,
            query,
            category,
            json.dumps(results, ensure_ascii=False),
            created,
        ))

    return report_id


def get_report(report_id, user_id):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM reports
            WHERE id = ? AND user_id = ?
        """, (report_id, user_id)).fetchone()


# ==================================================
# CLASSIFICATION
# ==================================================

def classify_query(query):
    value = query.strip()

    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return "Email"

    if value.startswith("@"):
        return "Username"

    if re.fullmatch(r"https?://\S+|www\.\S+", value, re.I):
        return "URL"

    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value):
        return "IP"

    if re.fullmatch(r"\+?[\d\s()\-]{7,20}", value):
        return "Телефон"

    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value.upper()):
        return "VIN"

    if re.fullmatch(r"\d{10}|\d{12}|\d{13}", value):
        return "Возможный номер реестра"

    return "Общий поиск"


# ==================================================
# PUBLIC WEB SEARCH
# ==================================================

def search_public_web(query, limit=7):
    """
    Searches publicly indexed web pages.
    Search engines may block requests; that is reported explicitly.
    """
    url = "https://html.duckduckgo.com/html/"
    results = []

    try:
        response = HTTP.get(
            url,
            params={"q": query},
            timeout=12,
        )

        if response.status_code != 200:
            log.warning("Search engine returned HTTP %s",
                        response.status_code)
            return []

        soup = BeautifulSoup(response.text, "html.parser")

        for item in soup.select(".result"):
            title_node = item.select_one(".result__a")
            snippet_node = item.select_one(".result__snippet")

            if not title_node:
                continue

            title = title_node.get_text(" ", strip=True)
            link = title_node.get("href", "").strip()
            snippet = (
                snippet_node.get_text(" ", strip=True)
                if snippet_node else ""
            )

            if not link.startswith("http"):
                continue

            if not title:
                continue

            results.append({
                "title": title[:250],
                "url": link[:1500],
                "snippet": snippet[:600],
            })

            if len(results) >= limit:
                break

    except requests.RequestException as exc:
        log.warning("Public search failed: %s", exc)

    except Exception:
        log.exception("Unexpected search error")

    return results


def make_search_links(query):
    q = quote_plus(query)
    return [
        {
            "title": "Открыть поиск Google",
            "url": f"https://www.google.com/search?q={q}",
            "snippet": "Откроет результаты поиска в браузере.",
        },
        {
            "title": "Открыть поиск Bing",
            "url": f"https://www.bing.com/search?q={q}",
            "snippet": "Альтернативный поисковик.",
        },
        {
            "title": "Открыть поиск DuckDuckGo",
            "url": f"https://duckduckgo.com/?q={q}",
            "snippet": "Альтернативный поисковик.",
        },
    ]


def build_queries(query, category):
    value = query.strip()
    clean = value.removeprefix("@").strip()

    if category == "Username":
        return [
            f'"{clean}"',
            f'site:vk.com "{clean}"',
            f'site:t.me "{clean}"',
            f'site:instagram.com "{clean}"',
            f'site:tiktok.com "{clean}"',
        ]

    if category == "Email":
        return [
            f'"{value}"',
        ]

    if category == "Телефон":
        digits = re.sub(r"\D", "", value)
        # Не перебираем варианты номера и не связываем его
        # автоматически с частными лицами.
        return [f'"{digits}"']

    if category == "IP":
        return [
            f'"{value}"',
            f'"{value}" domain',
        ]

    if category == "VIN":
        return [
            f'"{value.upper()}"',
        ]

    if category == "URL":
        parsed = urlparse(
            value if value.startswith(("http://", "https://"))
            else "https://" + value
        )
        domain = parsed.hostname or value
        return [
            f'"{domain}"',
            f'site:{domain}',
        ]

    return [f'"{value}"', value]


# ==================================================
# REPORT FORMATTING
# ==================================================

def make_report_text(query, category, results, report_id):
    safe_query = html.escape(query)
    safe_category = html.escape(category)

    lines = [
        "🕵️ <b>SHERLOCK AI OSINT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🔎 <b>Запрос:</b> <code>{safe_query}</code>",
        f"📂 <b>Тип:</b> {safe_category}",
        f"📊 <b>Результатов:</b> {len(results)}",
        "",
    ]

    if results:
        lines.append("<b>📚 Найденные публичные источники</b>")
        lines.append("")

        for index, result in enumerate(results[:10], start=1):
            title = html.escape(result.get("title", "Без названия"))
            link = html.escape(result.get("url", ""), quote=True)
            snippet = html.escape(result.get("snippet", ""))

            lines.append(f"<b>{index}. {title}</b>")
            lines.append(f'🔗 <a href="{link}">Открыть источник</a>')

            if snippet:
                lines.append(snippet[:500])

            lines.append("")
    else:
        lines.extend([
            "⚠️ <b>Поиск не вернул результатов.</b>",
            "",
            "Возможные причины:",
            "• поисковик ограничил автоматические запросы;",
            "• страницы не проиндексированы;",
            "• запрос слишком общий;",
            "",
            "Ниже доступны прямые ссылки на поисковики.",
            "",
        ])

        for item in make_search_links(query):
            title = html.escape(item["title"])
            link = html.escape(item["url"], quote=True)
            lines.append(f'🌐 <a href="{link}">{title}</a>')

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"🧾 <b>Отчёт:</b> <code>{report_id}</code>",
        "",
        "ℹ️ Совпадение имени или username не доказывает, "
        "что найденный профиль принадлежит искомому человеку.",
        "Проверяйте каждый результат по первоисточнику.",
    ])

    return "\n".join(lines)


# ==================================================
# TELEGRAM MENU
# ==================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔎 Поиск", callback_data="help_search"),
            InlineKeyboardButton("🌐 Домен / IP", callback_data="help_network"),
        ],
        [
            InlineKeyboardButton("💬 Username", callback_data="help_username"),
            InlineKeyboardButton("🏢 Компании", callback_data="help_company"),
        ],
        [
            InlineKeyboardButton("📄 Мои отчёты", callback_data="my_reports"),
            InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
        ],
    ])


START_TEXT = """
🕵️ <b>SHERLOCK AI OSINT</b>
<i>Публичный поиск и анализ открытых источников</i>

━━━━━━━━━━━━━━━━━━

👤 <b>Публичные упоминания</b>
ФИО, username, никнеймы и ключевые слова.

📲 <b>Контакты</b>
Поиск открыто опубликованных упоминаний email и телефона.

💬 <b>Социальные сети</b>
VK, Telegram, TikTok, Instagram и Одноклассники.

🌐 <b>Интернет-инфраструктура</b>
Домены, IP и публичные технические сведения.

🚘 <b>Транспорт</b>
Поиск публичных упоминаний VIN и автомобилей.

🏢 <b>Юридические лица</b>
Поиск открытых сведений о компаниях и ИП.

━━━━━━━━━━━━━━━━━━

<b>Как пользоваться:</b>
Отправь запрос обычным сообщением или используй:

/search запрос — общий поиск
/tag ключевые слова — поиск по теме
/inn номер — поиск по открытым реестрам компаний
/domain example.com — поиск по домену
/ip 192.0.2.1 — поиск публичных упоминаний IP
/report ID — открыть сохранённый отчёт
/status — состояние бота
/clear — удалить свои отчёты

<i>Бот не подтверждает личность только по совпадению имени
и не ищет закрытые персональные данные.</i>
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        START_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        START_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
        disable_web_page_preview=True,
    )


async def menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    messages = {
        "help_search": (
            "🔎 <b>Общий поиск</b>\n"
            "Пример: <code>/search название проекта</code>"
        ),
        "help_network": (
            "🌐 <b>Домен и IP</b>\n"
            "<code>/domain example.com</code>\n"
            "<code>/ip 192.0.2.1</code>"
        ),
        "help_username": (
            "💬 <b>Поиск username</b>\n"
            "Пример: <code>/search @username</code>\n"
            "Ищем публичные страницы и упоминания."
        ),
        "help_company": (
            "🏢 <b>Поиск компаний</b>\n"
            "Пример: <code>/inn 7707083893</code>\n"
            "Используй номер юридического лица или ИП."
        ),
        "help": START_TEXT,
    }

    if query.data == "my_reports":
        await query.edit_message_text(
            "📄 Чтобы открыть отчёт, отправь:\n"
            "<code>/report ID</code>\n\n"
            "ID указан в конце каждого отчёта.",
            parse_mode=ParseMode.HTML,
        )
        return

    await query.edit_message_text(
        messages.get(query.data, START_TEXT),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
        disable_web_page_preview=True,
    )


# ==================================================
# SEARCH HANDLER
# ==================================================

async def perform_search(update, context, query_text, forced_category=None):
    query_text = query_text.strip()

    if not query_text:
        await update.effective_message.reply_text(
            "Укажи запрос после команды.\n"
            "Пример: /search @username"
        )
        return

    if len(query_text) > 250:
        await update.effective_message.reply_text(
            "⚠️ Запрос слишком длинный. Максимум 250 символов."
        )
        return

    # Не принимаем запросы, явно направленные на поиск
    # закрытых документов или идентификацию по лицу.
    forbidden_patterns = [
        r"\bпаспорт\b",
        r"\bснилс\b",
        r"\bводительск\w*\s+удостоверен",
        r"\bнайди\s+человека\s+по\s+лицу\b",
        r"\bтелефонн\w*\s+книг\w*",
        r"\bутечк\w*\s+баз\w*",
        r"\bбанковск\w*\s+клиент\w*",
    ]

    if any(re.search(p, query_text, re.I) for p in forbidden_patterns):
        await update.effective_message.reply_text(
            "⛔ Этот запрос направлен на поиск закрытых "
            "персональных данных. Я могу искать публичные страницы, "
            "открытые публикации и официальные реестры."
        )
        return

    category = forced_category or classify_query(query_text)

    status_message = await update.effective_message.reply_text(
        "🔎 Ищу публичные результаты…"
    )

    collected = []
    seen_urls = set()
    search_errors = False

    for search_query in build_queries(query_text, category):
        results = await __import__("asyncio").to_thread(
            search_public_web, search_query, 7
        )

        if not results:
            search_errors = True

        for result in results:
            url = result["url"]

            if url not in seen_urls:
                seen_urls.add(url)
                collected.append(result)

            if len(collected) >= 10:
                break

        if len(collected) >= 10:
            break

    report_id = save_report(
        update.effective_user.id,
        query_text,
        category,
        collected,
    )

    report_text = make_report_text(
        query_text,
        category,
        collected,
        report_id,
    )

    if search_errors and not collected:
        report_text += (
            "\n\n⚠️ Автоматический поиск мог быть ограничен "
            "поисковиком. Используй прямые ссылки выше."
        )

    try:
        await status_message.edit_text(
            report_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Could not edit report message")
        await update.effective_message.reply_text(
            "Не удалось отобразить отчёт. Попробуй более короткий запрос."
        )


async def search_command(update, context):
    await perform_search(
        update,
        context,
        " ".join(context.args),
    )


async def tag_command(update, context):
    await perform_search(
        update,
        context,
        " ".join(context.args),
        forced_category="Поиск по ключевым словам",
    )


async def inn_command(update, context):
    value = " ".join(context.args).strip()

    if not value:
        await update.effective_message.reply_text(
            "Пример: /inn 7707083893\n"
            "Используй ИНН компании или ИП."
        )
        return

    if not re.fullmatch(r"\d{10}|\d{12}", value):
        await update.effective_message.reply_text(
            "Для поиска компании укажи ИНН из 10 или 12 цифр."
        )
        return

    await perform_search(
        update,
        context,
        f'"{value}" компания ИНН официальный реестр',
        forced_category="Юридическое лицо",
    )


async def domain_command(update, context):
    value = " ".join(context.args).strip()

    if not value:
        await update.effective_message.reply_text(
            "Пример: /domain example.com"
        )
        return

    await perform_search(
        update,
        context,
        value,
        forced_category="Домен",
    )


async def ip_command(update, context):
    value = " ".join(context.args).strip()

    if not value:
        await update.effective_message.reply_text(
            "Пример: /ip 192.0.2.1"
        )
        return

    await perform_search(
        update,
        context,
        value,
        forced_category="IP",
    )


async def report_command(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Пример: /report ID_ОТЧЁТА"
        )
        return

    row = get_report(
        context.args[0].strip(),
        update.effective_user.id,
    )

    if not row:
        await update.effective_message.reply_text(
            "Отчёт не найден или принадлежит другому пользователю."
        )
        return

    results = json.loads(row["results"])
    text = make_report_text(
        row["query"],
        row["category"],
        results,
        row["id"],
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def clear_command(update, context):
    with db() as conn:
        conn.execute(
            "DELETE FROM reports WHERE user_id = ?",
            (update.effective_user.id,),
        )

    await update.effective_message.reply_text(
        "🗑️ Твои сохранённые отчёты удалены."
    )


async def status_command(update, context):
    await update.effective_message.reply_text(
        "🟢 <b>Sherlock AI OSINT</b>\n"
        "Бот работает.\n"
        "Режим: поиск по открытым источникам.\n"
        "Хранилище отчётов: SQLite.",
        parse_mode=ParseMode.HTML,
    )


async def text_handler(update, context):
    text_value = update.effective_message.text or ""
    await perform_search(update, context, text_value)


async def error_handler(update, context):
    log.error(
        "Telegram handler error",
        exc_info=context.error,
    )


# ==================================================
# HEALTH CHECK
# ==================================================

@app.get("/")
def index():
    return (
        "Sherlock AI OSINT is running.",
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
    )


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "Sherlock AI OSINT",
        "time": datetime.now(timezone.utc).isoformat(),
    })


def run_web():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


# ==================================================
# START
# ==================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. "
            "Добавь токен в переменные окружения хостинга."
        )

    init_db()

    threading.Thread(
        target=run_web,
        daemon=True,
    ).start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("tag", tag_command))
    application.add_handler(CommandHandler("inn", inn_command))
    application.add_handler(CommandHandler("domain", domain_command))
    application.add_handler(CommandHandler("ip", ip_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(menu_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_error_handler(error_handler)

    log.info("Sherlock AI OSINT starting")
    application.run_polling()


if __name__ == "__main__":
    main()
