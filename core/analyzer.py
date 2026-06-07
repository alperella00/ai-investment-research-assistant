"""
Claude (Anthropic) entegrasyonu.

Tüm doğal dil analizleri, haber puanlama ve brifing metinleri buradan geçer.
Sohbet/analiz için akıllı model (Sonnet) ve TOOL USE kullanılır: Claude canlı
fiyat/haber/teknik gösterge çekebilir ve portföye ekleme/çıkarma, alarm kurma
gibi işlemleri kendisi yapabilir. Haber puanlama maliyet için Haiku'da kalır.
Sabit system prompt prompt-caching ile önbelleğe alınır; token kullanımı loglanır.
"""

import json
from typing import Any, Dict, List, Optional

from anthropic import Anthropic, APIError

from core import data_fetcher, user_manager
from utils.config import get_env, get_settings
from utils.logger import get_logger

logger = get_logger(__name__)

_settings = get_settings()
_claude_cfg = _settings.get("claude", {})

# Göreve göre model seçimi
CHAT_MODEL = _claude_cfg.get("chat_model", "claude-sonnet-4-6")
SCORING_MODEL = _claude_cfg.get("scoring_model", "claude-haiku-4-5-20251001")
MODEL = _claude_cfg.get("model", "claude-haiku-4-5-20251001")  # brifing/özet
MAX_TOKENS = _claude_cfg.get("max_tokens", 1024)
TEMPERATURE = _claude_cfg.get("temperature", 0.4)
MAX_TOOL_ITER = _claude_cfg.get("max_tool_iterations", 6)

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
    model: Optional[str] = None,
) -> str:
    """
    Tek seferlik Claude çağrısı (tool yok). System prompt önbelleğe alınır.
    Hata durumunda kullanıcı dostu bir mesaj döner.
    """
    try:
        client = _get_client()
        resp = client.messages.create(
            model=model or MODEL,
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
# TOOL USE — Claude'a "el" veren araçlar
# ─────────────────────────────────────────────

# Sohbet için system prompt: temel prompt + araç kullanım yönergesi
CHAT_SYSTEM = SYSTEM_PROMPT + """

ARAÇLAR (TOOL USE):
Elinde GERÇEK araçlar var. Bir veriyi tahmin etmek yerine araçla çek:
- get_price: bir sembolün anlık fiyatı (THYAO, NVDA, BTC, ALTIN, DOLAR...)
- get_technicals: RSI(14), hareketli ortalamalar (SMA50/200), hacim eğilimi
- get_news: sembolle ilgili son haber başlıkları
- get_portfolio: kullanıcının güncel portföyü ve değerleri

Kullanıcı portföyünü DEĞİŞTİRMEK isterse araçlarla bizzat YAP:
- add_holding: varlık ekle (symbol, quantity, cost). Eğer sembol
  yfinance/CoinGecko'da yoksa get_price boş/None döner; bu durumda kullanıcıdan
  GÜNCEL FİYATI iste ve manual_price parametresiyle ekle. Borsa İstanbul'un
  egzotik enstrümanları (ör. Darphane Altın Sertifikası, gram altına endeksli
  ürünler) çoğu kez veri kaynaklarında yoktur → manual_price kullan.
- remove_holding: varlık çıkar
- set_alarm: fiyat alarmı kur (symbol, target_price)

KURALLAR:
- Tanımadığın bir sembolü önce get_price ile dene; bulamazsan kullanıcıya sembolün
  ne olduğunu ve güncel fiyatını sor, sonra manual_price ile ekle.
- İşlem (ekleme/çıkarma/alarm) yaptıktan sonra ne yaptığını kısaca onayla.

GRUP/ŞİRKET KURALI (ÇOK ÖNEMLİ):
- "Koç grubu / Sabancı holding / şu grubun hisseleri" gibi sorularda hangi
  hisselerin o gruba ait olduğunu ASLA ezberden söyleme → get_group_members çağır.
  (Örn. ASELSAN hiçbir holdinge ait değildir, bir savunma kuruluşudur. Tofaş/Ford
  Otosan/Arçelik/Tüpraş Koç'tur.) Tablo boş dönerse uydurma, kullanıcıya söyle.
- Önceki mesajın konusunu (ör. az önce ASELSAN konuştuk) yeni soruya TAŞIMA.
  Kullanıcı yeni bir grup/şirket sorduysa SADECE onunla ilgilen.

HABER KURALI:
- "Haber var mı / son gelişmeler" sorularında search_news'i ŞİRKET/GRUP ADIYLA çağır
  (sembol koduyla değil): ör. search_news("Koç"), search_news("Tofaş").
  Holding sorusunda hem grup adını hem üye şirket adlarını aratabilirsin.
- Haber bulamazsan dürüst ol: "kaynaklarımda şu an bu konuda haber görünmüyor" de,
  haber UYDURMA.

FİYAT/SAYI KURALI (ÇOK ÖNEMLİ):
- Bir hisse/kripto/emtianın fiyatını, yüzde değişimini veya teknik seviyesini
  ASLA ezberden/hafızandan yazma. Bir sayı vereceksen ÖNCE get_price veya
  get_technicals çağır. Aracı çağırmadan fiyat/yüzde söylemek YASAK.
- Birden çok sembolden bahsedeceksen (ör. yarı iletkenler: NVDA, AMD, ASML),
  HER BİRİ için ayrı get_price çağır.
- Verdiğin her fiyatın yanında tarihini/tazeliğini belirt (araç sonucundaki
  'as_of' alanı). Verinin ~15 dk gecikmeli ve seans-içi/kapanış verisi olduğunu,
  seans sonrası (after-hours) hareketleri içermeyebileceğini unutma.

KULLANICI VERİYİ 'YANLIŞ' DERSE:
- Sadece 'haklısın' deyip aynı sayıyı TEKRAR YAZMA. Önce get_price'ı YENİDEN çağır.
- Veri yine aynıysa, bunun nedeni büyük ihtimalle: (a) yfinance ~15 dk gecikmeli,
  (b) bizim verimiz seans kapanışını gösterir, kullanıcı ise canlı/after-hours
  fiyata bakıyor olabilir. Bunu dürüstçe açıkla; uydurma bir sayıyla 'düzeltme' yapma.
- Kullanıcı doğru fiyatı söylerse, onu temel alarak yorum yapabilirsin ama
  'benim verim şunu gösteriyor, seninki güncel olabilir' diye ayrımı net belirt."""


# Anthropic tool tanımları
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_price",
        "description": "Bir sembolün anlık fiyatını ve günlük değişimini getirir. "
                       "Bulamazsa boş döner (sembol veri kaynağında yok demektir).",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Sembol, ör. THYAO, NVDA, BTC, ALTIN"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_technicals",
        "description": "Bir sembol için teknik göstergeleri (RSI14, SMA50/200, hacim eğilimi) hesaplar.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_news",
        "description": "Bir sembolle ilgili son haber başlıklarını getirir.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "search_news",
        "description": "Serbest metinle haber arar. Şirket/grup/aile/tema haberleri için "
                       "ŞİRKET ADIYLA ara (ör. 'Koç', 'Tofaş', 'Akbank', 'yapay zeka', 'faiz'). "
                       "Sembol koduyla değil, isimle aramak daha çok sonuç bulur.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Arama terimi, ör. 'Koç Holding'"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_group_members",
        "description": "Bir Türk holding grubunun (Koç, Sabancı, Şişecam, Oyak...) bağlı BIST "
                       "hisselerini KESİN tablodan döndürür. Grup/sektör sorularında hisseleri "
                       "tahmin etmek yerine MUTLAKA bunu kullan. Boş dönerse grubu tanımıyoruz demektir.",
        "input_schema": {
            "type": "object",
            "properties": {"group": {"type": "string", "description": "Grup adı, ör. 'Koç', 'Sabancı'"}},
            "required": ["group"],
        },
    },
    {
        "name": "get_portfolio",
        "description": "Kullanıcının güncel portföyünü ve canlı değerlerini döndürür.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_holding",
        "description": "Portföye varlık ekler (veya mevcut varlığı maliyet ortalamasıyla günceller). "
                       "Sembol veri kaynağında yoksa manual_price ZORUNLU.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "quantity": {"type": "number", "description": "Adet/miktar"},
                "cost": {"type": "number", "description": "Birim maliyet (alış fiyatı)"},
                "manual_price": {"type": "number", "description": "Veri kaynağında olmayan enstrüman için elle girilen güncel fiyat"},
                "note": {"type": "string", "description": "Enstrüman açıklaması, ör. 'BIST Darphane Altın Sertifikası'"},
            },
            "required": ["symbol", "quantity", "cost"],
        },
    },
    {
        "name": "remove_holding",
        "description": "Portföyden bir varlığı tümüyle çıkarır.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "set_alarm",
        "description": "Bir sembol için hedef fiyat alarmı kurar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "target_price": {"type": "number"},
            },
            "required": ["symbol", "target_price"],
        },
    },
]


