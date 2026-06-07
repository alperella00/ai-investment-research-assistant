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
    "OTKAR", "AYGAZ", "KORDS", "BRISA", "CIMSA", "AKSEN", "OYAKC",
}

# Holding grupları → bağlı BIST hisseleri (KESİN, model tahmin etmesin)
# Not: ASELSAN bu gruplardan hiçbirine ait DEĞİLDİR (TSKGV/Savunma vakfı).
HOLDING_GROUPS = {
    "KOÇ": ["KCHOL", "FROTO", "TOASO", "ARCLK", "TUPRS", "OTKAR", "AYGAZ", "TATGD"],
    "SABANCI": ["SAHOL", "AKBNK", "KORDS", "BRISA", "CIMSA", "AKSA", "AKSEN", "ENKAI"],
    "ŞIŞECAM": ["SISE", "TRKCM", "SODA"],
    "OYAK": ["OYAKC", "EREGL", "KRDMD"],
    "DOĞUŞ": ["DOAS"],
    "ECZACIBAŞI": ["ECILC", "ECZYT"],
}

# Sembol → haber aramasında kullanılacak şirket adı/takma adları
SYMBOL_ALIASES = {
    "THYAO": ["TÜRK HAVA YOLLARI", "THY"],
    "ASELS": ["ASELSAN"],
    "KCHOL": ["KOÇ HOLDING", "KOÇ"],
    "TOASO": ["TOFAŞ", "TOFAS"],
    "FROTO": ["FORD OTOSAN", "FORD OTO"],
    "ARCLK": ["ARÇELİK", "ARCELIK", "BEKO"],
    "TUPRS": ["TÜPRAŞ", "TUPRAS"],
    "OTKAR": ["OTOKAR"],
    "AYGAZ": ["AYGAZ"],
    "SAHOL": ["SABANCI HOLDING", "SABANCI"],
    "AKBNK": ["AKBANK"],
    "GARAN": ["GARANTİ BBVA", "GARANTİ BANKASI", "GARANTI"],
    "ISCTR": ["İŞ BANKASI", "IS BANKASI", "İŞBANK"],
    "SISE": ["ŞİŞECAM", "SISECAM"],
    "EREGL": ["EREĞLİ DEMİR", "ERDEMİR"],
    "BIMAS": ["BİM"],
    "PGSUS": ["PEGASUS"],
    "SASA": ["SASA POLYESTER"],
}


def symbol_aliases(symbol: str) -> List[str]:
    """Bir sembol için haber aramasında kullanılacak anahtar kelimeler."""
    s = symbol.strip().upper().replace(".IS", "")
    aliases = [s]
    aliases.extend(SYMBOL_ALIASES.get(s, []))
    kind, ticker = normalize_symbol(symbol)
    if kind == "crypto":
        aliases.append(ticker.upper())  # ör. BITCOIN
    return aliases


