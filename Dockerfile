# Yatırım Asistanı — üretim imajı
FROM python:3.11-slim

# Sistem bağımlılıkları (lxml/yfinance derlemeleri için)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Istanbul

WORKDIR /app

# Önce bağımlılıklar (katman önbelleği için)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodu
COPY . .

# Kalıcı veri/log dizinleri (compose'ta volume olarak bağlanır)
RUN mkdir -p logs data config/users

CMD ["python", "main.py"]
