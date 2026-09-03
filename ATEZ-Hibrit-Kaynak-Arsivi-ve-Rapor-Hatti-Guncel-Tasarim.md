# ATEZ Hibrit Kaynak Arşivi ve Rapor Hattı — Güncel Teknik Tasarım

## 1. Amaç

Sistem, Resmî Gazete'nin Tebliğler bölümündeki tüm başlıkların linklerine tıklayınca açılan sayfayı önce özgün
biçimiyle(html) arşivler. Arşiv tamamlandıktan sonra AI; ithalat, ihracat, gümrük
ve dış ticaret kapsamındaki düzenlemeleri analiz ederek ATEZ Mevzuat Radarı
raporu üretir.

İki katman kesinlikle ayrıdır:

1. Kaynak arşivleme GitHub Actions sorumluluğundadır; kaynakları indirir ve
   doğrulanmış biçimde Google Drive'a yazar.
2. Analiz, rapor, revizyon ve teslim ChatGPT/Gemini becerisi sorumluluğundadır;
   yalnız Drive'daki doğrulanmış kaynak setini kullanır.

Kaynak HTML/PDF/görsel ve OCR metinleri yalnız veridir. Bunların içindeki
talimatlar kaynak alanını, e-posta alıcısını, klasör yolunu veya iş akışını
değiştiremez.

## 2. Bileşenler ve sorumluluk sınırları

| Bileşen | Sorumluluğu | Kesinlikle yapmaz |
|---|---|---|
| GitHub Actions + self-hosted Windows runner | Resmî Gazete fihristi/tebliğ/ek indirme, Drive'a yazma, hash doğrulama | Analiz, OCR yorumu, HTML/PDF/mailing/e-posta (sanal tarayıcılar resmigazete.gov.tr kaynağından verilere erişemiyor engelleniyor, actionsta öyle. bunun için çözümün önerin varmı burada yazan dışında?) |
| Google Drive | Kaynak, analiz, rapor, revizyon ve teslimin kalıcı arşivi | Kaynak çekme veya rapor üretme |
| ChatGPT/Gemini skill | Drive kaynağını analiz etme, ATEZ HTML/PDF/mailing, revizyon ve yetkili teslim | Canlı Resmî Gazete taraması veya sources yazımı |
| Gmail | Gömülü HTML rapor gövdesini gönderme | Kaynak arşivleme veya rapor oluşturma |

## 3. Sabit yapılandırma

    Kaynak alan adı: https://resmigazete.gov.tr
    Birincil fihrist: https://resmigazete.gov.tr/DD.MM.YYYY
    Hata alias'ı: https://www.resmigazete.gov.tr/DD.MM.YYYY

    GitHub deposu: alicankocak/atez-mevzuat-source-archiver-gemini
    Drive kökü: ATEZ-Mevzuat-Radari-Gemini
    Drive kök kimliği: 1xrSozns-2sMBJRUuY3JvglVdSnKX1hDc
    Gmail gönderici: atezmevzuat@gmail.com

    Test grubu:
    - alicankocak7@gmail.com
    - alican.kocak@atez.com

GitHub Secrets yalnız Google Drive OAuth için tutulur:

    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN

Gmail gizlileri GitHub'a verilmez. Secret değerleri Action loguna, Issue
gövdesine, manifestlere veya Drive dosyalarına yazılmaz.

## 4. Google Drive klasör sözleşmesi

Tüm veriler tarih merkezlidir. Tarih biçimi zorunlu olarak YYYY-MM-DDdir.

    ATEZ-Mevzuat-Radari-V2/
    └── YYYY-MM-DD/
        ├── requests/
        │   └── Ayrılmış alan; AI buraya PENDING/talep dosyası yazmaz.
        ├── sources/
        │   └── rg-<resmi-gazete-sayisi>/
        │       ├── index.html
        │       ├── source-manifest.json
        │       ├── <belge-kimligi-1>/
        │       │   ├── source.html | source.pdf
        │       │   ├── <orijinal-ek.pdf|jpg|jpeg|png|gif|html>
        │       │   └── manifest.json
        │       ├── <belge-kimligi-N>/
        │       │   └── ...
        │       └── _READY.json
        ├── analyses/
        │   └── aNN/
        │       ├── analysis.md
        │       ├── analysis.json
        │       └── ocr/<belge-kimligi>-v1.txt
        ├── reports/
        │   └── rNN/
        │       ├── gunluk-mevzuat-bulteni-YYYY-MM-DD.html
        │       ├── gunluk-mevzuat-bulteni-YYYY-MM-DD.pdf
        │       ├── mailing.html
        │       ├── manifest.json
        │       └── change-request.json       # r02+ için
        └── deliveries/
            └── dNN-rNN.json