def get_group_members(group: str) -> List[str]:
    """Bir holding grubunun BIST hisselerini döndürür (kesin tablo)."""
    key = group.strip().upper().replace(" HOLDING", "").replace(" GRUBU", "").strip()
    # Türkçe büyük harf normalizasyonu için basit eşleme
    for gname, members in HOLDING_GROUPS.items():
        if key in gname or gname in key:
            return [m.strip() for m in members]
    return []


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
    """
    yfinance üzerinden son fiyat + günlük değişim çeker.

    ÖNEMLİ: fast_info["previous_close"] güvenilmez (bazen dünkü gerçek kapanışı
    değil alakasız bir değer döndürür → yanlış % değişim). Bu yüzden referans
    kapanışı her zaman GEÇMİŞ VERİDEN (dünkü resmi kapanış) hesaplıyoruz.
    """
    try:
        t = yf.Ticker(ticker)

        # Referans = dünkü resmi kapanış; güncel = bugünkü kapanış/son işlem
        hist = t.history(period="5d")
        if hist.empty:
            return None
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        price = float(closes.iloc[-1])
        prev = float(closes.iloc[-2]) if len(closes) >= 2 else price
        currency = "USD"

        # fast_info ile (varsa) daha güncel anlık fiyatı ve para birimini al
        try:
            info = t.fast_info
            live = _safe_fast_info(info, "last_price", "lastPrice")
            if live:
                price = float(live)
            cur = _safe_fast_info(info, "currency")
            if cur:
                currency = cur
        except Exception:
            pass

        change_pct = round((price - prev) / prev * 100, 2) if prev else None
        # Verinin tazeliği: son barın tarihi (yfinance ~15 dk gecikmeli olabilir)
        as_of = str(closes.index[-1].date()) if len(closes) else None

        return {
            "ticker": ticker,
            "price": round(price, 4),
            "previous_close": round(prev, 4),
            "change_pct": change_pct,
            "currency": currency,
            "as_of": as_of,
            "note": "Veri ~15 dk gecikmeli olabilir; seans sonrası hareketleri içermez.",
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


def get_technicals(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Bir sembol için temel teknik göstergeleri hesaplar:
    RSI(14), SMA20/50/200, son fiyatın ortalamalara göre konumu, hacim eğilimi.
    """
    kind, ticker = normalize_symbol(symbol)
    try:
        if kind == "crypto":
            # Kripto için yfinance USD paritesini dene (BTC-USD gibi)
            ticker = f"{symbol.upper()}-USD"
        t = yf.Ticker(ticker)
        hist = t.history(period="1y")
        if hist.empty or len(hist) < 20:
            return None

        close = hist["Close"]
        volume = hist["Volume"]
        last = float(close.iloc[-1])

        # RSI(14)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(window=14).mean()
        loss = (-delta.clip(upper=0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi_series = 100 - (100 / (1 + rs))
        rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else None

        def sma(n: int) -> Optional[float]:
            if len(close) >= n:
                return round(float(close.rolling(window=n).mean().iloc[-1]), 2)
            return None

        sma20, sma50, sma200 = sma(20), sma(50), sma(200)

        # Hacim eğilimi: son 5 gün ortalaması vs 20 gün ortalaması
        vol_trend = None
        if len(volume) >= 20:
            v5 = float(volume.tail(5).mean())
            v20 = float(volume.tail(20).mean())
            if v20:
                vol_trend = round((v5 - v20) / v20 * 100, 1)

        def pos(ma: Optional[float]) -> Optional[float]:
            return round((last - ma) / ma * 100, 1) if ma else None

        return {
            "symbol": symbol.upper(),
            "last": round(last, 4),
            "rsi14": round(rsi, 1) if rsi is not None else None,
            "sma20": sma20, "sma50": sma50, "sma200": sma200,
            "vs_sma50_pct": pos(sma50),     # +ise 50 günlük ortalamanın üstünde
            "vs_sma200_pct": pos(sma200),
            "volume_trend_pct": vol_trend,  # +ise hacim artıyor
        }
    except Exception as exc:
        logger.warning("Teknik gösterge hesaplanamadı (%s): %s", symbol, exc)
        return None


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
    Sembol kodunun yanında ŞİRKET ADINI da arar (ör. KCHOL → 'Koç Holding'),
    böylece haberlerde kod değil isim geçtiğinde de yakalanır.
    """
    max_articles = _settings.get("news", {}).get("max_articles_per_symbol", 5)
    aliases = [a.upper() for a in symbol_aliases(symbol)]
    return search_news(aliases, max_articles=max_articles)


def search_news(query, max_articles: int = 8) -> List[Dict[str, Any]]:
    """
    Serbest metin haber araması. query bir string veya anahtar kelime listesidir;
    başlık/özette herhangi biri geçen haberleri döndürür. Şirket/grup/tema
    haberlerini (ör. 'Koç', 'yapay zeka', 'faiz') bulmak için kullanılır.
    """
    sources = _settings.get("news_sources", {}).get("rss", [])
    sources = sources + _settings.get("news_sources", {}).get("general_rss", [])
    terms = [query] if isinstance(query, str) else list(query)
    terms = [t.upper() for t in terms if t]

    matched: List[Dict[str, Any]] = []
    seen_titles = set()
    for url in sources:
        for art in _parse_feed(url):
            haystack = f"{art['title']} {art['summary']}".upper()
            if any(term in haystack for term in terms):
                if art["title"] in seen_titles:
                    continue
                seen_titles.add(art["title"])
                matched.append(art)
            if len(matched) >= max_articles:
                break
        if len(matched) >= max_articles:
            break
    logger.debug("Haber araması %s → %d sonuç", terms, len(matched))
    return matched[:max_articles]


def get_general_news(limit: int = 10) -> List[Dict[str, Any]]:
    """Brifing için genel piyasa haberleri."""
    sources = _settings.get("news_sources", {}).get("general_rss", [])
    articles: List[Dict[str, Any]] = []
    for url in sources:
        articles.extend(_parse_feed(url, limit=limit))
    return articles[:limit]
