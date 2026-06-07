"""
Telegram mesaj şablonları.

Bildirim formatları (haber, brifing, alarm) burada üretilir; hem komut
işleyiciler hem de zamanlanmış görevler bu yardımcıları kullanır.
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

TR_TZ = timezone(timedelta(hours=3))
_SEP = "━━━━━━━━━━━━━━━━━━━━"


def _now_str() -> str:
    return datetime.now(TR_TZ).strftime("%d.%m.%Y %H:%M")


def news_notification(user_name: str, symbol: str, article: Dict[str, Any], impact: str, comment: str) -> str:
    """Haber bildirimi şablonu."""
    published = article.get("published") or _now_str()
    return (
        f"🔴 {user_name} — {symbol} HABERİ\n"
        f"{_SEP}\n"
        f"📰 {article.get('title', '')}\n"
        f"⚡ Etki: {impact}\n"
        f"📊 Kısa vade beklentisi: {comment}\n"
        f"🕐 {published}"
    )


def morning_briefing(date_str: str, global_txt: str, bist_txt: str,
                      fx_txt: str, gold_txt: str, crypto_txt: str,
                      portfolio_txt: str, warning_txt: str) -> str:
    """Sabah brifing şablonu."""
    return (
        f"☀️ GÜNLÜK BRİFİNG — {date_str}\n"
        f"{_SEP}\n"
        f"🌍 GLOBAL: {global_txt}\n"
        f"🇹🇷 BIST: {bist_txt}\n"
        f"💰 DÖVİZ: {fx_txt}\n"
        f"🥇 ALTIN: {gold_txt}\n"
        f"₿ KRİPTO: {crypto_txt}\n"
        f"{_SEP}\n"
        f"📋 PORTFÖYÜN: {portfolio_txt}\n"
        f"⚠️ DİKKAT: {warning_txt}"
    )


def price_alarm(symbol: str, price: float, target: float, change_pct: Any) -> str:
    """Fiyat alarmı şablonu."""
    change = f"{change_pct:+}%" if isinstance(change_pct, (int, float)) else "—"
    return (
        f"🚨 FİYAT ALARMI\n"
        f"{symbol}: {price} seviyesine ulaştı!\n"
        f"Alarm: {target}\n"
        f"Değişim: {change}"
    )


def portfolio_summary(user: Dict[str, Any], prices: Dict[str, Any]) -> str:
    """/portfoy komutu için tablo benzeri özet."""
    holdings = user.get("portfolio", [])
    if not holdings:
        return "📭 Portföyün boş. /ekle ile varlık ekleyebilirsin."

    lines = [f"📊 *{user.get('name', 'Portföy')}* — Portföy Durumu", _SEP]
    total_value = 0.0
    total_cost = 0.0

    for h in holdings:
        sym = h["symbol"].upper()
        qty = h["quantity"]
        cost = h["cost"]
        p = prices.get(sym)
        # Canlı fiyat yoksa elle girilen manuel fiyatı (egzotik enstrüman) kullan
        price = None
        currency = ""
        if p:
            price = p["price"]
            currency = p.get("currency", "")
        elif h.get("manual_price") is not None:
            price = h["manual_price"]
            currency = "(manuel)"

        if price is not None:
            value = price * qty
            cost_total = cost * qty
            pnl = value - cost_total
            pnl_pct = (pnl / cost_total * 100) if cost_total else 0
            total_value += value
            total_cost += cost_total
            emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{emoji} *{sym}* | {qty:g} ad | güncel {price:g} {currency}\n"
                f"   K/Z: {pnl:+.2f} ({pnl_pct:+.1f}%)"
            )
        else:
            lines.append(f"⚪ *{sym}* | {qty:g} ad | fiyat alınamadı")

    if total_cost:
        total_pnl = total_value - total_cost
        total_pct = total_pnl / total_cost * 100
        lines.append(_SEP)
        lines.append(
            f"💼 Toplam değer: *{total_value:,.2f}*\n"
            f"📈 Toplam K/Z: *{total_pnl:+,.2f}* ({total_pct:+.1f}%)"
        )
    return "\n".join(lines)


def weekly_report(user_name: str, snapshot_txt: str, commentary: str) -> str:
    """Haftalık rapor şablonu."""
    return (
        f"📅 HAFTALIK RAPOR — {user_name}\n"
        f"{_SEP}\n"
        f"{snapshot_txt}\n"
        f"{_SEP}\n"
        f"{commentary}"
    )