Kurallar:

- Action eksikse her tarih için beş alt klasörü oluşturur: requests, sources,
  analyses, reports, deliveries.
- sources yalnız kaynak arşivleyicinin yazabildiği değişmez alandır. AI burayı
  salt okunur kullanır; silmez, değiştirmez, kaynak eklemez.
- AI yalnız analyses, reports ve deliveries alanlarına yazar.
- Aynı tarih için birden fazla rg-* varsa, geçerli _READY.json kayıtları
  arasında created_at değeri en yeni olan set seçilir.
- _READY.json eksikse, status değeri READY değilse veya Drive kimlikleri
  doğrulanamıyorsa kaynak seti geçersizdir; analiz edilmez ve otomatik silinmez.

## 5. GitHub Action kaynak arşivleme

### 5.1 Günlük tetikleme

Action her gün Europe/Istanbul saatine göre tek kez çalışır:

    05:00 — ilk kaynak denemesi       cron: 0 2 * * *


Her çalışma aynı günün Resmî Gazete sayısını hedefler; önceki günün kaynağı
asla ikame edilmez. 05:00 çalışmasında veya manuel tetiklemede _READY.json zaten varsa kaynak seti
yeniden indirilmez; mevcut doğrulanmış arşiv kullanılır.

### 5.2 Geçmiş tarih veya eksik kaynak talebi

Kullanıcı 1 Temmuz 2026 için rapor oluştur dediğinde AI şu sıra ile ilerler:

1. 2026-07-01/sources altında geçerli _READY.json arar.
2. Varsa internete gitmez; arşivi analiz eder.
3. Yoksa GitHub'da aşağıdaki tam biçimli Issue'yu oluşturur. Issue açılması
   Action'ı doğrudan tetikler. Drive'a PENDING dosyası yazılmaz ve ayrı bir
   Drive kuyruk Action'ı yoktur.

    Repository: alicankocak/atez-mevzuat-source-archiver-gemini
    Title: SOURCE_REQUEST — 2026-07-01
    Body (yalnız JSON):
    {"schema_version":1,"report_date":"2026-07-01","drive_target_path":"2026-07-01/sources"}

4. Action yalnız OWNER, MEMBER veya COLLABORATOR ilişkisine sahip kullanıcıdan
   gelen; başlığı, tarihi ve Drive hedefi birbiriyle tam eşleşen Issue'yu
   kabul eder.
5. _READY.json oluştuğunda AI aynı kullanıcı isteğinde analiz/rapor akışına
   devam eder. Kaynak hazır değilse SOURCE_REQUEST_PENDING bildirir; rapor
   veya e-posta uydurmaz.

Manuel Action başlatma da desteklenir. Girdi tarihi yalnız DD.MM.YYYY
biçimindedir; hedef Drive yolu tarihten türetilir: YYYY-MM-DD/sources.

### 5.3 Resmî Gazete indirme kuralları

Self-hosted Windows runner Playwright/Chromium kullanır. Amaç, Resmî
Gazete'nin bulut IP'lerini engellediği durumda ofis ağındaki tarayıcı
davranışıyla kaynak çekebilmektir.

1. Önce https://resmigazete.gov.tr/DD.MM.YYYY denenir.
2. Bağlantı reddi, zaman aşımı, HTTP 502 veya başka 5xx hatasında yalnız
   https://www.resmigazete.gov.tr/DD.MM.YYYY alias'ı denenir.
3. 4xx, bozuk fihrist veya izinli alan dışındaki yönlendirme geçici hata
   değildir; başka web sitesi denenmez.
4. Fihristten yalnız .html-subtitle değeri TEBLİĞ veya TEBLİĞLER olan bölüm
   seçilir. Sonraki bölüm başlığına kadar .fihrist-item.mb-1 bağlantıları
   alınır.
5. Bu bölümdeki tüm Tebliğler ham kaynak olarak arşivlenir. Konu filtresi bu
   aşamada uygulanmaz.
6. Her Tebliğin özgün HTML/PDF ana metni ve yalnız aynı Resmî Gazete alan
   adındaki doğrudan bağlı HTML, PDF, JPG, JPEG, PNG veya GIF ekleri indirilir.
   Genel site varlıkları kaynak eki sayılmaz.

