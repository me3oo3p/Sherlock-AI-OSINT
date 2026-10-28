
import os
import re
import html
import json
import time
import uuid
import sqlite3
import threading
import ipaddress
from urllib.parse import quote_plus, urlparse

import requests
import phonenumbers
from flask import Flask, jsonify, Response
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "sherlock.db")
REPORT_BASE_URL = os.getenv("REPORT_BASE_URL", "").rstrip("/")

app = Flask(__name__)
DB_LOCK = threading.Lock()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    )
}


def db_connect():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with DB_LOCK:
        with db_connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    query_type TEXT NOT NULL,
                    results TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            db.commit()


def classify_query(query):
    value = query.strip()

    if not value:
        return "empty"

    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return "email"

    if re.fullmatch(r"@[A-Za-z0-9_]{3,32}", value):
        return "username"

    if re.fullmatch(r"https?://\S+|www\.\S+", value, re.I):
        return "url"

    candidate = value.strip(" .")
    try:
        ipaddress.ip_address(candidate)
        return "ip"
    except ValueError:
        pass

    compact = re.sub(r"[\s()+.-]", "", value)
    if re.fullmatch(r"\+?\d{10,15}", compact):
        return "phone"

    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value.upper()):
        return "vin"

    if re.fullmatch(r"\d{10}|\d{12}|\d{13}|\d{15}", compact):
        return "company_number"

    return "general"


def phone_details(value):
    try:
        number = phonenumbers.parse(value, None)
        valid = phonenumbers.is_valid_number(number)
        possible = phonenumbers.is_possible_number(number)

        return {
            "formatted": phonenumbers.format_number(
                number, phonenumbers.PhoneNumberFormat.INTERNATIONAL
            ),
            "country_code": number.country_code,
            "region": phonenumbers.region_code_for_number(number) or "Не определён",
            "valid": valid,
            "possible": possible,
            "note": (
                "Регион и код не гарантируют текущего оператора или местоположение. "
                "Номер мог быть перенесён к другому оператору."
            ),
        }
    except Exception:
        return {
            "note": (
                "Не удалось разобрать номер. Попробуйте международный формат, "
                "например +79991234567."
            )
        }


def extract_results(query, query_type):
    """
    Ищет только общедоступные страницы.
    Поисковик может ограничивать запросы, поэтому отсутствие результатов
    не означает, что упоминаний в интернете нет.
    """
    queries = [query]

    if query_type == "phone":
        digits = re.sub(r"\D", "", query)
        queries = list(dict.fromkeys([
            query,
            digits,
            "+" + digits if digits else query,
        ]))

    elif query_type == "email":
        queries = [f'"{query}"']

    elif query_type == "username":
        username = query.lstrip("@")
        queries = [
            f'"{username}"',
            f'"@{username}"',
        ]

    elif query_type == "url":
        parsed = urlparse(
            query if query.lower().startswith(("http://", "https://"))
            else "https://" + query
        )
        domain = parsed.netloc or parsed.path
        queries = [domain, f'"{domain}"']

    elif query_type == "ip":
        queries = [query, f'"{query}"']

    elif query_type == "vin":
        queries = [query.upper(), f'"{query.upper()}"']

    elif query_type == "company_number":
        queries = [query, f'"{query}"']

    collected = []
    seen = set()
    failures = []

    for search_term in queries[:3]:
        try:
            response = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": search_term},
                headers=HEADERS,
                timeout=12,
            )

            if response.status_code != 200:
                failures.append(
                    f"Поисковик вернул HTTP {response.status_code}"
                )
                continue

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, "html.parser")

            blocks = soup.select(".result")
            for block in blocks:
                link = block.select_one(".result__a")
                snippet = block.select_one(".result__snippet")

                if not link:
                    continue

                title = link.get_text(" ", strip=True)
                href = link.get("href", "")
                description = (
                    snippet.get_text(" ", strip=True)
                    if snippet else ""
                )

                if not href.startswith(("http://", "https://")):
                    continue

                key = href.split("#", 1)[0]
                if key in seen:
                    continue

                seen.add(key)
                collected.append({
                    "title": title[:300],
                    "url": href[:1500],
                    "snippet": description[:1000],
                })

            time.sleep(1)

        except requests.RequestException as exc:
            failures.append(type(exc).__name__)
        except Exception as exc:
            failures.append(type(exc).__name__)

    status = "ok" if collected else (
        "blocked" if failures else "no_results"
    )

    return {
        "status": status,
        "items": collected[:20],
        "message": (
            "Найдены публичные страницы."
            if collected else
            "Поисковик мог ограничить запрос или не вернуть подходящие "
            "страницы. Это не доказывает отсутствие информации."
        ),
        "diagnostics": list(dict.fromkeys(failures))[:3],
    }


