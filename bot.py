#!/usr/bin/env python3
"""
email-to-telegram-bot — уведомления в Telegram о новых письмах Яндекс-почты.

Каждые POLL_INTERVAL секунд опрашивает IMAP Яндекса, берёт ВСЕ непрочитанные
письма (опционально только от TARGET_EMAIL), пересылает тему/тело в Telegram
и помечает их прочитанными. Секреты — в .env, в коде их нет.
"""

import os
import time
import logging
import threading
import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr

import telebot
from dotenv import load_dotenv

load_dotenv()

# ── Конфигурация из окружения (.env) ──────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
YANDEX_EMAIL = os.environ["YANDEX_EMAIL"]
YANDEX_PASSWORD = os.environ["YANDEX_PASSWORD"]
TARGET_EMAIL = os.environ.get("TARGET_EMAIL", "").strip()  # пусто = принимать от всех
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.yandex.ru")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip()  # напр. http://HOST:PORT или socks5://HOST:PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("email2tg")

# Если сеть не пускает к api.telegram.org напрямую — ходим через прокси.
if TELEGRAM_PROXY:
    telebot.apihelper.proxy = {"https": TELEGRAM_PROXY}
    log.info("Telegram через прокси: %s", TELEGRAM_PROXY)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)


def _decode(value):
    """Декодирует MIME-заголовок (тема/имя) в строку."""
    if not value:
        return ""
    parts = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                parts.append(text.decode(enc or "utf-8", errors="ignore"))
            except (LookupError, TypeError):
                parts.append(text.decode("utf-8", errors="ignore"))
        else:
            parts.append(text)
    return "".join(parts)


def _extract_body(msg):
    """Достаёт текстовое тело письма (предпочитая text/plain)."""
    if msg.is_multipart():
        html_fallback = ""
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="ignore")
            if ctype == "text/plain":
                return text
            if ctype == "text/html" and not html_fallback:
                html_fallback = text
        return html_fallback
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="ignore")


def _format(msg):
    subject = _decode(msg.get("Subject"))
    from_ = _decode(msg.get("From"))
    date_ = msg.get("Date", "")
    body = _extract_body(msg).strip()
    if len(body) > 3000:
        body = body[:3000] + "…"
    return f"📧 Новое письмо\nТема: {subject}\nОт: {from_}\nДата: {date_}\n\n{body}"


def process_unseen():
    """Обрабатывает все непрочитанные письма (от TARGET_EMAIL, если задан)."""
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        mail.login(YANDEX_EMAIL, YANDEX_PASSWORD)
        mail.select("inbox")

        criteria = ["UNSEEN"]
        if TARGET_EMAIL:
            criteria += ["FROM", TARGET_EMAIL]
        status, data = mail.search(None, *criteria)
        if status != "OK":
            log.error("IMAP search вернул %s", status)
            return

        ids = data[0].split()
        if not ids:
            log.info("Новых писем нет")
            return
        log.info("Непрочитанных подходящих писем: %d", len(ids))

        for mail_id in ids:
            status, msg_data = mail.fetch(mail_id, "(RFC822)")
            if status != "OK":
                log.error("Не удалось забрать письмо %s", mail_id)
                continue
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw)

            # Доп. проверка отправителя (страховка, если сервер проигнорил FROM)
            if TARGET_EMAIL:
                _, addr = parseaddr(msg.get("From", ""))
                if addr.lower() != TARGET_EMAIL.lower():
                    continue

            text = _format(msg)
            try:
                bot.send_message(TELEGRAM_CHAT_ID, text[:4096])
            except Exception as e:
                log.error("Не отправилось в Telegram (%s) — письмо оставляю непрочитанным", e)
                continue  # НЕ помечаем Seen → повторим в следующем цикле

            mail.store(mail_id, "+FLAGS", "\\Seen")
            log.info("Письмо %s переслано и помечено прочитанным", mail_id.decode())
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def poll_loop():
    log.info(
        "Старт опроса почты каждые %d с (ящик %s, фильтр FROM=%s)",
        POLL_INTERVAL, YANDEX_EMAIL, TARGET_EMAIL or "—",
    )
    while True:
        try:
            process_unseen()
        except Exception as e:
            log.error("Ошибка цикла опроса: %s", e)
        time.sleep(POLL_INTERVAL)


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.send_message(
        message.chat.id,
        f"Бот запущен. Слежу за почтой {YANDEX_EMAIL} и шлю сюда новые письма"
        + (f" от {TARGET_EMAIL}." if TARGET_EMAIL else "."),
    )
    log.info("/start от chat_id=%s", message.chat.id)


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    log.info("Бот запущен, слушаю команды Telegram…")
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
