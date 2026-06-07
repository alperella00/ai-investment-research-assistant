"""
Veri çekme katmanı.

Kaynaklar:
  - yfinance      → hisse (BIST/ABD), döviz, altın, endeksler
  - CoinGecko     → kripto fiyatları (ücretsiz uç nokta)
  - RSS/feedparser→ KAP, Reuters, Investing haberleri

Tüm fonksiyonlar ağ hatalarına karşı try/except ile korunur, timeout
uygular ve hata durumunda None / boş liste döner (asla exception fırlatmaz).
Senkron yazılmıştır; async çağrılarda `asyncio.to_thread` ile sarmalanmalıdır.
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests
import yfinance as yf

from utils.config import get_env, get_settings
from utils.logger import get_logger

logger = get_logger(__name__)

_settings = get_settings()
_TIMEOUT = _settings.get("data", {}).get("request_timeout_seconds", 15)
_RATE_SLEEP = _settings.get("data", {}).get("rate_limit_sleep_seconds", 1.0)

# ─────────────────────────────────────────────
# Sembol eşleme tabloları
# ─────────────────────────────────────────────

# Kripto sembolleri → CoinGecko id
CRYPTO_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "XRP": "ripple", "ADA": "cardano",
    "DOGE": "dogecoin", "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "matic-network", "LINK": "chainlink", "TRX": "tron",
}

# Takma adlar → yfinance ticker (emtia/döviz)
ALIAS_MAP = {
    "ALTIN": "GC=F",     # Ons altın (vadeli)
    "GUMUS": "SI=F",     # Gümüş
    "DOLAR": "USDTRY=X",
    "USD": "USDTRY=X",
    "EURO": "EURTRY=X",
    "EUR": "EURTRY=X",
    "PETROL": "CL=F",
    "BRENT": "BZ=F",
}

# En çok takip edilen BIST sembolleri (otomatik .IS eki için)
BIST_SYMBOLS = {
    "THYAO", "ASELS", "GARAN", "AKBNK", "ISCTR", "YKBNK", "SISE",
    "EREGL", "KCHOL", "SAHOL", "BIMAS", "TUPRS", "PETKM", "FROTO",
    "TOASO", "TCELL", "PGSUS", "KRDMD", "HEKTS", "SASA", "TTKOM",
    "VESTL", "ARCLK", "ENKAI", "KOZAL", "KOZAA", "GUBRF", "ALARK",
}


def normalize_symbol(symbol: str) -> Tuple[str, str]:
    """
    Bir kullanıcı sembolünü (kind, ticker) çiftine çevirir.

    kind:
      'crypto' → CoinGecko id ticker'da
      'yf'     → yfinance ticker
    """
    s = symbol.strip().upper()

    if s in CRYPTO_MAP:
        return "crypto", CRYPTO_MAP[s]
    if s in ALIAS_MAP:
        return "yf", ALIAS_MAP[s]
    if s.endswith(".IS"):
        return "yf", s
    if s in BIST_SYMBOLS:
        return "yf", f"{s}.IS"
    # Döviz paritesi (USDTRY=X gibi) veya ham ticker
    return "yf", s


# ─────────────────────────────────────────────
# Fiyat çekme
# ─────────────────────────────────────────────

def _safe_fast_info(info: Any, *keys: str) -> Optional[Any]:
    """FastInfo sürüm farklarına dayanıklı erişim (key veya attribute)."""
    for key in keys:
        try:
            val = info[key]
            if val is not None:
                return val
        except (KeyError, TypeError, AttributeError, Exception):
            pass
        val = getattr(info, key, None)
        if val is not None:
            return val
    return None


def _get_yf_quote(ticker: str) -> Optional[Dict[str, Any]]:
    """yfinance üzerinden son fiyat + günlük değişim çeker."""
    try:
        t = yf.Ticker(ticker)
        price = prev = currency = None

        # fast_info hızlı ve hafiftir; sürüme göre erişimi koruyoruz
        try:
            info = t.fast_info
            price = _safe_fast_info(info, "last_price", "lastPrice")
            prev = _safe_fast_info(info, "previous_close", "previousClose")
            currency = _safe_fast_info(info, "currency")
        except Exception:
            pass

        if price is None:
            # Yedek: kısa geçmiş veri (sürümden bağımsız, güvenilir)
            hist = t.history(period="2d")
            if hist.empty:
                return None
            price = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[0]) if len(hist) > 1 else price

        price = float(price)
        prev = float(prev) if prev else None
        currency = currency or "USD"

        change_pct = None
        if prev:
            change_pct = round((price - prev) / prev * 100, 2)

        return {
            "ticker": ticker,
            "price": round(float(price), 4),
            "previous_close": round(float(prev), 4) if prev else None,
            "change_pct": change_pct,
            "currency": currency,
            "source": "yfinance",
        }
    except Exception as exc:  # ağ/parsing dahil her şeyi yutuyoruz
        logger.warning("yfinance fiyat alınamadı (%s): %s", ticker, exc)
        return None


def _get_crypto_quote(coingecko_id: str) -> Optional[Dict[str, Any]]:
    """CoinGecko üzerinden kripto fiyatı (USD) çeker."""
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": coingecko_id,
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    headers = {}
    api_key = get_env("COINGECKO_API_KEY")
    if api_key:
        headers["x-cg-pro-api-key"] = api_key

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get(coingecko_id, {})
        if not data:
            return None
        return {
            "ticker": coingecko_id,
            "price": round(float(data["usd"]), 2),
            "change_pct": round(float(data.get("usd_24h_change", 0)), 2),
            "currency": "USD",
            "source": "coingecko",
        }
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.warning("CoinGecko fiyat alınamadı (%s): %s", coingecko_id, exc)
        return None


def get_price(symbol: str) -> Optional[Dict[str, Any]]:
    """Tek bir sembol için normalize edip fiyat döndürür."""
    kind, ticker = normalize_symbol(symbol)
    time.sleep(_RATE_SLEEP)  # nazik rate-limit
    if kind == "crypto":
        quote = _get_crypto_quote(ticker)
    else:
        quote = _get_yf_quote(ticker)
    if quote:
        quote["symbol"] = symbol.upper()
    return quote


def get_prices(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Birden çok sembol için fiyat sözlüğü döndürür."""
    result: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        q = get_price(sym)
        if q:
            result[sym.upper()] = q
    return result


