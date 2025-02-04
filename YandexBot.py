import imaplib
import email
from email.header import decode_header
import telebot
import logging
from email.utils import parseaddr
import threading
from datetime import datetime

# Включаем логирование
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Конфигурация
TELEGRAM_BOT_TOKEN = "8056554842:AAG7d5-Bv-3j7Uz3YLfeBajrLbGPegQs9Ss"
YANDEX_EMAIL = "crowmen007@yandex.ru"
YANDEX_PASSWORD = "ydzekkgbmyvspklg"
TARGET_EMAIL = "sladait@yandex.ru"  # Адрес, с которого принимаются письма

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Функция для получения последнего непочитанного письма за текущий день
def get_latest_unseen_email_today():
    try:
        logging.info("Подключение к Яндекс.Почте...")
        mail = imaplib.IMAP4_SSL("imap.yandex.ru")
        mail.login(YANDEX_EMAIL, YANDEX_PASSWORD)
        mail.select("inbox")

        # Получаем текущую дату в формате IMAP (например, "04-Feb-2025")
        current_date = datetime.now().strftime("%d-%b-%Y")

        # Запросим все непочитанные письма с сегодняшней даты
        status, messages = mail.search(None, f'(UNSEEN SINCE {current_date})')
        logging.info(f"Статус поиска: {status}, сообщения: {messages}")

        if status != "OK":
            logging.error("Ошибка поиска сообщений")
            return None

        mail_ids = messages[0].split()
        if not mail_ids:
            logging.warning("Нет новых непочитанных писем с сегодняшнего дня")
            return None

        # Получаем данные последнего непочитанного письма
        mail_id = mail_ids[-1]
        status, msg_data = mail.fetch(mail_id, "(RFC822)")
        if status != "OK":
            logging.error(f"Ошибка при получении письма {mail_id}")
            return None

        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])

                # Проверяем адрес отправителя
                from_ = msg.get("From")
                from_name, from_addr = parseaddr(from_)
                if from_addr != TARGET_EMAIL:
                    logging.info(f"Письмо от {from_addr} не соответствует целевому адресу")
                    continue

                # Декодируем тему письма
                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or "utf-8")

                # Дата письма
                date_ = msg.get("Date")

                # Извлекаем тело письма
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        content_disposition = str(part.get("Content-Disposition"))

                        if "attachment" not in content_disposition:
                            if content_type == "text/plain":  # Текст
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                break
                            elif content_type == "text/html":  # HTML
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                break
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                email_info = f"Тема: {subject}\nОт: {from_}\nДата: {date_}\nТело:\n{body[:1000]}..."

                # Помечаем письмо как прочитанное
                mail.store(mail_id, '+FLAGS', '\\Seen')

                mail.logout()
                return email_info

    except Exception as e:
        logging.error(f"Ошибка при получении письма: {e}")
        return None

# Функция для опроса почты каждую минуту
def poll_email():
    email_info = get_latest_unseen_email_today()
    if email_info:
        logging.info(f"Новое письмо: {email_info}")
        # Отправляем сообщение в Telegram
        bot.send_message(843297494, email_info)  # Укажите свой chat_id
    else:
        logging.info("Нет новых писем с сегодняшнего дня")

    # Запускаем функцию снова через 60 секунд
    threading.Timer(60, poll_email).start()

# Обработчик команды /start
@bot.message_handler(commands=["start"])
def send_welcome(message):
    print(f"Получена команда /start от {message.chat.id}")
    bot.send_message(message.chat.id, "Бот запущен! Он будет опрашивать почту каждую минуту.")

# Запуск опроса почты при старте бота
if __name__ == "__main__":
    print("Бот запущен и слушает команды...")
    poll_email()  # Запускаем процесс опроса почты
    bot.polling(none_stop=True)
