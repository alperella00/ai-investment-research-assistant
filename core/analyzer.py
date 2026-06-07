"""
Claude (Anthropic) entegrasyonu.

Tüm doğal dil analizleri, haber puanlama ve brifing metinleri buradan geçer.
Maliyet optimizasyonu için Haiku 4.5 kullanılır ve sabit system prompt
prompt-caching ile önbelleğe alınır. Token kullanımı her çağrıda loglanır.
"""

from typing import Any, Dict, List, Optional

from anthropic import Anthropic, APIError

from utils.config import get_env, get_settings
from utils.logger import get_logger

logger = get_logger(__name__)

_settings = get_settings()
_claude_cfg = _settings.get("claude", {})

MODEL = _claude_cfg.get("model", "claude-haiku-4-5-20251001")
MAX_TOKENS = _claude_cfg.get("max_tokens", 1024)
TEMPERATURE = _claude_cfg.get("temperature", 0.4)

# Sabit system prompt — prompt caching'e uygun (değişmez)
SYSTEM_PROMPT = """Sen deneyimli bir finansal araştırma asistanısın. Türk ve global piyasaları takip ediyorsun.

GÖREVLER:
- Kullanıcının portföyü bağlamında piyasa analizi yap
- Haberlerin portföy üzerindeki etkisini değerlendir
- Teknik göstergeler (RSI, hareketli ortalama, hacim) hakkında yorum yap
- Makroekonomik gelişmelerin etkisini analiz et

KURALLAR:
- Asla kesin "al" veya "sat" demeyeceksin. "Değerlendirilebilir", "risk içeriyor", "fırsat olabilir" gibi ifadeler kullan
- Her analizde hem olumlu hem olumsuz senaryoyu belirt
- Kaynaklarını ve verilerin tarihini belirt
- Belirsiz olduğun konularda bunu açıkça söyle
- Kısa ve öz cevaplar ver, gereksiz tekrar yapma

FORMAT:
- Emoji kullan (📈📉🔴🟢⚠️)
- Önce 1 satır özet, sonra detay
- Sayısal verileri vurgula
- Telegram'da okunacak şekilde formatla (Markdown)

DİL:
- Türkçe, samimi ama profesyonel
- Teknik terimleri kullan ama gerektiğinde açıkla"""


# Tekil Anthropic istemcisi (API anahtarı .env'den okunur)
_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    """Tembel (lazy) istemci kurulumu."""
    global _client
    if _client is None:
        api_key = get_env("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY tanımlı değil (.env kontrol edin).")
        _client = Anthropic(api_key=api_key)
    return _client


def _log_usage(label: str, usage: Any) -> None:
    """Token kullanımını loglar (maliyet takibi)."""
    try:
        logger.info(
            "[Claude:%s] giriş=%s çıkış=%s önbellek_yaz=%s önbellek_oku=%s",
            label,
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
            getattr(usage, "cache_creation_input_tokens", 0),
            getattr(usage, "cache_read_input_tokens", 0),
        )
    except Exception:
        pass


def _format_portfolio(user: Dict[str, Any], prices: Optional[Dict[str, Any]] = None) -> str:
    """Portföyü Claude'a verilecek okunabilir metne çevirir."""
    if not user.get("portfolio"):
        return "Portföy boş."
    lines = [f"Risk profili: {user.get('risk_profile', 'bilinmiyor')}"]
    for h in user["portfolio"]:
        sym = h["symbol"]
        line = f"- {sym}: {h['quantity']} adet, ort. maliyet {h['cost']}"
        if prices and sym.upper() in prices:
            p = prices[sym.upper()]
            line += f", güncel {p['price']} {p.get('currency', '')}"
            if p.get("change_pct") is not None:
                line += f" ({p['change_pct']:+}%)"
        lines.append(line)
    return "\n".join(lines)


def _call_claude(
    user_content: str,
    label: str = "analiz",
    max_tokens: Optional[int] = None,
) -> str:
    """
    Tek seferlik Claude çağrısı. System prompt önbelleğe alınır.
    Hata durumunda kullanıcı dostu bir mesaj döner.
    """
    try:
        client = _get_client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens or MAX_TOKENS,
            temperature=TEMPERATURE,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # prompt caching
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        _log_usage(label, resp.usage)
        return "".join(block.text for block in resp.content if block.type == "text").strip()
    except APIError as exc:
        logger.error("Claude API hatası (%s): %s", label, exc)
        return "⚠️ Analiz şu anda yapılamıyor (API hatası). Lütfen biraz sonra tekrar deneyin."
    except Exception as exc:
        logger.error("Beklenmeyen Claude hatası (%s): %s", label, exc)
        return "⚠️ Beklenmeyen bir hata oluştu."


