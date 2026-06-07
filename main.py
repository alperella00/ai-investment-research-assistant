"""
Yatırım Asistanı — Ana giriş noktası.

Telegram botunu ve APScheduler zamanlayıcısını aynı asyncio olay döngüsünde
birlikte çalıştırır. SIGINT/SIGTERM ile düzgün (graceful) kapanır.

Çalıştırma:
    python main.py
"""

import sys

from telegram import Update
from telegram.ext import Application, ContextTypes

from bot.telegram_handler import register_handlers
from core.scheduler import setup_scheduler
from utils.config import get_env
from utils.logger import get_logger

logger = get_logger(__name__)


async def on_startup(application: Application) -> None:
    """Bot başladıktan sonra scheduler'ı kurar ve başlatır."""
    scheduler = setup_scheduler(application.bot)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info("Zamanlayıcı başlatıldı.")

    # Bot komut menüsünü Telegram'a tanıt
    try:
        await application.bot.set_my_commands([
            ("basla", "Kayıt ve portföy kurulumu"),
            ("portfoy", "Portföy durumu"),
            ("brifing", "Piyasa brifingi"),
            ("analiz", "Sembol/sektör analizi"),
            ("haber", "Sembol haberleri"),
            ("alarm", "Fiyat alarmı kur"),
            ("ekle", "Portföye varlık ekle"),
            ("cikar", "Portföyden varlık çıkar"),
            ("ayar", "Bildirim ayarları"),
        ])
    except Exception as exc:
        logger.warning("Komut menüsü ayarlanamadı: %s", exc)


async def on_shutdown(application: Application) -> None:
    """Kapanışta scheduler'ı durdurur."""
    scheduler = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Zamanlayıcı durduruldu.")
    logger.info("Uygulama kapatıldı. Görüşmek üzere 👋")


def main() -> None:
    token = get_env("TELEGRAM_BOT_TOKEN")
    if not token or token.startswith("123456789"):
        logger.error("TELEGRAM_BOT_TOKEN tanımlı değil veya örnek değer. .env dosyasını doldurun.")
        sys.exit(1)

    if not get_env("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY tanımlı değil. .env dosyasını doldurun.")
        sys.exit(1)

    logger.info("Yatırım Asistanı başlatılıyor...")

    application = (
        Application.builder()
        .token(token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    register_handlers(application)

    # run_polling SIGINT/SIGTERM sinyallerini yakalayıp graceful kapanış sağlar
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
