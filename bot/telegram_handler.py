"""
Telegram komut işleyicileri.

Tüm bot komutları ve doğal dil yönlendirmesi burada tanımlanır.
`register_handlers(application)` ile python-telegram-bot uygulamasına bağlanır.

Komutlar:
  /basla   → kayıt + interaktif portföy kurulumu (ConversationHandler)
  /portfoy → portföy durumu (fiyat, K/Z, toplam değer)
  /haber   → sembol haberleri
  /analiz  → teknik + temel analiz
  /brifing → anlık piyasa brifingi
  /alarm   → fiyat alarmı kur
  /ekle    → portföye varlık ekle
  /cikar   → portföyden varlık çıkar
  /ayar    → bildirim tercihleri (inline butonlar)
  (diğer)  → doğal dil → Claude analizi
"""

import asyncio
from typing import List

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import formats
from core import analyzer, data_fetcher, user_manager
from utils.logger import get_logger

logger = get_logger(__name__)

# ConversationHandler durumları
ASK_NAME, ASK_RISK, ASK_PORTFOLIO = range(3)

RISK_OPTIONS = ["düşük", "orta", "orta-yüksek", "yüksek"]


async def _reply(update: Update, text: str, **kwargs) -> None:
    """Markdown denemesi, başarısızsa düz metin yedeği ile yanıt."""
    try:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except Exception:
        await update.effective_message.reply_text(text, **kwargs)


# ─────────────────────────────────────────────
# /basla — interaktif kurulum
# ─────────────────────────────────────────────

async def cmd_basla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_manager.create_user(chat_id, update.effective_user.first_name or "Kullanıcı")
    await _reply(
        update,
        "👋 Hoş geldin! Seni yatırım asistanına kaydediyorum.\n\n"
        "Önce sana nasıl hitap edeyim? (İsmini yaz)",
    )
    return ASK_NAME


async def setup_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()[:40]
    user_manager.update_setting(update.effective_chat.id, "name", name)
    keyboard = ReplyKeyboardMarkup([[r] for r in RISK_OPTIONS], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"Tamam {name}! Risk profilin nedir?", reply_markup=keyboard
    )
    return ASK_RISK