def _execute_tool(name: str, tool_input: Dict[str, Any], chat_id: int | str) -> Dict[str, Any]:
    """Bir tool çağrısını gerçek fonksiyonlara bağlar; sonuç JSON-uyumlu döner."""
    try:
        if name == "get_price":
            q = data_fetcher.get_price(tool_input["symbol"])
            return q or {"bulundu": False, "mesaj": "Bu sembol veri kaynağında yok. "
                                                     "Kullanıcıdan güncel fiyat alıp manual_price ile eklemen gerekir."}
        if name == "get_technicals":
            t = data_fetcher.get_technicals(tool_input["symbol"])
            return t or {"bulundu": False, "mesaj": "Teknik veri hesaplanamadı."}
        if name == "get_news":
            arts = data_fetcher.get_news_for_symbol(tool_input["symbol"])
            return {"haberler": [a["title"] for a in arts[:5]]} if arts else {"haberler": []}
        if name == "search_news":
            arts = data_fetcher.search_news(tool_input["query"])
            return {"haberler": [a["title"] for a in arts]} if arts else {"haberler": [], "mesaj": "Bu konuda haber bulunamadı (kaynaklarımız sınırlı)."}
        if name == "get_group_members":
            members = data_fetcher.get_group_members(tool_input["group"])
            if members:
                return {"grup": tool_input["group"], "hisseler": members}
            return {"grup": tool_input["group"], "hisseler": [],
                    "mesaj": "Bu grubu kesin tablomuzda bulamadık; üyelerini uydurma, kullanıcıya belirt."}
        if name == "get_portfolio":
            user = user_manager.load_user(chat_id) or {}
            syms = [h["symbol"] for h in user.get("portfolio", [])]
            prices = data_fetcher.get_prices(syms) if syms else {}
            holdings = []
            for h in user.get("portfolio", []):
                p = prices.get(h["symbol"].upper())
                holdings.append({
                    "symbol": h["symbol"], "quantity": h["quantity"], "cost": h["cost"],
                    "current_price": (p["price"] if p else h.get("manual_price")),
                    "manual": p is None and h.get("manual_price") is not None,
                })
            return {"portfolio": holdings, "risk_profile": user.get("risk_profile")}
        if name == "add_holding":
            user_manager.add_holding(
                chat_id, tool_input["symbol"], float(tool_input["quantity"]),
                float(tool_input["cost"]),
                manual_price=tool_input.get("manual_price"),
                note=tool_input.get("note"),
            )
            return {"ok": True, "mesaj": f"{tool_input['symbol']} eklendi."}
        if name == "remove_holding":
            ok = user_manager.remove_holding(chat_id, tool_input["symbol"])
            return {"ok": ok, "mesaj": "Çıkarıldı." if ok else "Portföyde bulunamadı."}
        if name == "set_alarm":
            sym = tool_input["symbol"]
            target = float(tool_input["target_price"])
            q = data_fetcher.get_price(sym)
            direction = "auto"
            if q:
                direction = "above" if target > q["price"] else "below"
            user_manager.add_alarm(chat_id, sym, target, direction)
            return {"ok": True, "mesaj": f"{sym} için {target} alarmı kuruldu ({direction})."}
        return {"error": f"Bilinmeyen araç: {name}"}
    except Exception as exc:
        logger.error("Tool çalıştırma hatası (%s): %s", name, exc)
        return {"error": str(exc)}


