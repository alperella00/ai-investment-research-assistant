"""
Yapılandırılmış loglama kurulumu.

Hem konsola hem de dönen (rotating) dosyaya yazar. Tüm modüller
`get_logger(__name__)` çağırarak aynı yapılandırmayı paylaşır.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# Proje kök dizini ve log dosyası yolu
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_BASE_DIR, "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "asistan.log")

# Loglamanın yalnızca bir kez yapılandırılmasını sağlamak için bayrak
_configured = False


def _configure_root() -> None:
    """Kök logger'ı bir kez yapılandırır (konsol + dosya handler)."""
    global _configured
    if _configured:
        return

    os.makedirs(_LOG_DIR, exist_ok=True)

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Konsol handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Dosya handler (5 MB'lık 5 dosya döngüsü)
    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Gürültülü üçüncü taraf logger'ları kısıyoruz
    for noisy in ("httpx", "apscheduler", "yfinance", "telegram", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """İsimlendirilmiş bir logger döndürür; kök yapılandırmayı garantiler."""
    _configure_root()
    return logging.getLogger(name)
