"""
Haber tarayıcı.

Belirli aralıklarla her kullanıcının portföyündeki varlıklar için haber tarar,
Claude'a önem puanı sorar ve eşiği geçen haberleri ilgili kullanıcıya bildirir.
Aynı haberin tekrar gönderilmemesi için görülen haber bağlantıları diske kaydedilir.
"""

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Set

from telegram import Bot
from telegram.constants import ParseMode

from bot import formats
from core import analyzer, data_fetcher, user_manager
from utils.config import get_settings
from utils.logger import get_logger

logger = get_logger(__name__)

_settings = get_settings()
_news_cfg = _settings.get("news", {})
THRESHOLD = _news_cfg.get("importance_threshold", 7)

TR_TZ = timezone(timedelta(hours=3))

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SEEN_PATH = os.path.join(_BASE_DIR, "data", "seen_news.json")


def _load_seen() -> Set[str]:
    try:
        with open(_SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_seen(seen: Set[str]) -> None:
    os.makedirs(os.path.dirname(_SEEN_PATH), exist_ok=True)
    # Sınırsız büyümeyi önlemek için son 2000 kaydı tut
    trimmed = list(seen)[-2000:]
    with open(_SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False)


def _is_active_hour() -> bool:
    """Tarama yalnızca yapılandırılan saat aralığında çalışır."""
    hours = _news_cfg.get("active_hours", {"start": 9, "end": 23})
    now_hour = datetime.now(TR_TZ).hour
    return hours["start"] <= now_hour <= hours["end"]


def _impact_label(score: int) -> str:
    if score >= 8:
        return "Yüksek"
    if score >= 6:
        return "Orta"
    return "Düşük"


async def scan_all_users(bot: Bot) -> None:
    """
    Tüm kullanıcılar için haber taraması yapar ve eşiği geçen haberleri gönderir.
    APScheduler tarafından periyodik çağrılır.
    """
    if not _is_active_hour():
        logger.info("Haber taraması atlandı (aktif saat dışı).")
        return

    users = user_manager.list_users()
    if not users:
        return

    seen = _load_seen()
    logger.info("Haber taraması başladı (%d kullanıcı).", len(users))

    for user in users:
        # Bildirim kapalıysa atla
        if user.get("notification_preference") == "off":
            continue

        chat_id = user["chat_id"]
        symbols = [h["symbol"] for h in user.get("portfolio", [])]

        for symbol in symbols:
            try:
                # Senkron veri çekmeyi thread'e taşı
                articles = await asyncio.to_thread(data_fetcher.get_news_for_symbol, symbol)
            except Exception as exc:
                logger.warning("Haber çekme hatası (%s/%s): %s", chat_id, symbol, exc)
                continue

            for article in articles:
                link = article.get("link") or article.get("title", "")
                seen_key = f"{chat_id}:{link}"
                if not link or seen_key in seen:
                    continue
                seen.add(seen_key)

                # Önem puanı (senkron API çağrısını thread'e taşı)
                try:
                    score = await asyncio.to_thread(
                        analyzer.score_news_importance, article, user, symbol
                    )
                except Exception as exc:
                    logger.warning("Puanlama hatası: %s", exc)
                    continue

                logger.debug("%s/%s haber puanı=%d", chat_id, symbol, score)
                if score < THRESHOLD:
                    continue

                # Kısa beklenti yorumu üret
                try:
                    comment = await asyncio.to_thread(
                        analyzer.analyze_symbol, symbol, user, None, [article]
                    )
                except Exception:
                    comment = "Değerlendirme şu an üretilemedi."

                # Telegram limitleri için kısalt
                short_comment = comment.split("\n")[0][:200]
                msg = formats.news_notification(
                    user.get("name", "Sen"), symbol.upper(), article,
                    _impact_label(score), short_comment,
                )
                try:
                    await bot.send_message(chat_id=chat_id, text=msg)
                    logger.info("Haber bildirimi gönderildi: %s/%s (puan %d)", chat_id, symbol, score)
                except Exception as exc:
                    logger.error("Bildirim gönderilemedi (%s): %s", chat_id, exc)

    _save_seen(seen)
    logger.info("Haber taraması tamamlandı.")