async def setup_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    risk = update.message.text.strip().lower()
    if risk not in RISK_OPTIONS:
        risk = "orta"
    user_manager.update_setting(update.effective_chat.id, "risk_profile", risk)
    await update.message.reply_text(
        "📦 Şimdi portföyünü kuralım.\n\n"
        "Her satıra bir varlık yaz: `SEMBOL ADET MALIYET`\n"
        "Örnek:\n`THYAO 100 250`\n`NVDA 10 120`\n`BTC 0.05 60000`\n\n"
        "Bitirince /tamam yaz. Portföysüz devam etmek için de /tamam.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_PORTFOLIO


async def setup_portfolio_line(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    added = 0
    for line in update.message.text.strip().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        sym, qty, cost = parts[0], parts[1], parts[2]
        try:
            user_manager.add_holding(chat_id, sym, float(qty), float(cost))
            added += 1
        except ValueError:
            await _reply(update, f"⚠️ Anlaşılmadı: `{line}` (sayı bekleniyordu)")
    if added:
        await _reply(update, f"✅ {added} varlık eklendi. Devam edebilir veya /tamam yazabilirsin.")
    else:
        await _reply(update, "Hiç varlık eklenmedi. Format: `SEMBOL ADET MALIYET`")
    return ASK_PORTFOLIO


async def setup_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(
        update,
        "🎉 Kurulum tamamlandı!\n\n"
        "Komutlar:\n"
        "/portfoy — portföy durumu\n"
        "/brifing — piyasa brifingi\n"
        "/analiz THYAO — analiz\n"
        "/haber NVDA — haberler\n"
        "/alarm BTC 70000 — fiyat alarmı\n"
        "/ekle, /cikar, /ayar\n\n"
        "Ayrıca bana normal cümleyle soru sorabilirsin 💬",
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("İptal edildi.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ─────────────────────────────────────────────
# /portfoy
# ─────────────────────────────────────────────

async def cmd_portfoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = user_manager.load_user(chat_id)
    if not user:
        await _reply(update, "Önce /basla ile kayıt olmalısın.")
        return
    await update.effective_chat.send_action("typing")
    symbols = [h["symbol"] for h in user.get("portfolio", [])]
    prices = await asyncio.to_thread(data_fetcher.get_prices, symbols)
    await _reply(update, formats.portfolio_summary(user, prices))


# ─────────────────────────────────────────────
# /haber [SEMBOL]
# ─────────────────────────────────────────────

async def cmd_haber(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(update, "Kullanım: `/haber THYAO`")
        return
    symbol = context.args[0].upper()
    await update.effective_chat.send_action("typing")
    articles = await asyncio.to_thread(data_fetcher.get_news_for_symbol, symbol)
    if not articles:
        await _reply(update, f"📭 {symbol} için güncel haber bulunamadı.")
        return
    lines = [f"📰 *{symbol} — Son Haberler*", "━━━━━━━━━━━━━━━━━━━━"]
    for a in articles:
        title = a["title"]
        link = a.get("link", "")
        lines.append(f"• [{title}]({link})" if link else f"• {title}")
    await _reply(update, "\n".join(lines), disable_web_page_preview=True)


# ─────────────────────────────────────────────
# /analiz [SEMBOL veya SEKTÖR]
# ─────────────────────────────────────────────

async def cmd_analiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(update, "Kullanım: `/analiz THYAO` veya `/analiz teknoloji`")
        return
    target = " ".join(context.args).upper()
    user = user_manager.load_user(update.effective_chat.id) or user_manager.create_user(update.effective_chat.id)
    await update.effective_chat.send_action("typing")

    price_data = await asyncio.to_thread(data_fetcher.get_price, target.split()[0])
    news = await asyncio.to_thread(data_fetcher.get_news_for_symbol, target.split()[0])
    result = await asyncio.to_thread(analyzer.analyze_symbol, target, user, price_data, news)
    await _reply(update, result)


# ─────────────────────────────────────────────
# /brifing
# ─────────────────────────────────────────────

async def cmd_brifing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = user_manager.load_user(update.effective_chat.id) or user_manager.create_user(update.effective_chat.id)
    await update.effective_chat.send_action("typing")
    from core import scheduler as sched  # döngüsel importu önlemek için yerel

    snapshot = await asyncio.to_thread(data_fetcher.get_market_snapshot)
    txt = sched._snapshot_to_text(snapshot)
    snapshot_text = (
        f"Global: {txt['global']} | BIST: {txt['bist']} | Döviz: {txt['fx']} | "
        f"Altın: {txt['gold']} | Kripto: {txt['crypto']}"
    )
    symbols = [h["symbol"] for h in user.get("portfolio", [])]
    prices = await asyncio.to_thread(data_fetcher.get_prices, symbols)
    commentary = await asyncio.to_thread(
        analyzer.generate_briefing_commentary, user, snapshot_text, prices
    )
    from datetime import datetime
    date_str = datetime.now(user_manager.TR_TZ).strftime("%d.%m.%Y")
    portfolio_line = commentary.split("DİKKAT")[0].strip()[:300]
    warning_line = commentary.split("DİKKAT")[-1].lstrip(":").strip()[:200] if "DİKKAT" in commentary else "Önemli uyarı yok."
    msg = formats.morning_briefing(
        date_str, txt["global"], txt["bist"], txt["fx"], txt["gold"],
        txt["crypto"], portfolio_line or "—", warning_line,
    )
    await _reply(update, msg)


# ─────────────────────────────────────────────
# /alarm [SEMBOL] [FIYAT]
# ─────────────────────────────────────────────

async def cmd_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await _reply(update, "Kullanım: `/alarm BTC 70000`")
        return
    symbol = context.args[0].upper()
    try:
        target = float(context.args[1])
    except ValueError:
        await _reply(update, "Fiyat sayısal olmalı. Örnek: `/alarm BTC 70000`")
        return

    chat_id = update.effective_chat.id
    # Mevcut fiyata göre yön belirle (üstüne mi altına mı)
    quote = await asyncio.to_thread(data_fetcher.get_price, symbol)
    direction = "auto"
    if quote:
        direction = "above" if target > quote["price"] else "below"
    user_manager.add_alarm(chat_id, symbol, target, direction)
    cur = f" (şu an {quote['price']:g})" if quote else ""
    await _reply(update, f"🚨 Alarm kuruldu: *{symbol}* {target:g}{cur}\nYön: {direction}")


# ─────────────────────────────────────────────
# /ekle [SEMBOL] [ADET] [MALIYET]
# ─────────────────────────────────────────────

async def cmd_ekle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await _reply(update, "Kullanım: `/ekle THYAO 100 250`")
        return
    symbol = context.args[0].upper()
    try:
        qty = float(context.args[1])
        cost = float(context.args[2])
    except ValueError:
        await _reply(update, "Adet ve maliyet sayısal olmalı.")
        return
    user_manager.add_holding(update.effective_chat.id, symbol, qty, cost)
    await _reply(update, f"✅ Eklendi: *{symbol}* — {qty:g} adet @ {cost:g}")


# ─────────────────────────────────────────────
# /cikar [SEMBOL]
# ─────────────────────────────────────────────

async def cmd_cikar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(update, "Kullanım: `/cikar THYAO`")
        return
    symbol = context.args[0].upper()
    if user_manager.remove_holding(update.effective_chat.id, symbol):
        await _reply(update, f"🗑️ *{symbol}* portföyden çıkarıldı.")
    else:
        await _reply(update, f"{symbol} portföyde bulunamadı.")


# ─────────────────────────────────────────────
# /ayar — bildirim tercihleri (inline butonlar)
# ─────────────────────────────────────────────

async def cmd_ayar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = user_manager.load_user(update.effective_chat.id)
    if not user:
        await _reply(update, "Önce /basla ile kayıt ol.")
        return
    pref = user.get("notification_preference", "high")
    brief = "açık" if user.get("briefing_enabled", True) else "kapalı"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Tüm haberler", callback_data="notif:all"),
         InlineKeyboardButton("⭐ Sadece önemli", callback_data="notif:high")],
        [InlineKeyboardButton("🔕 Bildirim kapalı", callback_data="notif:off")],
        [InlineKeyboardButton(f"☀️ Brifing: {brief} (değiştir)", callback_data="brief:toggle")],
    ])
    await update.effective_message.reply_text(
        f"⚙️ *Ayarlar*\nBildirim tercihi: *{pref}*\nBrifing: *{brief}*",
        reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data.startswith("notif:"):
        pref = data.split(":")[1]
        user_manager.update_setting(chat_id, "notification_preference", pref)
        await query.edit_message_text(f"✅ Bildirim tercihi güncellendi: *{pref}*", parse_mode=ParseMode.MARKDOWN)
    elif data == "brief:toggle":
        user = user_manager.load_user(chat_id)
        new_val = not user.get("briefing_enabled", True)
        user_manager.update_setting(chat_id, "briefing_enabled", new_val)
        await query.edit_message_text(
            f"✅ Brifing {'açıldı ☀️' if new_val else 'kapatıldı 🔕'}", parse_mode=ParseMode.MARKDOWN
        )


# ─────────────────────────────────────────────
# Doğal dil → Claude
# ─────────────────────────────────────────────

async def natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if not text:
        return
    chat_id = update.effective_chat.id
    user = user_manager.load_user(chat_id) or user_manager.create_user(chat_id)
    await update.effective_chat.send_action("typing")

    # TOOL USE: Claude gerektikçe canlı fiyat/haber/teknik çeker veya portföyü düzenler
    answer = await asyncio.to_thread(analyzer.chat_with_tools, text, user, chat_id)
    await _reply(update, answer)


# ─────────────────────────────────────────────
# Hata işleyici
# ─────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram işleyici hatası: %s", context.error, exc_info=context.error)


# ─────────────────────────────────────────────
# Kayıt
# ─────────────────────────────────────────────

def register_handlers(application: Application) -> None:
    """Tüm komut/işleyicileri uygulamaya bağlar."""
    # /basla konuşma akışı
    conv = ConversationHandler(
        entry_points=[CommandHandler("basla", cmd_basla)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_name)],
            ASK_RISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_risk)],
            ASK_PORTFOLIO: [
                CommandHandler("tamam", setup_done),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setup_portfolio_line),
            ],
        },
        fallbacks=[CommandHandler("iptal", cancel)],
    )
    application.add_handler(conv)

    application.add_handler(CommandHandler("portfoy", cmd_portfoy))
    application.add_handler(CommandHandler("haber", cmd_haber))
    application.add_handler(CommandHandler("analiz", cmd_analiz))
    application.add_handler(CommandHandler("brifing", cmd_brifing))
    application.add_handler(CommandHandler("alarm", cmd_alarm))
    application.add_handler(CommandHandler("ekle", cmd_ekle))
    application.add_handler(CommandHandler("cikar", cmd_cikar))
    application.add_handler(CommandHandler("ayar", cmd_ayar))
    application.add_handler(CallbackQueryHandler(settings_callback))

    # Komut olmayan tüm metinler → doğal dil
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language))

    application.add_error_handler(error_handler)
    logger.info("Telegram işleyicileri kaydedildi.")