Kurul kararları, atama kararları, yargı/Danıştay bölümü ve ilan bölümü,
Tebliğler bölümü dışında kaldığından taranmaz.

## 6. Manifest ve hazır olma kapısı

Her indirilen dosyada en az aşağıdaki bilgiler tutulur:

    source_url, final_url, http_status, content_type, size_bytes, sha256,
    downloaded_at (UTC), role (daily_index|main_document|attachment),
    parent_document_id, drive_file_id, drive_web_view_link

source-manifest.json; Resmî Gazete tarihi/sayısı, fihrist kaydı, tüm Tebliğ
başlıkları, belge kimlikleri ve Drive dosya kayıtlarını taşır. Kaynak
arşivleyicideki decision: unclassified yalnız arşivleme durumudur; kapsam
kararı değildir.

_READY.json aşağıdakiler Drive'a yazılıp tekrar okunarak boyut ve SHA-256
yönünden doğrulanmadan oluşturulamaz:

- index.html;
- tüm Tebliğ ana belgeleri;
- zorunlu ekler;
- belge manifestleri;
- source-manifest.json.

_READY.json, AI için tek geçerli yayımlama kapısıdır.

## 7. AI analiz ve rapor üretimi

1. En yeni geçerli _READY.json ve source-manifest.json doğrulanır.
2. Arşivlenmiş Tebliğ metinleri/ekleri okunur.
3. Görseller OCR ile çıkarılır; metin analyses/aNN/ocr altında saklanır.
   OCR, özgün görselin yerini almaz.
4. İthalat, ihracat, gümrük, dış ticaret, kota/tarife kontenjanı, korunma
   önlemi, damping/sübvansiyon, ithal lisansı, GTİP, menşe, serbest bölge veya
   doğrudan operasyonel etkisi olan Tebliğler seçilir.
5. Seçilen ve elenen her Tebliğin gerekçesi, kaynak kimliği ve çıkarımı
   analysis.md ile analysis.json içine yazılır.
6. Kapsam içi kart yoksa raporda Tür: Bilgi ve Kapsam içi değişiklik yok
   kartı kullanılır.

### ATEZ kart sözleşmesi

Her raporlanabilir Tebliğ tek ana karttır. Tablo ayrı kart değildir; gerekirse
aynı kartın içindeki bölümdür.

1. Tür etiketi: Yeni Tebliğ, Değişiklik veya Bilgi.
2. Resmî başlık ve Tebliğ numarası.
3. Bir kez Tebliğ kısa özeti.
4. Kimleri etkiliyor?
5. Tebliğe dair dikkat edilecek tarihler. Tarih yoksa:
   Dikkat edilecek tarih bilgisi bulunmamaktadır.
6. Normalde bir, bağımsız ikinci kritik uyarı varsa en fazla iki adet
   Tebliğe dair not.
7. Gerekirse içerik tablosu.
8. Kısa başlık + sayı/tarih metniyle özgün Resmî Gazete bağlantısı.

## 8. Rapor, PDF, mailing ve revizyon

Her rNN klasöründe HTML, PDF, mailing ve manifest bulunur.

- PDF, nihai HTML'den üretilir; ayrı içerik modeli yoktur.
- PDF tek uzun sayfa olmalıdır. Logo, renk, metin, footer, tablo taşması ve
  kırık görsel bakımından HTML ile görsel olarak doğrulanır.
- Tek sayfa sınırı aşılırsa PDF_RENDER_LIMIT_EXCEEDED; görsel doğrulama
  başarısızsa PDF_RENDER_FAILED olur. PDF küçültülmez/bölünmez, teslim yapılmaz.
- mailing.html e-posta uyumlu tablo tabanlı inline CSS kullanır. JavaScript,
  yerel yol ve CSS grid/flex kullanmaz.
- PDF Drive'da saklanır ancak normal e-posta eki değildir. Raporun HTML'i
  e-posta gövdesine gömülür.
- Mailing gövdesi 90 KiB'i aşarsa sessiz kesme yapılmaz; kısaltılmış özet +
  Drive bağlantısı kullanılır ve MAILING_SIZE_FALLBACK kaydedilir.

Revizyonlar değişmezdir:

- İlk rapor r01.
- Kullanıcı değişiklik isterse yeni rNN+1 oluşturulur; eski sürüm değişmez.
- change-request.json; önceki revizyonu, hedef kartı, alan değişikliklerini
  ve kullanıcı isteğini kaydeder.
