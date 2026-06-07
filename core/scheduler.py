"""
Zamanlayıcı.

APScheduler (AsyncIOScheduler) ile aşağıdaki görevleri kurar:
  - Sabah brifing      → her gün (settings: morning_briefing)
  - Akşam özeti        → her gün (settings: evening_summary)
  - Haber taraması     → her N dakikada (settings: scan_interval_minutes)
  - Fiyat alarmı       → her N dakikada (settings: check_interval_minutes)
  - Haftalık rapor     → haftada bir (settings: weekly_report_*)

Her görev ayrı bir job'tır ve hatalar yutularak loglanır
(bir görevin çökmesi diğerlerini etkilemez).
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.constants import ParseMode

from bot import formats
from core import analyzer, data_fetcher, news_scanner, user_manager
from utils.config import get_settings
from utils.logger import get_logger

logger = get_logger(__name__)

_settings = get_settings()
_sched_cfg = _settings.get("schedule", {})
_TZ_NAME = _sched_cfg.get("timezone", "Europe/Istanbul")
_TZ = pytz.timezone(_TZ_NAME)

TR_TZ = timezone(timedelta(hours=3))


def _snapshot_to_text(snapshot: Dict[str, Any]) -> Dict[str, str]:
    """Piyasa anlık görünümünü kategori başına metne dönüştürür."""
    def fmt_category(cat: str) -> str:
        items = snapshot.get(cat, {})
        if not items:
            return "veri yok"
        parts = []
        for label, q in items.items():
            chg = q.get("change_pct")
            chg_str = f" ({chg:+}%)" if chg is not None else ""
            parts.append(f"{label} {q['price']:g}{chg_str}")
        return ", ".join(parts)

    return {
        "global": fmt_category("global"),
        "bist": fmt_category("turkey"),
        "fx": fmt_category("fx"),
        "gold": fmt_category("gold"),
        "crypto": fmt_category("crypto"),
    }


async def _send(bot: Bot, chat_id: str, text: str) -> None:
    """Hata yutan güvenli mesaj gönderici."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # Markdown ayrıştırma hatasına karşı düz metin yedeği
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            logger.error("Mesaj gönderilemedi (%s): %s", chat_id, exc)


# ─────────────────────────────────────────────
# Görevler
# ─────────────────────────────────────────────

async def morning_briefing_job(bot: Bot) -> None:
    """Sabah brifingi: tüm brifing-aktif kullanıcılara gönderilir."""
    logger.info("Sabah brifingi görevi başladı.")
    try:
        snapshot = await asyncio.to_thread(data_fetcher.get_market_snapshot)
    except Exception as exc:
        logger.error("Brifing piyasa verisi alınamadı: %s", exc)
        return

    txt = _snapshot_to_text(snapshot)
    snapshot_text = (
        f"Global: {txt['global']} | BIST: {txt['bist']} | "
        f"Döviz: {txt['fx']} | Altın: {txt['gold']} | Kripto: {txt['crypto']}"
    )
    date_str = datetime.now(TR_TZ).strftime("%d.%m.%Y")

    for user in user_manager.list_users():
        if not user.get("briefing_enabled", True):
            continue
        symbols = [h["symbol"] for h in user.get("portfolio", [])]
        try:
            prices = await asyncio.to_thread(data_fetcher.get_prices, symbols)
        except Exception:
            prices = {}

        try:
            commentary = await asyncio.to_thread(
                analyzer.generate_briefing_commentary, user, snapshot_text, prices
            )
        except Exception:
            commentary = "Portföy değerlendirmesi şu an üretilemedi."

        # Yorumu portföy özeti + dikkat olarak böl (basit)
        portfolio_line = commentary.split("DİKKAT")[0].strip()[:300]
        warning_line = ""
        if "DİKKAT" in commentary:
            warning_line = commentary.split("DİKKAT")[-1].lstrip(":").strip()[:200]

        msg = formats.morning_briefing(
            date_str, txt["global"], txt["bist"], txt["fx"], txt["gold"],
            txt["crypto"], portfolio_line or "—", warning_line or "Önemli bir uyarı yok.",
        )
        await _send(bot, user["chat_id"], msg)
    logger.info("Sabah brifingi tamamlandı.")


async def evening_summary_job(bot: Bot) -> None:
    """Akşam özeti: günün portföy durumu."""
    logger.info("Akşam özeti görevi başladı.")
    for user in user_manager.list_users():
        if not user.get("briefing_enabled", True):
            continue
        symbols = [h["symbol"] for h in user.get("portfolio", [])]
        try:
            prices = await asyncio.to_thread(data_fetcher.get_prices, symbols)
        except Exception:
            prices = {}
        msg = "🌙 *AKŞAM ÖZETİ*\n" + formats.portfolio_summary(user, prices)
        await _send(bot, user["chat_id"], msg)
    logger.info("Akşam özeti tamamlandı.")