def chat_with_tools(question: str, user: Dict[str, Any], chat_id: int | str) -> str:
    """
    Doğal dil sorusunu TOOL USE ile yanıtlar. Claude gerektikçe canlı veri çeker
    veya portföyü değiştirir. Çok turlu (agentic) döngü ile çalışır.
    """
    try:
        client = _get_client()
    except Exception as exc:
        logger.error("Claude istemcisi kurulamadı: %s", exc)
        return "⚠️ Analiz servisi yapılandırılmamış (API anahtarı eksik)."

    portfolio_txt = _format_portfolio(user)
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": f"KULLANICI PORTFÖYÜ:\n{portfolio_txt}\n\nSORU:\n{question}"}
    ]
    final_text = ""

    for _ in range(MAX_TOOL_ITER):
        try:
            resp = client.messages.create(
                model=CHAT_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=[{"type": "text", "text": CHAT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=messages,
            )
        except APIError as exc:
            logger.error("Claude API hatası (sohbet): %s", exc)
            return "⚠️ Analiz şu anda yapılamıyor (API hatası). Biraz sonra dene."
        except Exception as exc:
            logger.error("Beklenmeyen sohbet hatası: %s", exc)
            return "⚠️ Beklenmeyen bir hata oluştu."

        _log_usage("sohbet", resp.usage)

        text_parts = [b.text for b in resp.content if b.type == "text"]
        if text_parts:
            final_text = "".join(text_parts).strip()

        if resp.stop_reason != "tool_use":
            return final_text or "…"

        # Araçları çalıştır ve sonuçları geri besle
        messages.append({"role": "assistant", "content": resp.content})
        tool_results: List[Dict[str, Any]] = []
        for block in resp.content:
            if block.type == "tool_use":
                logger.info("[tool] %s ← %s", block.name, block.input)
                result = _execute_tool(block.name, block.input, chat_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
        messages.append({"role": "user", "content": tool_results})

    return final_text or "⚠️ İşlem çok fazla adım gerektirdi, lütfen biraz sadeleştir."


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
    return _call_claude("\n".join(parts), label="sembol-analiz", model=CHAT_MODEL)


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
    raw = _call_claude(content, label="haber-puan", max_tokens=10, model=SCORING_MODEL)
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