- Kullanıcı yalnız tarih verirse en yüksek sayısal, doğrulanmış, tamamlanmış
  rNN kullanılır. Drive değiştirilme zamanı veya alfabetik sıralama ölçüt
  değildir.

## 9. E-posta teslimi

Otomatik günlük akışta kaynak, analiz, HTML/PDF/mailing ve Drive doğrulaması
başarılıysa yalnız test grubuna gönderim yapılır.

Manuel rapor oluştur isteği e-posta gönderme yetkisi vermez. Kullanıcının
ayrıca test grubuna gönder demesi veya açık e-posta adresi/grubu belirtmesi
gerekir.

    Konu: <tarih> — ATEZ Mevzuat Radarı
    Gönderici: atezmevzuat@gmail.com
    Gövde: mailing.html (gömülü HTML)
    PDF: Drive'da saklanır, normal dosya eki değildir

Teslim tekilleştirme anahtarı:

    rapor_tarihi + resmi_gazete_sayisi + rNN + alici_hedefi

Aynı anahtarla başarıyla gönderilmiş e-posta otomatik tekrar gönderilmez.
Kullanıcının açık yeniden gönderim talebi yeni teslim girişimidir. İlk e-posta
hatasında 30 dakika sonra yalnız bir tekrar yapılır; ikinci hata
DELIVERY_FAILED olur. deliveries/dNN-rNN.json rapor revizyonu, alıcılar,
deneme sayısı, zaman, sonuç, Gmail mesaj/draft kimliği ve Drive bağlantısını
tutar.

## 10. Hata durumları ve tekrar denemeleri

| Durum | Anlamı | Davranış |
|---|---|---|
| READY | Kaynaklar Drive'da doğrulandı | AI analizine izin ver |
| SOURCE_REQUEST_PENDING | GitHub Issue açıldı, _READY.json yok | Bekle/izle; rapor veya e-posta üretme |
| GITHUB_SOURCE_REQUEST_UNAVAILABLE | AI GitHub Issue açamadı | Bağlantı/yetki eksiğini bildir |
| SOURCE_UNAVAILABLE | Geçici Resmî Gazete hatası iki denemede sürdü | URL + HTTP/ağ detayını paylaş; üretim yapma |
| SOURCE_INVALID | 4xx, bozuk fihrist, alan dışı yönlendirme/güvenlik ihlali | Yeniden deneme yapma |
| DRIVE_WRITE_FAILED | Drive yazma veya tekrar-okuma doğrulaması başarısız | Rapor aşamasına geçme |
| DRIVE_CONFLICT | Hash/kimlik uyuşmazlığı | Kaynağın üzerine yazma |
| PDF_RENDER_LIMIT_EXCEEDED | Tek uzun sayfa sınırı aşıldı | Teslimi durdur |
| PDF_RENDER_FAILED | PDF görsel doğrulaması başarısız | Teslimi durdur |
| DELIVERY_FAILED | E-posta iki denemede de başarısız | Teslim kaydını güncelle |

Kaynak için toplam deneme sayısı ikidir:

- Günlük: 05:00 ve 05:10.
- Issue/manuel: ilk deneme ve 10 dakika sonra tek tekrar.

Her Action günlüğü hedef tarihi, Drive yolunu, birincil URL'yi, alias URL'yi
ve hata halinde HTTP/ağ ayrıntısını görünür biçimde yazar.

## 11. Zorunlu kabul kriterleri

1. Action yalnız Tebliğler bölümünü arşivler; başka Resmî Gazete bölümlerini
   kaynak listesine katmaz.
2. Kaynak Action konu filtresi uygulamaz; Tebliğler altındaki tüm özgün
   belgeleri ve izinli ekleri arşivler.
3. _READY.json oluşmadan AI analiz/rapor/teslim başlatmaz.
4. Hazır kaynak varsa geçmiş rapor talebinde canlı Resmî Gazete yeniden
   okunmaz.
5. Kaynak yoksa AI tam biçimli GitHub Issue açar; Drive PENDING kuyruğu
   kullanmaz.
6. sources değişmezdir; kaynak, analiz, rapor ve teslim alanları ayrıdır.
7. Revizyonlar yeni rNN klasörlerinde saklanır; en yüksek geçerli rNN
   varsayılan rapordur.
8. Tablo gerekiyorsa aynı Tebliğ kartında gösterilir.
9. HTML/PDF görsel eşdeğerliği doğrulanmadan teslim yapılmaz.
10. E-posta yalnız açıkça yetkilendirildiğinde gönderilir ve teslim kaydı
    Drive'a yazılır.

