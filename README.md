# 📈 Yatırım Araştırma Asistanı (Telegram + Claude)

Telegram üzerinden çalışan, **çok kullanıcılı** kişisel yatırım araştırma asistanı.
Her kullanıcının kendi portföyü, risk profili ve bildirim tercihleri vardır.
Piyasa verisi, haber taraması ve yapay zeka destekli analiz (Claude Haiku 4.5) sunar.

> ⚠️ **Yasal uyarı:** Bu araç yatırım tavsiyesi vermez. Yalnızca araştırma ve
> bilgilendirme amaçlıdır. Asistan asla kesin "al/sat" demez.

---

## ✨ Özellikler

- 🤖 **Doğal dil sohbeti** — portföyün bağlamında Claude'a soru sor
- 📊 **Portföy takibi** — anlık fiyat, kâr/zarar, toplam değer
- 📰 **Akıllı haber taraması** — haberleri Claude 1-10 puanlar, eşiği geçen size bildirilir
- ☀️ **Otomatik brifingler** — sabah brifingi, akşam özeti, haftalık rapor
- 🚨 **Fiyat alarmları** — hedef seviyeye ulaşınca bildirim
- 🌍 **Çoklu kaynak** — BIST, ABD hisseleri, döviz, altın (yfinance) + kripto (CoinGecko)

---

## 🧱 Mimari

```
ai-investment-research-assistant/
├── main.py                  # Giriş noktası (bot + scheduler, graceful shutdown)
├── config/
│   ├── settings.yaml        # Genel ayarlar (sıklık, eşik, modeller)
│   └── users/               # Her kullanıcı için ayrı JSON
├── bot/
│   ├── telegram_handler.py  # Komutlar + doğal dil yönlendirme
│   └── formats.py           # Mesaj/bildirim şablonları
├── core/
│   ├── data_fetcher.py      # yfinance + CoinGecko + RSS
│   ├── analyzer.py          # Claude API (prompt caching, token logu)
│   ├── news_scanner.py      # Periyodik haber tarama + puanlama
│   ├── scheduler.py         # APScheduler job'ları
│   └── user_manager.py      # Kullanıcı CRUD + portföy/alarm
└── utils/
    ├── config.py            # Ayar/ortam yükleyici
    └── logger.py            # Dosya + konsol loglama
```

---

## ⚙️ Kurulum

### 1. Gereksinimler
- Python 3.11+
- Telegram bot token ([@BotFather](https://t.me/BotFather)'dan)
- Anthropic API anahtarı ([console.anthropic.com](https://console.anthropic.com))

### 2. Yerel kurulum
```bash
cd ai-investment-research-assistant
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env dosyasını aç, TELEGRAM_BOT_TOKEN ve ANTHROPIC_API_KEY değerlerini gir
```

### 3. Chat ID'ni öğren
Botuna Telegram'dan `/basla` yaz — bot seni otomatik kaydeder.
(Hazır örnek kullanıcı için `config/users/user_template.json` içindeki
`chat_id` alanını kendi chat ID'nle değiştirip dosyayı `<chat_id>.json`
olarak kaydedebilirsin.)

### 4. Çalıştır
```bash
python main.py
```

---

## 🐳 Docker ile çalıştırma (VPS deploy)

```bash
cp .env.example .env   # değerleri doldur
docker compose up -d --build
docker compose logs -f # logları izle
```

Veriler `config/users/`, `logs/`, `data/` dizinlerinde kalıcıdır.

---

## 💬 Komutlar

| Komut | Açıklama |
|-------|----------|
| `/basla` | Kayıt + interaktif portföy kurulumu |
| `/portfoy` | Portföy durumu (fiyat, K/Z, toplam değer) |
| `/brifing` | Anlık piyasa brifingi |
| `/analiz THYAO` | Teknik + temel analiz özeti |
| `/haber NVDA` | Sembol haberleri |
| `/alarm BTC 70000` | Fiyat alarmı kur |
| `/ekle THYAO 100 250` | Portföye varlık ekle (sembol adet maliyet) |
| `/cikar THYAO` | Portföyden varlık çıkar |
| `/ayar` | Bildirim tercihleri (inline butonlar) |
| _(serbest mesaj)_ | Portföyün bağlamında Claude'a soru |

### Sembol formatları
| Tür | Örnek |
|-----|-------|
| BIST hisse | `THYAO`, `ASELS` (otomatik `.IS` eklenir) |
| ABD hisse | `NVDA`, `AMD` |
| Kripto | `BTC`, `ETH`, `SOL` |
| Döviz | `DOLAR`, `EURO` |
| Emtia | `ALTIN`, `GUMUS`, `PETROL` |

---

## 🔧 Ayarlar (`config/settings.yaml`)

- `news.scan_interval_minutes` — haber tarama sıklığı (varsayılan 120)
- `news.importance_threshold` — bildirim eşiği 1-10 (varsayılan 7)
- `schedule.morning_briefing` / `evening_summary` — brifing saatleri
- `alarms.check_interval_minutes` — alarm kontrol sıklığı
- `claude.model` — kullanılan model (varsayılan `claude-haiku-4-5-20251001`)

---

## ⏰ Zamanlanmış görevler

| Görev | Zaman (TR) |
|-------|-----------|
| Sabah brifing | Her gün 08:00 |
| Haber taraması | Her 2 saatte (09:00–23:00) |
| Akşam özeti | Her gün 18:00 |
| Fiyat alarmı kontrolü | Her 15 dakika |
| Haftalık rapor | Pazar 20:00 |

---

## 📝 Notlar

- **Maliyet:** Haber puanlama her haber için 1 Claude çağrısı yapar.
  Maliyeti kontrol etmek için `importance_threshold` ve `scan_interval_minutes`
  ayarlarını yükseltebilirsiniz. Token kullanımı loglanır.
- **Prompt caching:** Sabit system prompt önbelleğe alınır → tekrar eden
  çağrılarda giriş token maliyeti düşer.
- **Rate limit:** yfinance/CoinGecko çağrıları arasında küçük bir bekleme vardır.
- KAP/Reuters RSS uç noktaları zamanla değişebilir; `news_sources` listesini
  güncel tutun.