# ─────────────────────────────────────────────
# Genel amaçlı analiz (doğal dil sorular)
# ─────────────────────────────────────────────

def analyze_question(
    question: str,
    user: Dict[str, Any],
    prices: Optional[Dict[str, Any]] = None,
    extra_context: str = "",
) -> str:
    """Kullanıcının serbest sorusunu portföy bağlamında yanıtlar."""
    portfolio_txt = _format_portfolio(user, prices)
    market_block = ""
    if extra_context:
        market_block = f"PİYASA VERİSİ:\n{extra_context}\n\n"
    content = (
        f"KULLANICI PORTFÖYÜ:\n{portfolio_txt}\n\n"
        f"{market_block}"
        f"SORU:\n{question}"
    )
    return _call_claude(content, label="soru")


def analyze_symbol(
    symbol: str,
    user: Dict[str, Any],
    price_data: Optional[Dict[str, Any]] = None,
    news: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Belirli bir sembol/sektör için teknik + temel analiz özeti."""
    parts = [f"ANALİZ İSTENEN: {symbol}"]
    if price_data:
        parts.append(
            f"Güncel fiyat: {price_data['price']} {price_data.get('currency', '')} "
            f"(günlük {price_data.get('change_pct', '?')}%)"
        )
    if news:
        headlines = "\n".join(f"- {a['title']}" for a in news[:5])
        parts.append(f"İlgili son haberler:\n{headlines}")
    parts.append(f"\nKULLANICININ RİSK PROFİLİ: {user.get('risk_profile', 'orta')}")
    parts.append(
        "\nLütfen bu sembol için kısa bir teknik + temel analiz özeti ver. "
        "Olumlu ve olumsuz senaryoları belirt."
    )
    return _call_claude("\n".join(parts), label="sembol-analiz")


# ─────────────────────────────────────────────
# Haber önem puanlama
# ─────────────────────────────────────────────

def score_news_importance(article: Dict[str, Any], user: Dict[str, Any], symbol: str) -> int:
    """
    Bir haberin kullanıcının portföyü için önemini 1-10 arası puanlar.
    Yalnızca tek bir sayı döndürmesi istenir; ayrıştırılamazsa 0 döner.
    """
    portfolio_syms = ", ".join(h["symbol"] for h in user.get("portfolio", []))
    content = (
        f"Kullanıcı portföyü: {portfolio_syms}\n"
        f"İlgili sembol: {symbol}\n"
        f"Risk profili: {user.get('risk_profile', 'orta')}\n\n"
        f"HABER BAŞLIĞI: {article.get('title', '')}\n"
        f"HABER ÖZETİ: {article.get('summary', '')}\n\n"
        "Bu haber bu kullanıcının portföyü için ne kadar önemli? "
        "SADECE 1-10 arası tek bir tam sayı yaz, başka hiçbir şey yazma."
    )
    raw = _call_claude(content, label="haber-puan", max_tokens=10)
    # Yanıttan ilk sayıyı çıkar
    digits = "".join(c for c in raw if c.isdigit())
    try:
        score = int(digits[:2]) if digits else 0
        return max(0, min(10, score))
    except ValueError:
        return 0


# ─────────────────────────────────────────────
# Brifing / özet metinleri
# ─────────────────────────────────────────────

def generate_briefing_commentary(
    user: Dict[str, Any],
    snapshot_text: str,
    portfolio_prices: Dict[str, Any],
) -> str:
    """Sabah brifingi için kısa portföy yorumu + dikkat notu üretir."""
    portfolio_txt = _format_portfolio(user, portfolio_prices)
    content = (
        f"PİYASA ÖZETİ:\n{snapshot_text}\n\n"
        f"KULLANICI PORTFÖYÜ:\n{portfolio_txt}\n\n"
        "Yukarıdaki verilere göre kullanıcının portföyü için 2-3 cümlelik kısa bir "
        "değerlendirme ve varsa 1 satır 'DİKKAT' notu yaz. Çok kısa tut."
    )
    return _call_claude(content, label="brifing", max_tokens=400)
