#!/usr/bin/env python3
"""
email-to-telegram-bot — уведомления в Telegram о новых письмах Яндекс-почты.

Каждые POLL_INTERVAL секунд опрашивает IMAP, берёт ВСЕ непрочитанные письма
(опционально только от TARGET_EMAIL), пересылает их в Telegram и помечает
прочитанными. HTML-письма разворачиваются в аккуратное сообщение с лёгкой
Telegram-разметкой. Секреты — в .env, в коде их нет.
"""

import os
import re
import time
import logging
import threading
import subprocess
import tempfile
import imaplib
import email
import html as ihtml
from io import BytesIO
from html.parser import HTMLParser
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

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
RENDER_HTML = os.environ.get("RENDER_HTML", "1").strip() not in ("0", "false", "no", "")  # HTML→картинка
RENDER_WIDTH = int(os.environ.get("RENDER_WIDTH", "600"))

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

TG_LIMIT = 4096


# ── HTML → Telegram-разметка ──────────────────────────────────────────────
# Telegram HTML понимает только <b> <i> <u> <s> <code> <pre> <a>. Конвертер
# ставит переносы на блочных тегах, разделяет ячейки таблиц и сохраняет
# жирный/курсив/зачёркнутый (в т.ч. style="...line-through...").
_BLOCK = {"p", "div", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
          "li", "ul", "ol", "section", "header", "footer", "article", "blockquote"}
_VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base",
         "col", "embed", "source", "track", "wbr"}
_EMPH = {"b": "b", "strong": "b", "i": "i", "em": "i",
         "s": "s", "strike": "s", "del": "s"}


class _HtmlToTg(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0          # внутри <style>/<script>
        self.stack = []        # (tag, emphasis|None) для балансировки

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
            return
        if tag == "br":
            self.out.append("\n")
            return
        if tag in _VOID:
            return
        if tag in _BLOCK:
            self.out.append("\n")
        if tag in ("td", "th") and self.out and not self.out[-1].endswith(("\n", "\t", " ")):
            self.out.append("\t")
        emph = _EMPH.get(tag)
        if emph is None:
            style = dict(attrs).get("style", "") or ""
            if "line-through" in style:
                emph = "s"
        if emph:
            self.out.append("<%s>" % emph)
        self.stack.append((tag, emph))

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            if self.skip:
                self.skip -= 1
            return
        if tag in _VOID:
            return
        while self.stack:
            t, emph = self.stack.pop()
            if emph:
                self.out.append("</%s>" % emph)
            if t == tag:
                break
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        data = re.sub(r"\s+", " ", data)   # HTML схлопывает пробелы/переносы
        if data.strip() or data == " ":
            self.out.append(ihtml.escape(data, quote=False))


def html_to_tg(html_src):
    p = _HtmlToTg()
    p.feed(html_src)
    p.close()
    text = "".join(p.out)
    res = []
    for ln in text.split("\n"):
        ln = ln.replace("\t", " · ")                 # ячейки таблицы → разделитель
        ln = re.sub(r" +", " ", ln).strip()
        ln = re.sub(r"(?:\s*·\s*){2,}", " · ", ln)    # схлопнуть пустые ячейки
        ln = ln.strip(" ·").strip()
        if ln == "" and (not res or res[-1] == ""):
            continue
        res.append(ln)
    while res and res[-1] == "":
        res.pop()
    return "\n".join(res).strip()


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


_RU_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _ru_date(raw):
    """RFC2822-дата письма → '14 июня 2026, 01:26' (без английских названий)."""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        return "%d %s %d, %02d:%02d" % (dt.day, _RU_MONTHS[dt.month], dt.year, dt.hour, dt.minute)
    except Exception:
        return raw


def _get_part(msg, ctype):
    for part in msg.walk():
        if part.get_content_type() == ctype and "attachment" not in str(part.get("Content-Disposition", "")):
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="ignore")
    return None


