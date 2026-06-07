"""
Kullanıcı yönetimi.

Her kullanıcı `config/users/<chat_id>.json` dosyasında saklanır.
Bu modül kullanıcı oluşturma, okuma, güncelleme, silme ve portföy/alarm
işlemlerini kapsar. Eşzamanlı erişimi basitçe korumak için bir kilit kullanılır.
"""

import json
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from utils.config import get_users_dir
from utils.logger import get_logger

logger = get_logger(__name__)

# Türkiye saat dilimi (UTC+3)
TR_TZ = timezone(timedelta(hours=3))

# Dosya yazımlarını koruyan basit kilit
_lock = threading.RLock()


def _user_path(chat_id: int | str) -> str:
    """Bir chat_id için JSON dosya yolunu döndürür."""
    return os.path.join(get_users_dir(), f"{chat_id}.json")


def _now_iso() -> str:
    """Türkiye saatiyle ISO formatlı zaman damgası."""
    return datetime.now(TR_TZ).isoformat()


def default_user(chat_id: int | str, name: str = "Kullanıcı") -> Dict[str, Any]:
    """Varsayılan bir kullanıcı şeması üretir."""
    return {
        "chat_id": str(chat_id),
        "name": name,
        "risk_profile": "orta",
        "notification_preference": "high",  # high | all | off
        "briefing_enabled": True,
        "created_at": _now_iso(),
        "portfolio": [],   # [{symbol, quantity, cost}]
        "alarms": [],      # [{symbol, target, direction, created_at}]
    }


# ─────────────────────────────────────────────
# Temel CRUD
# ─────────────────────────────────────────────

def user_exists(chat_id: int | str) -> bool:
    return os.path.exists(_user_path(chat_id))


def load_user(chat_id: int | str) -> Optional[Dict[str, Any]]:
    """Kullanıcıyı diskten okur; yoksa None döner."""
    path = _user_path(chat_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Kullanıcı okunamadı (%s): %s", chat_id, exc)
        return None


def save_user(user: Dict[str, Any]) -> None:
    """Kullanıcıyı diske atomik şekilde yazar."""
    chat_id = user["chat_id"]
    path = _user_path(chat_id)
    tmp = f"{path}.tmp"
    with _lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(user, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)  # atomik
            logger.debug("Kullanıcı kaydedildi: %s", chat_id)
        except OSError as exc:
            logger.error("Kullanıcı kaydedilemedi (%s): %s", chat_id, exc)
            raise


def create_user(chat_id: int | str, name: str = "Kullanıcı") -> Dict[str, Any]:
    """Yeni kullanıcı oluşturur (varsa mevcut olanı döndürür)."""
    existing = load_user(chat_id)
    if existing:
        return existing
    user = default_user(chat_id, name)
    save_user(user)
    logger.info("Yeni kullanıcı oluşturuldu: %s (%s)", name, chat_id)
    return user


def delete_user(chat_id: int | str) -> bool:
    """Kullanıcı dosyasını siler."""
    path = _user_path(chat_id)
    if os.path.exists(path):
        os.remove(path)
        logger.info("Kullanıcı silindi: %s", chat_id)
        return True
    return False


def list_users() -> List[Dict[str, Any]]:
    """Tüm kayıtlı kullanıcıları döndürür."""
    users: List[Dict[str, Any]] = []
    directory = get_users_dir()
    for fname in os.listdir(directory):
        if not fname.endswith(".json") or fname.startswith("user_template"):
            continue
        chat_id = fname[:-5]
        user = load_user(chat_id)
        if user:
            users.append(user)
    return users


# ─────────────────────────────────────────────
# Portföy işlemleri
# ─────────────────────────────────────────────

def add_holding(chat_id: int | str, symbol: str, quantity: float, cost: float) -> Dict[str, Any]:
    """Portföye varlık ekler veya mevcut varlığı ortalamayla günceller."""
    user = load_user(chat_id) or create_user(chat_id)
    symbol = symbol.upper()
    for h in user["portfolio"]:
        if h["symbol"].upper() == symbol:
            # Maliyet ortalaması
            total_qty = h["quantity"] + quantity
            if total_qty > 0:
                h["cost"] = (h["quantity"] * h["cost"] + quantity * cost) / total_qty
            h["quantity"] = total_qty
            save_user(user)
            return user
    user["portfolio"].append({"symbol": symbol, "quantity": quantity, "cost": cost})
    save_user(user)
    logger.info("%s portföyüne eklendi: %s x%s @%s", chat_id, symbol, quantity, cost)
    return user


def remove_holding(chat_id: int | str, symbol: str) -> bool:
    """Portföyden bir varlığı tümüyle çıkarır."""
    user = load_user(chat_id)
    if not user:
        return False
    symbol = symbol.upper()
    before = len(user["portfolio"])
    user["portfolio"] = [h for h in user["portfolio"] if h["symbol"].upper() != symbol]
    if len(user["portfolio"]) < before:
        save_user(user)
        logger.info("%s portföyünden çıkarıldı: %s", chat_id, symbol)
        return True
    return False


# ─────────────────────────────────────────────
# Alarm işlemleri
# ─────────────────────────────────────────────

def add_alarm(chat_id: int | str, symbol: str, target: float, direction: str = "auto") -> Dict[str, Any]:
    """
    Fiyat alarmı ekler.
    direction: 'above' | 'below' | 'auto' (auto ise mevcut fiyata göre belirlenir)
    """
    user = load_user(chat_id) or create_user(chat_id)
    alarm = {
        "symbol": symbol.upper(),
        "target": float(target),
        "direction": direction,
        "created_at": _now_iso(),
    }
    user["alarms"].append(alarm)
    save_user(user)
    logger.info("%s için alarm eklendi: %s @%s", chat_id, symbol, target)
    return user


def remove_alarm(chat_id: int | str, index: int) -> bool:
    """Belirtilen indeksteki alarmı kaldırır."""
    user = load_user(chat_id)
    if not user or index < 0 or index >= len(user["alarms"]):
        return False
    user["alarms"].pop(index)
    save_user(user)
    return True


def update_setting(chat_id: int | str, key: str, value: Any) -> bool:
    """Kullanıcı düzeyinde bir ayarı günceller (ör. notification_preference)."""
    user = load_user(chat_id)
    if not user:
        return False
    user[key] = value
    save_user(user)
    logger.info("%s ayarı güncellendi: %s=%s", chat_id, key, value)
    return True
