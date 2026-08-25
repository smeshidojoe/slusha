FROM python:3.13-slim

# tini — чтобы docker stop доходил до python обычным SIGTERM и бот
# завершался штатно, а не убивался на полуслове
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

WORKDIR /app

# зависимости отдельным слоем: правки кода не тянут переустановку пакетов
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# пакет кладём внутрь app/slusha — импорт остаётся «python -m slusha»
COPY . ./slusha

# всё изменяемое (база, лог) живёт в /app/data — он монтируется с хоста
RUN useradd -u 1000 -m slusha \
 && mkdir -p /app/data \
 && chown -R slusha:slusha /app
USER slusha

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "slusha"]