async def alarm_check_job(bot: Bot) -> None:
    """Fiyat alarmlarını kontrol eder; tetiklenenleri gönderir ve kaldırır."""
    for user in user_manager.list_users():
        alarms = user.get("alarms", [])
        if not alarms:
            continue
        chat_id = user["chat_id"]
        remaining = []
        for alarm in alarms:
            symbol = alarm["symbol"]
            target = alarm["target"]
            direction = alarm.get("direction", "auto")
            try:
                quote = await asyncio.to_thread(data_fetcher.get_price, symbol)
            except Exception:
                quote = None
            if not quote:
                remaining.append(alarm)
                continue

            price = quote["price"]
            triggered = (
                (direction == "above" and price >= target)
                or (direction == "below" and price <= target)
                or (direction == "auto" and abs(price - target) / target <= 0.005)
            )
            if triggered:
                msg = formats.price_alarm(symbol, price, target, quote.get("change_pct"))
                await _send(bot, chat_id, msg)
                logger.info("Alarm tetiklendi: %s/%s @%s", chat_id, symbol, target)
                # tek seferlik → kaldır
            else:
                remaining.append(alarm)

        if len(remaining) != len(alarms):
            user["alarms"] = remaining
            user_manager.save_user(user)


async def weekly_report_job(bot: Bot) -> None:
    """Haftalık rapor."""
    logger.info("Haftalık rapor görevi başladı.")
    try:
        snapshot = await asyncio.to_thread(data_fetcher.get_market_snapshot)
    except Exception:
        snapshot = {}
    txt = _snapshot_to_text(snapshot)
    snapshot_text = (
        f"🌍 Global: {txt['global']}\n🇹🇷 BIST: {txt['bist']}\n"
        f"💰 Döviz: {txt['fx']}\n🥇 Altın: {txt['gold']}\n₿ Kripto: {txt['crypto']}"
    )
    for user in user_manager.list_users():
        if not user.get("briefing_enabled", True):
            continue
        symbols = [h["symbol"] for h in user.get("portfolio", [])]
        try:
            prices = await asyncio.to_thread(data_fetcher.get_prices, symbols)
            commentary = await asyncio.to_thread(
                analyzer.generate_briefing_commentary, user, snapshot_text, prices
            )
        except Exception:
            commentary = "Haftalık değerlendirme üretilemedi."
        msg = formats.weekly_report(user.get("name", "Sen"), snapshot_text, commentary)
        await _send(bot, user["chat_id"], msg)
    logger.info("Haftalık rapor tamamlandı.")


async def news_scan_job(bot: Bot) -> None:
    """Haber tarama görevini news_scanner'a devreder."""
    try:
        await news_scanner.scan_all_users(bot)
    except Exception as exc:
        logger.error("Haber tarama görevi hatası: %s", exc)


# ─────────────────────────────────────────────
# Kurulum
# ─────────────────────────────────────────────

def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Tüm job'ları kaydedip yapılandırılmış scheduler'ı döndürür (start edilmez)."""
    scheduler = AsyncIOScheduler(timezone=_TZ)

    def _parse_hm(value: str, default: str) -> tuple[int, int]:
        try:
            h, m = (value or default).split(":")
            return int(h), int(m)
        except Exception:
            h, m = default.split(":")
            return int(h), int(m)

    mh, mm = _parse_hm(_sched_cfg.get("morning_briefing"), "08:00")
    eh, em = _parse_hm(_sched_cfg.get("evening_summary"), "18:00")
    wh, wm = _parse_hm(_sched_cfg.get("weekly_report_time"), "20:00")
    week_day = _sched_cfg.get("weekly_report_day", "sun")

    news_interval = _settings.get("news", {}).get("scan_interval_minutes", 120)
    alarm_interval = _settings.get("alarms", {}).get("check_interval_minutes", 15)

    # Her job ayrı ayrı eklenir
    scheduler.add_job(morning_briefing_job, "cron", hour=mh, minute=mm,
                      args=[bot], id="morning_briefing", replace_existing=True)
    scheduler.add_job(evening_summary_job, "cron", hour=eh, minute=em,
                      args=[bot], id="evening_summary", replace_existing=True)
    scheduler.add_job(weekly_report_job, "cron", day_of_week=week_day, hour=wh, minute=wm,
                      args=[bot], id="weekly_report", replace_existing=True)
    scheduler.add_job(news_scan_job, "interval", minutes=news_interval,
                      args=[bot], id="news_scan", replace_existing=True)
    scheduler.add_job(alarm_check_job, "interval", minutes=alarm_interval,
                      args=[bot], id="alarm_check", replace_existing=True)

    logger.info(
        "Scheduler kuruldu: brifing %02d:%02d, akşam %02d:%02d, haber/%ddk, alarm/%ddk, haftalık %s %02d:%02d",
        mh, mm, eh, em, news_interval, alarm_interval, week_day, wh, wm,
    )
    return scheduler
