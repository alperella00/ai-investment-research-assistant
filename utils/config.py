"""
Ayarların yüklenmesi.

`settings.yaml` dosyasını okur ve singleton bir sözlük olarak sunar.
Ortam değişkenleri (.env) ayrıca burada yüklenir.
"""

import os
from functools import lru_cache
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

from utils.logger import get_logger

logger = get_logger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS_PATH = os.path.join(_BASE_DIR, "config", "settings.yaml")
_USERS_DIR = os.path.join(_BASE_DIR, "config", "users")

# .env dosyasını süreç başında bir kez yükle
load_dotenv(os.path.join(_BASE_DIR, ".env"))


@lru_cache(maxsize=1)
def get_settings() -> Dict[str, Any]:
    """settings.yaml içeriğini sözlük olarak döndürür (bir kez okunur)."""
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            settings = yaml.safe_load(f)
        logger.info("Ayarlar yüklendi: %s", _SETTINGS_PATH)
        return settings or {}
    except FileNotFoundError:
        logger.error("Ayar dosyası bulunamadı: %s", _SETTINGS_PATH)
        raise
    except yaml.YAMLError as exc:
        logger.error("Ayar dosyası ayrıştırılamadı: %s", exc)
        raise


def get_env(key: str, default: str | None = None) -> str | None:
    """Ortam değişkeni okur."""
    return os.getenv(key, default)


def get_users_dir() -> str:
    """Kullanıcı JSON dosyalarının bulunduğu dizini döndürür."""
    os.makedirs(_USERS_DIR, exist_ok=True)
    return _USERS_DIR
