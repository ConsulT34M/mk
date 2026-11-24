FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home portal \
    && install -d -o portal -g portal -m 0700 /var/lib/tm-recordings
COPY app.py common.py cases.py db.py recordings.py start.sh ./
COPY templates templates
COPY static static
USER portal
CMD ["sh", "start.sh"]
