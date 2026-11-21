# Özellikler ve kullanım (Türkçe)

Hisse haberleri, analist not değişiklikleri, SEC dosyaları ve ABD makro gelişmelerini
takip eder; LLM ile analiz edip önemli olanları Telegram'dan bildirir; web arayüzünde gösterir.

- Arayüz: http://localhost:8500 (ya da `BORSA_RADAR_URL`)
- İsteğe bağlı Hermes skill'i: `~/.hermes/skills/finance/borsa-radar/SKILL.md` (Hermes kullanıyorsan)

## Bileşenler
| Modül | Görev |
|---|---|
| `collectors/news.py` | Finnhub, Yahoo RSS/API, Google News → filtre + dedupe |
| `collectors/ratings.py` | Analist notları (Yahoo + FMP), kural tabanlı önem |
| `collectors/sec.py` | 8-K, 10-Q/K, ihraçlar, 13D/G, Form 4 içeriden alım/satım |
| `collectors/macro.py` | Fed/CNBC/MarketWatch/BLS akışları, FRED takvimi, FOMC, bilanço tarihleri, açıklanan veriler |
| `analyzer.py` | Yeni olayları toplu LLM analizi (Hermes CLI → Codex, yedek OpenRouter) |
| `reactions.py` | Olay anı fiyatı ve +15dk / +1s / +1g değişim |
| `notifier.py` | Telegram (Hermes botu), eşikler, sessiz saatler, hız limiti |
| `digest.py` | Sabah/akşam özetleri, veri öncesi hatırlatmalar |
| `web/` | FastAPI + Jinja arayüz, lightweight-charts grafik |
| `prices.py` | Ortak günlük fiyat deposu (Yahoo, bölünme kayıtları); tüm motorlar buradan okur |
| `portfolio.py` | İşlem defteri: alt portföyler, ortalama maliyet, realize K/Z, kapanan turlar, zaman ağırlıklı getiri, SPX/NDX/IXIC kıyası |
| `importers/revolut.py` | Revolut hesap dökümü CSV içe aktarma (tekrar yüklemek güvenli) |
| `stockdata.py` | FMP + Yahoo ham veri, `api_cache` tablosunda alınma zamanıyla önbellek (hata olursa son iyi kopya, "bayat" işaretli) |
| `technicals.py` | Ortalama, RSI, ATR, oynaklık, S&P'ye göre güç, trend puanı (fiyat deposundan) |
| `options.py` | Yahoo opsiyon zincirinden yaklaşık harita: call/put duvarı, max pain, gamma yönü ve dönüş seviyesi, beklenen hareket, olağandışı akış |
| `hub.py` | Hisse merkezi `/h/SEMBOL`: Sayılar, Neden?, Analizler, Kazanç, Uzmanlar, Yöneticiler, Büyük para — her bölüm sonuç + kaynak + "doğrulanamadı" |
| `scorecard.py` | Karne `/h/SEMBOL/karne`: 6 boyut (temel 30, değerleme 20, trend 20, opsiyon 15, haber 10, anomali 5), adil değer (3 yöntem medyanı), sicil |
| `breadth.py` | S&P 500 genişliği (Wikipedia bileşen listesi, 7 gün önbellek): %50G/%200G üstü, yükselen−düşen, yeni zirve−dip, A/D çizgisi, McClellan |
| `regime.py` | Piyasa rejimi (kurallar v1): trend, VIX, genişlik, kredi, katılım, getiri eğrisi, dolar → 0-100 + etiket; İklim (0-10) / Hava (−8..+8); geriye dönük geçmiş ve sicil |
| `pulse.py` | Piyasa nabzı: endeks kartları (evre, RSI), makro göstergeler, HV/VIX yüzdelikleri, momentum faktör stresi, dağıtım günleri, takvim, opsiyon vadesi |
| `rotation.py` | Sektör/faktör rotasyonu: S&P'ye göre göreli güç, RRG tarzı kadran, haftalık kadran değişimi, 6 oranlı risk modu termometresi, senin hisselerinin sektörü |
| `market_report.py` | `/piyasa` sayfası için tek T tarihli rapor (5 dk önbellek) ve diğer sayfalardaki rejim rozeti |
| `risk.py` | Portföy riski `/portfolio/risk`: beta, oynaklık, VaR95, zirveden düşüş, korelasyon kümeleri, sektör yoğunluğu, faktör ve geçmiş kriz stres testleri, rejim tavanı, uyarılar |
| `thesis.py` | Tez defteri `/tez`: neden aldım + ölçülebilir çıkış kuralları (stop, zarar %, karne, gelir büyümesi, sulandırma, rejim, tarih) + gözden geçirme tarihi; kurallar her gece kontrol, tetikte Telegram |
| `alerts.py` | Fiyat alarmları `/alarmlar`: anlık seviye, kapanış seviyesi, günlük hareket %; Telegram |
| `context.py` | Tek bakışta durum (portföy, rejim, setup'lar, 24 saat gelişmeler, 72 saat takvim; `--symbol` ile tek hisse): `borsa-radar context --json`, `/api/context`, özetlerin girdisi |
| `karar.py` | Karar `/karar`: her pozisyon için kural tabanlı durum kartı (Sağlam / İzle / Tezi sorgula / Kural ihlali / Yetersiz veri) + portföy panosu; lehine/aleyhine sinyaller, ne değiştirir, sıradaki tarihler; model yalnızca doğrulanmış kısa yorum yazar; her gece 01:25, sicil ayrı |
| `setups.py` | Setup tarayıcı `/setups`: geri çekilme, kırılım, boğa bayrağı, aşırı satış sıçraması, bant, bilanço sonrası; giriş bandı/stop/2 hedef/R:R, kapanış bazlı takip, canlı + geriye dönük sicil |

## Site düzeni
Tek site, üst menü: **Bugün** `/` + Karar `/karar` (portföy, rejim, setup'lar, önemli gelişmeler, takvim, son özet) · **Portföy**
(Özet, İşlemler, Kapanan) · **Piyasa** (Rejim & Nabız `/piyasa`, Makro `/macro`, Takvim `/calendar`) · **Hisseler**
(Hisselerim `/tickers` → hisse merkezi `/h/SEMBOL` + karne, Fikir listesi `/fikirler`, Tarayıcı `/tarayici`) · **Setup** (Günün setup'ları, Strateji laboratuvarı `/setups/strateji`) · **Akış** (`/akis`, Günlük özetler) · **Ayarlar**.
Eski `/s/SEMBOL` adresleri `/h/SEMBOL#analizler`'e yönlenir. Portföye giren hisseler otomatik izlemeye alınır (`watch_holdings`).
Sabah/akşam özetleri portföy, rejim ve setup bilgisini de içerir (sabah özeti strateji laboratuvarının defterdeki yeni sinyallerini ve açılışta çıkacak kağıt işlemleri de yazar; ayrı bildirim yok); canlı setup tetiklenince / hedefe ya da stopa gelince Telegram notu gider.

## Risk, tez, alarm
```bash
borsa-radar risk [--json]
borsa-radar tez add CBRS "neden..." --rule "stop 95" --rule "zarar 30" --rule "gelir 40"
borsa-radar tez list · tez show CBRS · tez check
borsa-radar alarm add "NVDA kapanış 150 altı" · alarm list · alarm rm 3
```
Kaynak fikirler (yeniden yazıldı, kopyalanmadı): tradermonty/claude-trading-skills (MIT) tez yaşam döngüsü, devre kesici,
işlem öncesi kapı, pozisyon boyutu, işlem koçu; staskh/trading_skills (MIT) korelasyon/risk.

## Karar
Önce sonuç: Bugün sayfasının tepesinde portföy kararı, her hisse sayfasının başında kart (`/h/SEMBOL#karar`), ayrıntı `/karar`.
Etiketler durum tarifidir, al-sat emri değildir; durumu kurallar belirler, model yalnızca yorum yazar. Sabah özetine yalnızca durum sayıları ve değişenler girer.
Şirket ekseni karnedeki temel puandan gelir; karnede yoksa (ücretsiz FMP planı çoğu hissede veri vermiyor) radarın topladığı SEC çeyrek
verisinden hesaplanır ve kartta "temel (SEC)" diye yazar. Model yorumu ayrı rotadan gider (`karar_llm_provider` / `karar_llm_model`, varsayılan Opus).
Kota korumaları: sağlayıcının kota/limit diye reddettiği model çağrısı günlük sınırdan düşmez ve o rota 60 dk atlanır (`llm.py`);
FMP 429 dönünce 30 dk hiç çağrı yapılmaz (`stockdata.py`), yavaş değişen FMP verileri günlerce önbellekte kalır (`hub.py`).
```bash
borsa-radar karar [--json]            # pano: piyasa, durum sayıları, gündem, hisse başına bir satır
borsa-radar karar NVDA [--json]       # tek kart tüm ayrıntısıyla
borsa-radar karar run [--no-llm]      # yeniden hesapla (her gece 01:25'te otomatik)
borsa-radar karar sicil [--json]      # durumlara göre sonraki 20 işlem günü, S&P'ye göre fark
```

## Hermes
Hermes (Telegram) aynı veriyi CLI'dan okur; skill: `~/.hermes/skills/finance/borsa-radar/SKILL.md`.
```bash
borsa-radar context --json              # her şey tek JSON (yalnız kayıtlı veri, hızlı)
borsa-radar context --symbol NVDA --json
borsa-radar portfolio --json · karne NVDA --show --json · piyasa --json · setups --json
```

## Portföy
```bash
borsa-radar import revolut dokum.csv --sub P1 [--dry-run]   # Revolut → Yatırım → ··· → Belgeler → Hesap dökümü (tüm dönem)
borsa-radar trade add BUY AMZN 10 262.5 --sub P2 --note "bayrak kırılımı"
borsa-radar trade move NVDA P2          # bir sembolün tüm işlemlerini başka alt portföye taşı
borsa-radar cash deposit 5000 --sub P1
borsa-radar sub add P3 Opsiyon
borsa-radar portfolio                   # özet; arayüz: /portfolio, /portfolio/trades, /portfolio/closed
```

## Hisse merkezi ve karne
```bash
borsa-radar karne AMZN            # hesapla + kaydet (özet cümlesi modelden, sayı denetimli)
borsa-radar karne AMZN --show     # son karneyi göster
borsa-radar karne --all --no-llm  # portföy + izleme listesi (her gece 00:40'ta otomatik; sicili büyütür)
```
Arayüz: üst menüdeki "Hisse ara" kutusu veya portföydeki sembole tıklama → `/h/SEMBOL`. Tüm sayılar sunucuda hesaplanır;
model yalnızca karne özet cümlesini yazar ve girdide olmayan bir sayı kullanırsa metin reddedilir.

## Piyasa (rejim, nabız, rotasyon)
```bash
borsa-radar piyasa                 # rejim + rotasyon + genişlik özeti (T = son SPY kapanışı)
borsa-radar piyasa --refresh       # S&P 500 bileşen fiyatları → genişlik → rejim kaydı (~2 dk; her gece 00:25'te otomatik)
borsa-radar piyasa --json
```
Arayüz: `/piyasa` (Rejim · Nabız · Rotasyon · Genişlik · Sicil). Rejim geçmişi fiyat deposundan geriye dönük hesaplanır;
sicil, her etiketten sonraki 20 işlem gününün SPY getirisini bağımsız gözlem sayısıyla (n<20 uyarısı) gösterir.

## Setup tarayıcı
```bash
borsa-radar setups                # son taramayı göster
borsa-radar setups run [--no-llm] # tara (her gece 01:05'te otomatik; Telegram'a özet sırada bekler)
borsa-radar setups record         # sicil: canlı ve geriye dönük test ayrı
borsa-radar setups backfill       # aynı kuralları son ~500 güne uygula
```
Taranan evren iki katmanlı. **Kendi listen:** portföydeki hisseler + izleme listesi + Ayarlar'daki `setup_extra` havuzu.
**Keşif:** `setup_wide` ayarındaki endeks listelerinin (varsayılan `sp500 sp400`, istenirse `sp600`; boş = kapalı) kendi listende
olmayan üyeleri; liste Wikipedia'dan haftada bir, fiyatlar Yahoo'dan. Keşiften günde en fazla 5 kurulum gösterilir (kalite ≥ 70,
stop en az 1 ATR uzakta, sektör başına en fazla 2); kalanı sicil için kaydedilir ama sayfalarda, özetlerde ve Telegram durum
bildirimlerinde yer almaz. Keşif isimleri için FMP çağrısı yapılmaz; bilanço tarihi yalnızca seçilen 5 isim için Yahoo'dan bakılır.
`borsa-radar setups backfill --wide` geriye dönük testi keşif evreniyle birlikte yeniden kurar (~20 dk) ve keşif seçim kuralının
kendi sicilini (günün en yüksek puanlı 5'i nasıl sonuçlanırdı) kaydeder; bu sicil Keşif bölümünde gösterilir. İlk ölçüm (Kasım 2026,
914 şirket, ~11 bin işlem): beklenti −0,03R, kalite puanı sonuçla ters ilişkili; kurallar s1'in geniş evrende avantajı yok. Takip kapanış bazlıdır;
isabet = Hedef 1 / (Hedef 1 + stop), tetiklenmeyenler pay dışı.

## Strateji laboratuvarı
```bash
borsa-radar lab download   # 2005 → bugün düzeltilmiş günlük mumlar (S&P 500 + 400 + kendi liste + piyasa göstergeleri) → data/lab_history.npz (~5 dk)
borsa-radar lab members    # o tarihteki S&P 500 üyeleri (2006 →) + hâlâ işlem gören eski üyelerin mumları → data/sp500_members.json, data/lab_history_ex.npz
borsa-radar lab run        # seçili stratejilerin 20 yıllık sicilini hesapla ve kaydet (~1 dk, kota yok); --universe today = eski evren
borsa-radar lab stress     # dört stres testi + hakem (~5 dk); sonuç sayfada ve laboratuvar günlüğünde
borsa-radar lab audit      # aylık denetim: download + members + run + stress (her ayın ilk pazarı 07:00 ayrı süreçte otomatik)
borsa-radar lab signals    # canlı tarama + kağıt defter takibi (her gece setup taramasının ardından otomatik)
borsa-radar lab            # kağıt defter özeti
```
Sayfa: Setup → Strateji laboratuvarı (`/setups/strateji`). Kurallar mekaniktir; geriye dönük test, canlı tarama ve kağıt defter aynı
fonksiyonları kullanır (`lab.py`). Emir modeli: sinyal ABD kapanışında görülür, alış ertesi seansta (o seans geçerli limit emir), satış
çıkış kuralının tetiklendiği kapanıştan sonraki açılışta; her işleme gidiş-dönüş %0,10 maliyet (sayfada iki katıyla da gösterilir).

Seçim yöntemi (Kasım 2026): literatürdeki kısa vadeli ortalamaya dönüş aileleri (Connors RSI(2), ConnorsRSI, kümülatif RSI, IBS,
Double 7, art arda düşüş) × 7 çıkış kuralı × emir tipi, sonra piyasa filtresi / süre / limit derinliği / zarar kes. Kurallar yalnızca
2006–2016 verisiyle seçildi (başarı ≥ %80, ≥ 150 işlem, kâr faktörü ≥ 1,5), 2017 → bugün sınav dönemi olarak bir kez ölçüldü.
Sonuç: seçim döneminde %80–92 başarı veren kuralların sınavda başarısı %70–79'a düştü; piyasa filtresi (S&P 500 200 günlük ortalama
üstü, VIX < 30, HYG/LQD 50 günlük ortalama üstü) ve 30 seanslık süre ile seçilen **Derin düşüş alımı** (ConnorsRSI < 15, kapanışın
%6 altına limit, maliyet üstü ilk kapanışta çık) sınavda 600 işlemde %80,8 başarı, işlem başı +%2,2; %4 limitli **Geri çekilme
alımı** 1.810 işlemde %79,4, +%1,3. Kazançlar küçük, kayıplar seyrek ama büyük (en kötü işlem −%33/−%42); 10 pozisyonluk kağıt
portföy sınav döneminde yılda %7–10, en derin düşüş %14–19 (S&P 500: %14,6 ve %34). Zarar kes eklemek seçim döneminde getiriyi
düşürdüğü için yok. Canlı kağıt defter 17 Kasım 2026'da başladı. (Bu rakamlar eski evrenle; aşağıya bakın.)
Not: kredi filtresi temettü düzeltilmiş kapanışla hesaplanır (HYG'nin aylık temettüsü ham oranı her ay düşürür).

Hayatta kalma yanlılığı (Kasım 2026, ücretsiz veriyle): evren artık **o tarihteki S&P 500 üyeleri** — bir hisse yalnızca endekste olduğu
günlerde sinyal üretir; açık işlem endeksten çıksa da kuralına göre kapanır. Üyelik geçmişi github.com/fja05680/sp500 (MIT, ağustos
2026'ya kadar; sonrası bugünkü listeyle tamamlanır). Tarihsel ticker'lar Sharadar'ın ücretsiz `tickers` tablosuyla (fiyat tarihleri,
ilgili ticker'lar) şirkete eşlenir; bu sayede yeniden kullanılan ticker'lar (CEG, DOW, SNDK) ve zincirler (21st Century Fox → Fox Corp)
doğru şirkete gider. 2006'dan beri 963 tarihsel ticker: üye-günlerin %81,3'ünün fiyatı var (hâlâ işlem gören 79 eski üye Yahoo'dan
indirildi); **309 şirket borsadan tamamen çekildi** (satın alma ya da iflas) ve ücretsiz fiyatı yok — üye-günlerin %18,5'i. Tiingo'nun
ücretsiz planı bunların ~184'ünü taşıyor (ayda 500 sembol, hesap gerekir), bağlanmadı. Eski evren `--universe today` ile çalışır.
Sonuç (sınav 2017 →): Derin düşüş alımı 216 işlem, %77,3 başarı, +%1,97 (eski evren: 600, %80,8, +%2,17); seçim döneminde 84 işlem
%75,0 ve +%0,37 (eski: %82,2, +%1,21). Geri çekilme alımı 787 işlem, %78,1, +%1,16 (eski: 1.810, %79,4, +%1,28). Defter yıllık %2,8 ve
%5,5. Bugünkü S&P 500 ile (yalnız hayatta kalanlar) seçim dönemi %82,8 / +%1,45 idi: farkın büyük kısmı hayatta kalma yanlılığıydı.
Stres hakemi yeni evrende de ikisini reddediyor (seçim/sınav sırası ve rastgele giriş testi).

Stres testleri (`lab_stress.py`): bir strateji ancak dört testin dördünü birden geçerse sayılır, aksi halde reddedilir; eşikler
sonuçlara bakılmadan önce `RULES` içinde sabitlendi ve `tests/test_lab_stress.py` onları birebir kilitler.
1. **Maliyet taraması:** 2017 → kural %0–2 gidiş-dönüş maliyetle yeniden işlenir; başa baş maliyet raporlanır. Şart: gerçekçi maliyetin
   3 katında (%0,30) işlem başı ortalama ve 10 pozisyonluk defter hâlâ artıda.
2. **Her başlangıç günü:** defter 2006'dan beri her seansta başlatılıp 252 seans işletilir. Şart: 12 aylık medyan > 0, en kötü ≥ −%20.
3. **Seçim / sınav sırası:** aynı kural ailesinin 200 ayarı (eşik × limit × süre × piyasa filtresi) başarı oranına göre iki dönemde
   ayrı sıralanır. Şart: canlı ayar iki dönemde de ilk çeyrekte (en az 100 işlem).
4. **Rastgele girişe karşı:** kuralla aynı sayıda işlem yapan 2.000 rastgele strateji: (a) aynı emir ve çıkış, rastgele uygun gün
   (sinyalin katkısı), (b) aynı filtreli evrende rastgele giriş, gerçek işlemlerle aynı tutma süresi. Şart: işlem başı ortalamada
   ikisinin de ≥ %95'ini geçmek ve z ≥ √(2·ln N); N = laboratuvarda bugüne kadar denenen farklı ayar sayısı (`lab_attempts`).
Reddedilen stratejinin sinyalleri sayfada kayıt için izlenmeye devam eder ama sabah özetine girmez. Her çalıştırma `lab_journal`'a
kural ve kod özetiyle bir satır yazar; eşik ya da test kodu değişince sayfa bunu işaretler.

## Fikir listesi
```bash
borsa-radar fikir sec        # SEC XBRL frames: 2018 → bugün, ~5.000 şirketin çeyreklik gelir/kâr/bilanço kalemleri → data/frames.db (~7 dk ilk sefer)
borsa-radar fikir run        # dört kural + fiyat/likidite + kısa liste için Yahoo (analist, not değişikliği, içeriden alım, haber)
borsa-radar fikir backtest   # kuralları 2019'dan beri her çeyrekte yeniden kur, 3/6/12 aylık getiriyi ölç (~15 sn)
borsa-radar fikir            # son liste
```
Sayfa: Hisseler → Fikir listesi (`/fikirler`); her cumartesi 09:30 kendiliğinden yenilenir. Kurallar: **Kâra yaklaşan**
(son 4 çeyreğin ≥3'ü zarar, zarar iki çeyrektir küçülüyor ve bir yıl öncesinden az, gelir ≥ +%15, hisse sayısı ≤ +%15; Zacks),
**İlk kâr** (≥3 zarar çeyreğinden sonra ilk kâr, gelir ≥ +%10; Zacks "First Profit"), **Hızlanan büyüme** (net kâr ≥ +%25, gelir
≥ +%15 ve hızlanıyor; CANSLIM C/A), **Finansalı iyileşen** (Piotroski benzeri 7 test, nakit akışı hariç). Hepsinde son 12 ay geliri
≥ 50 M$; listede fiyat ≥ 3 $ ve günlük işlem ≥ 2 M$. Şirket başına tek gelir kalemi seçilir (kapsamı iyi olanların en büyüğü; bankada
toplam gelir), mali yılın 4. çeyreği yıllıktan türetilir. Geçmiş ölçümde bir çeyrek, 10-Q için çeyrek sonundan 50, türetilmiş 4. çeyrek
(10-K) için 95 gün sonra "açıklanmış" sayılır.

İlk ölçüm (Kasım 2026, 2019-05 → 2026-08, 30 tarih, bugünkü S&P 1500 üyeleriyle): 6 aylık getiri, aynı günkü tüm hisselerin
ortalamasına göre fark — İlk kâr +9,7 puan (263 seçim, S&P'yi yenen %57), Kâra yaklaşan +8,3 (156, %49; medyan +%6, sonuç birkaç büyük
kazanana dayanıyor), Hızlanan büyüme +1,7 (2.339), Finansalı iyileşen +0,1 (2.081). Zarar eden şirketlerde hayatta kalan yanlılığı daha
büyüktür (batan zarar ediciler testte yok). Google Trends kullanılmadı: araştırma bulguları karışık, maliyet sonrası kârlı değil, resmi API yok.

Borsadan çekilenler (Kasım 2026): fiyatı olmayan seçimler artık sessizce atlanmıyor. Sharadar'ın ücretsiz `tickers` tablosu (SEC CIK ile)
seçimin ufuk içinde borsadan çekilip çekilmediğini söyler; sonuç bir aralık olarak verilir — ölçülen · her biri −%30 (Shumway 1997)
· −%100. Çekilen seçimler, hayatta kalan seçimlerin fiyatı olan payıyla tartılır (fiyat evrenimiz yalnız endeks üyeleri). Hâlâ işlem
gören eski S&P 500 üyelerinin fiyatları da eklendi. Ölçüm 18 Kasım 2026, SEC boşlukları şirket bazında doldurulduktan sonra (rakamlar
SEC verisi yenilendikçe kayar). 6 ay, S&P 500'e göre fark: İlk kâr +9,4 → +8,4 → +6,9 puan (23 çekilen seçim); Kâra yaklaşan +10,3 →
+9,0 → +7,0 (25); Hızlanan büyüme +2,5 → +2,0 → +1,1; Finansalı iyileşen +1,3 → +0,6 → −0,7. En sert varsayımda (her çekilen seçim
tam ağırlıkla −%30) İlk kâr +5,7, Kâra yaklaşan +3,9; −%100 tam ağırlıkta sırasıyla +0,2 ve −5,5. Aynı SEC verisiyle iki etki ayrı:
eski üyelerin fiyatlarını eklemek tüm hisselere göre farkı İlk kâr'da 9,6 → 8,8, Kâra yaklaşan'da 10,2 → 9,7 puana indirdi; SEC
verisindeki boşlukların doldurulması (aynı fiyat evreninde) ilk ölçüme göre İlk kâr'ı 9,7 → 9,6, Kâra yaklaşan'ı 8,3 → 10,2 yaptı.

## Hisse tarayıcı
```bash
borsa-radar tara run            # gece verisini şimdi yenile (ilk sefer bütün hisselerin fiyatlarını indirir, uzun sürer)
borsa-radar tara momentum -n 30 # hazır bir taramayı terminalde listele (kara_yakin, ilk_kar, canslim, momentum, asiri_satis, …)
```
Sayfa: Hisseler → Tarayıcı (`/tarayici`). NYSE / Nasdaq / NYSE American'daki bütün adi hisseler (ADR dahil; SPAC kabukları, varantlar
ve günlük işlem hacmi 250 bin $ altı hariç). Kaynaklar: Sharadar'ın ücretsiz `tickers` tablosu (borsa, sektör, alt grup; haftada bir
`data/sharadar_tickers.csv.zip`), SEC XBRL frames (fikir listesiyle aynı çeyrek verisi), Yahoo günlük mumlar (~400 gün), Finnhub bilanço
takvimi (45 gün). SEC'in XBRL servisinde son dört çeyreği eksik olan şirketlerde (2026'da bazı elektrik şirketleri, bankalar,
gayrimenkul fonları, Booking, Costco'nun 12/16 haftalık çeyrekleri; SEC'in kendi şirket bazlı verisinde de yoklar) Yahoo'nun
çeyreklik gelir tablosu kullanılır (haftalık önbellek, satırda "finansal: Yahoo"; bilanço olmadığı için F-skoru ve kurallar boş).
SEC'te hisse sayısı olmayan şirketlerde (çok sınıflı hisseler: GOOGL, META, V, BRK-B …) piyasa değeri Yahoo'daki son çeyreğin
ortalama seyreltilmiş hisse sayısıyla hesaplanır (yaklaşık). ADR'ler kendi para birimiyle raporladığı için finansalsız kalır.
`--no-yahoo` bu yedeği kapatır. Gece işi salı–cumartesi 02:40'ta çalışır, sonuç `screener_runs` / `screener_rows` tablolarına bir JSON satırı olarak
yazılır (son 3 çalıştırma tutulur); sayfa filtreleri bellekte uygular. Alanlar: piyasa değeri, F/S, F/K, gelir ve kâr büyümesi,
marjlar, F-skoru, seyrelme, başa başa kalan çeyrek, 1–12 ay getiri, S&P 500'e göre güç, RSI, ortalamalara ve 52 hafta zirvesine
uzaklık, ATR, göreli hacim, bilanço tarihi, fikir listesi kuralları. Hazır taramalardan yalnız fikir listesi kuralları (✓) geçmişte
ölçüldü; diğerleri test edilmemiş araştırma filtreleridir. Taramalar adla kaydedilir, CSV olarak indirilir, satırdan "İzle" ile takibe alınır.

## Kullanım
```bash
borsa-radar ticker add AMD          # ekle
borsa-radar ticker remove EOSE      # çıkar (geçmiş kalır)
borsa-radar ticker list
borsa-radar events --symbol NVDA --days 7 --min-importance 3
borsa-radar calendar --days 7
borsa-radar status
borsa-radar test-telegram
```

## Gizli anahtarlar
`~/.config/borsa-radar/secrets.env` (600): FRED, FINNHUB, BLS, BEA, EIA, ALPHA_VANTAGE, FMP.
Telegram token/chat `~/.hermes/.env`'den okunur.

## Servis
```bash
sudo cp deploy/borsa-radar.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now borsa-radar
journalctl -u borsa-radar -f
```
Tailscale: `tailscale serve --bg --https=8500 http://127.0.0.1:8500`

## Geliştirme
```bash
uv sync && uv run pytest -q
```
