FROM python:3.12-slim

# wkhtmltoimage — рендер HTML-письма в картинку. Берём официальную patched-Qt
# сборку (.deb с GitHub): работает headless, без xvfb. Шрифты — для кириллицы.
ARG WKHTMLTOX_URL=https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_amd64.deb
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates wget fontconfig fonts-dejavu-core fonts-liberation \
    && wget -q -O /tmp/wkhtmltox.deb "$WKHTMLTOX_URL" \
    && apt-get install -y --no-install-recommends /tmp/wkhtmltox.deb \
    && rm -f /tmp/wkhtmltox.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
# -u: небуферизованный вывод, чтобы логи сразу шли в docker logs
CMD ["python", "-u", "bot.py"]
