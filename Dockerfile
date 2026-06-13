FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
# -u: небуферизованный вывод, чтобы логи сразу шли в docker logs
CMD ["python", "-u", "bot.py"]