# ─────────────────────────────────────────────
# Piyasa anlık görünümü (brifing için)
# ─────────────────────────────────────────────

def get_market_snapshot() -> Dict[str, Dict[str, Any]]:
    """
    settings.yaml'daki endeksler için anlık görünüm üretir.
    Brifing mesajında kullanılır.
    """
    indices = _settings.get("market_indices", {})
    snapshot: Dict[str, Dict[str, Any]] = {}

    for category, items in indices.items():
        snapshot[category] = {}
        for label, ticker in items.items():
            if category == "crypto":
                q = _get_crypto_quote(ticker)
            else:
                q = _get_yf_quote(ticker)
            time.sleep(_RATE_SLEEP)
            if q:
                snapshot[category][label] = q
    return snapshot


# ─────────────────────────────────────────────
# Haber çekme
# ─────────────────────────────────────────────

def _parse_feed(url: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Bir RSS akışını ayrıştırır."""
    articles: List[Dict[str, Any]] = []
    try:
        # feedparser kendi içinde indirir; requests ile timeout sağlıyoruz
        resp = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:limit]:
            articles.append({
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip()[:500],
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "source": feed.feed.get("title", url),
            })
    except (requests.RequestException, Exception) as exc:
        logger.warning("RSS akışı okunamadı (%s): %s", url, exc)
    return articles


def get_news_for_symbol(symbol: str) -> List[Dict[str, Any]]:
    """
    Bir sembolle ilgili haberleri RSS kaynaklarından filtreleyerek getirir.
    Basit anahtar kelime eşleşmesi kullanılır (başlık/özet içinde sembol geçiyorsa).
    """
    sources = _settings.get("news_sources", {}).get("rss", [])
    max_articles = _settings.get("news", {}).get("max_articles_per_symbol", 5)

    keyword = symbol.upper().replace(".IS", "")
    # Kriptolar için tam isim de atransak
    aliases = [keyword]
    kind, ticker = normalize_symbol(symbol)
    if kind == "crypto":
        aliases.append(ticker.upper())  # ör. BITCOIN

    matched: List[Dict[str, Any]] = []
    for url in sources:
        for art in _parse_feed(url):
            haystack = f"{art['title']} {art['summary']}".upper()
            if any(alias in haystack for alias in aliases):
                matched.append(art)
            if len(matched) >= max_articles:
                break
        if len(matched) >= max_articles:
            break
    logger.debug("%s için %d haber bulundu", symbol, len(matched))
    return matched[:max_articles]


def get_general_news(limit: int = 10) -> List[Dict[str, Any]]:
    """Brifing için genel piyasa haberleri."""
    sources = _settings.get("news_sources", {}).get("general_rss", [])
    articles: List[Dict[str, Any]] = []
    for url in sources:
        articles.extend(_parse_feed(url, limit=limit))
    return articles[:limit]