def _body_for_tg(msg):
    """Тело письма как Telegram-HTML. Предпочитаем HTML-часть (text/plain от 1С
    приходит слипшимся), иначе экранируем plain."""
    html_src = _get_part(msg, "text/html")
    if html_src:
        body = html_to_tg(html_src)
        if body:
            return body
    plain = _get_part(msg, "text/plain") or ""
    return ihtml.escape(plain.strip(), quote=False)


def _format(msg):
    subject = ihtml.escape(_decode(msg.get("Subject")), quote=False)
    from_ = ihtml.escape(_decode(msg.get("From")), quote=False)
    date_ = ihtml.escape(_ru_date(msg.get("Date", "")), quote=False)
    body = _body_for_tg(msg)
    header = "📧 <b>%s</b>\n👤 %s\n🕒 %s" % (subject, from_, date_)
    msg_text = header + "\n\n" + body
    if len(msg_text) > TG_LIMIT:
        msg_text = msg_text[:TG_LIMIT - 1].rsplit("\n", 1)[0] + "\n…"
    return msg_text


def _send(text):
    """Отправка с разметкой; при ошибке парсинга — fallback на чистый текст."""
    try:
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML",
                         disable_web_page_preview=True)
        return True
    except Exception as e:
        log.warning("HTML-отправка не прошла (%s) — шлю чистым текстом", e)
        plain = ihtml.unescape(re.sub(r"<[^>]+>", "", text))
        bot.send_message(TELEGRAM_CHAT_ID, plain[:TG_LIMIT])
        return True


def render_html_png(html_src):
    """Рендерит HTML письма в PNG через wkhtmltoimage (headless, xvfb)."""
    hp = pp = None
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html_src)
        hp = f.name
    pp = hp + ".png"
    try:
        subprocess.run(
            ["wkhtmltoimage", "--quality", "92",
             "--width", str(RENDER_WIDTH), "--encoding", "utf-8",
             "--disable-javascript", hp, pp],
            check=True, capture_output=True, timeout=90,
        )
        with open(pp, "rb") as fp:
            return fp.read()
    finally:
        for p in (hp, pp):
            try:
                if p:
                    os.remove(p)
            except OSError:
                pass


def _caption(msg):
    subject = ihtml.escape(_decode(msg.get("Subject")), quote=False)
    from_ = ihtml.escape(_decode(msg.get("From")), quote=False)
    date_ = ihtml.escape(_ru_date(msg.get("Date", "")), quote=False)
    return ("📧 <b>%s</b>\n👤 %s · 🕒 %s" % (subject, from_, date_))[:1024]


def _deliver(msg):
    """HTML-письмо → картинка (вид 1:1); иначе/при сбое — текстовый формат."""
    if RENDER_HTML:
        html_src = _get_part(msg, "text/html")
        if html_src:
            try:
                png = render_html_png(html_src)
                bot.send_photo(TELEGRAM_CHAT_ID, BytesIO(png),
                               caption=_caption(msg), parse_mode="HTML")
                return
            except Exception as e:
                log.warning("Рендер HTML→картинку не удался (%s) — шлю текстом", e)
    _send(_format(msg))


def process_unseen():
    """Обрабатывает все непрочитанные письма (от TARGET_EMAIL, если задан)."""
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        mail.login(YANDEX_EMAIL, YANDEX_PASSWORD)
        mail.select("inbox")

        # На сервере ищем только UNSEEN. Фильтр по отправителю — на клиенте ниже
        # (parseaddr-проверка). Серверный SEARCH FROM на Яндексе НЕЛЬЗЯ: он бьёт
        # по токенам и не матчит полный адрес "user@domain.tld"
        # (напр. SEARCH FROM "user@yandex.ru" -> 0 результатов при наличии письма),
        # из-за чего письма молча терялись.
        status, data = mail.search(None, "UNSEEN")
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

            try:
                _deliver(msg)
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
        "Бот запущен. Слежу за почтой %s и шлю сюда новые письма%s"
        % (YANDEX_EMAIL, (" от %s." % TARGET_EMAIL) if TARGET_EMAIL else "."),
    )
    log.info("/start от chat_id=%s", message.chat.id)


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    log.info("Бот запущен, слушаю команды Telegram…")
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
