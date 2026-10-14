FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/app/data/groups.db

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py ./

# قاعدة البيانات تُحفظ في مجلد خارجي (volume) حتى لا تضيع عند تحديث الحاوية
VOLUME ["/app/data"]

CMD ["python", "main.py"]