def save_report(query, query_type, result):
    report_id = uuid.uuid4().hex[:12]
    payload = json.dumps(result, ensure_ascii=False)

    with DB_LOCK:
        with db_connect() as db:
            db.execute(
                """
                INSERT INTO reports
                (id, query, query_type, results, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (report_id, query, query_type, payload, int(time.time())),
            )
            db.commit()

    return report_id


def get_report(report_id):
    with DB_LOCK:
        with db_connect() as db:
            row = db.execute(
                "SELECT * FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()

    if not row:
        return None

    return {
        "id": row["id"],
        "query": row["query"],
        "query_type": row["query_type"],
        "results": json.loads(row["results"]),
        "created_at": row["created_at"],
    }


def format_report(report_id, query, query_type, result):
    lines = [
        "🔎 Sherlock AI OSINT",
        "",
        f"Запрос: {query}",
        f"Тип: {query_type}",
        f"Статус: {result['status']}",
        "",
    ]

    if query_type == "phone":
        details = phone_details(query)
        lines.extend([
            "📱 Информация о формате номера:",
            f"Международный формат: {details.get('formatted', 'не определён')}",
            f"Код страны: {details.get('country_code', 'не определён')}",
            f"Регион: {details.get('region', 'не определён')}",
            f"Валидный формат: {details.get('valid', 'не определён')}",
            "",
            details.get("note", ""),
            "",
        ])

    items = result.get("items", [])
    if items:
        lines.append("🌐 Публичные результаты:")
        for index, item in enumerate(items[:10], start=1):
            lines.append(f"{index}. {item['title']}")
            lines.append(item["url"])
            if item.get("snippet"):
                lines.append(item["snippet"])
            lines.append("")
    else:
        lines.append(result.get("message", "Результаты не найдены."))
        diagnostics = result.get("diagnostics", [])
        if diagnostics:
            lines.append("Диагностика: " + ", ".join(diagnostics))
        lines.append("")

    lines.append(f"ID отчёта: {report_id}")
    if REPORT_BASE_URL:
        lines.append(f"Отчёт: {REPORT_BASE_URL}/r/{report_id}")

    lines.extend([
        "",
        "Важно: используются публичные веб-страницы. "
        "Результаты требуют проверки по первоисточникам.",
    ])

    return "\n".join(lines)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🕵️ Sherlock AI OSINT\n\n"
        "Поиск по общедоступным источникам.\n\n"
        "Команда:\n"
        "/search запрос\n\n"
        "Примеры:\n"
        "/search +79991234567\n"
        "/search example@example.com\n"
        "/search @username\n"
        "/search example.com\n"
        "/search 8.8.8.8\n"
        "/search название компании\n\n"
        "/report ID — открыть сохранённый отчёт\n"
        "/status — состояние бота\n"
        "/clear — удалить контекст текущего диалога\n\n"
        "Бот не ищет закрытые документы, банковские данные "
        "или домашние адреса частных лиц."
    )
    await update.effective_message.reply_text(text)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()

    if not query:
        await update.effective_message.reply_text(
            "Укажи запрос после команды.\nПример: /search example.com"
        )
        return

    if len(query) > 200:
        await update.effective_message.reply_text(
            "Слишком длинный запрос. Максимум — 200 символов."
        )
        return

    query_type = classify_query(query)
    if query_type == "empty":
        await update.effective_message.reply_text("Пустой запрос.")
        return

    status_message = await update.effective_message.reply_text(
        "🔎 Проверяю общедоступные страницы. Это может занять несколько секунд…"
    )

    result = extract_results(query, query_type)
    report_id = save_report(query, query_type, result)
    report_text = format_report(report_id, query, query_type, result)

    # Telegram message limit is 4096 characters.
    if len(report_text) > 3900:
        report_text = report_text[:3800] + "\n\nОтчёт сокращён."
        if REPORT_BASE_URL:
            report_text += f"\nПолный отчёт: {REPORT_BASE_URL}/r/{report_id}"

    try:
        await status_message.edit_text(report_text, disable_web_page_preview=True)
    except Exception:
        await update.effective_message.reply_text(
            report_text, disable_web_page_preview=True
        )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Укажи ID отчёта: /report ID"
        )
        return

    report = get_report(context.args[0].strip())
    if not report:
        await update.effective_message.reply_text(
            "Отчёт не найден. Проверь ID."
        )
        return

    await update.effective_message.reply_text(
        format_report(
            report["id"],
            report["query"],
            report["query_type"],
            report["results"],
        )[:3900],
        disable_web_page_preview=True,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "✅ Sherlock AI OSINT запущен.\n"
        "Хранилище: SQLite\n"
        "Поиск: публичные веб-страницы\n"
        "Команда: /search запрос"
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Контекст текущего диалога очищен. Сохранённые отчёты не удалены."
    )


@app.get("/")
def home():
    return "Sherlock AI OSINT is running."


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Sherlock AI OSINT"})


@app.get("/r/<report_id>")
def public_report(report_id):
    report = get_report(report_id)
    if not report:
        return jsonify({"error": "Report not found"}), 404

    # Escape all stored values before embedding them in HTML.
    safe_query = html.escape(report["query"])
    safe_type = html.escape(report["query_type"])
    safe_status = html.escape(report["results"].get("status", "unknown"))

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Sherlock AI OSINT report</title></head><body>",
        "<h1>Sherlock AI OSINT</h1>",
        f"<p><b>Запрос:</b> {safe_query}</p>",
        f"<p><b>Тип:</b> {safe_type}</p>",
        f"<p><b>Статус:</b> {safe_status}</p>",
        "<h2>Публичные результаты</h2><ul>",
    ]

    for item in report["results"].get("items", []):
        title = html.escape(item.get("title", "Без названия"))
        url = item.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue

        safe_url = html.escape(url, quote=True)
        snippet = html.escape(item.get("snippet", ""))
        parts.append(
            f"<li><a href='{safe_url}' rel='noopener noreferrer'>"
            f"{title}</a><p>{snippet}</p></li>"
        )

    parts.extend([
        "</ul><p>Результаты собраны из общедоступных веб-страниц "
        "и требуют проверки.</p></body></html>"
    ])

    return Response("\n".join(parts), mimetype="text/html")


def run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN в переменных окружения."
        )

    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("clear", clear_command))

    print("Sherlock AI OSINT is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
