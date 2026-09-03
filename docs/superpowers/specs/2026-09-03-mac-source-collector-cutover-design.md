# Mac Kaynak Arşivleyiciye Geçiş Tasarımı

## Amaç

ATEZ rapor skillinin kaynak taleplerini Mac'te çalışan Drive watcher'a göndermesi, Resmî Gazete Tebliğlerinin doğrulanmış biçimde yeni Drive arşivine yazılması ve rapor üretiminin bu arşivden devam etmesi amaçlanır. Eski Windows/GitHub Issue hattı, Mac hattı gerçek uçtan uca kabul testini geçtikten sonra devreden çıkarılır.

## Tek aktif veri akışı

1. Kullanıcı belirli bir tarih için ATEZ raporu ister.
2. Skill yeni Drive kökündeki `<tarih>/sources/rg-*/_READY.json` kaydını arar.
3. Geçerli kaynak seti yoksa skill, kökteki `requests/` klasörüne `SOURCE_REQUEST__<tarih>__<istek-kimliği>.json` dosyası yükler.
4. Mac watcher talebi atomik olarak `PROCESSING_` adına geçirerek sahiplenir.
5. Arşivleyici yalnız `resmigazete.gov.tr` ve `www.resmigazete.gov.tr` kaynaklarından fihrist, Tebliğ HTML/PDF dosyaları ve bağlı ekleri indirir.
6. Dosyalar Drive'a yüklenir, boyut ve SHA-256 değerleri geri okunarak doğrulanır.
7. Tüm dosyalar doğrulandıktan sonra en son `_READY.json` yazılır ve talep `DONE_` olarak işaretlenir.
8. Skill manifestteki bütün Tebliğleri sınıflandırır; HTML'i ham baytlardan ayrıştırır, PDF/görseller için metin çıkarma ve gerektiğinde OCR uygular, ardından analiz ve raporu üretir.

GitHub Action yalnız zamanlanmış ve elle başlatılan Mac arşivleme işleri için kalır. Sohbetten gelen taleplerde GitHub Issue oluşturulmaz. Böylece watcher ile Action aynı sohbet talebi için yarışmaz.

## Talep sözleşmesi

Talep dosyası UTF-8 JSON'dur:

```json
{
  "schema_version": 1,
  "request_id": "benzersiz-kimlik",
  "report_date": "YYYY-MM-DD",
  "requested_at": "RFC3339 UTC",
  "requested_by": "atez-mevzuar-rapor-alcn"
}
```

Watcher yalnız bu şemayı ve tam tarih biçimini kabul eder. Dosya adından veya serbest metinden tarih tahmini yapmaz. Aynı tarih için geçerli bir `_READY.json` varsa yeniden indirme yapmadan talebi `DONE_` olarak tamamlar. Aynı tarih işleniyorsa ikinci talep bekler veya mevcut işlemin sonucunu kullanır.

Başarısız talep `FAILED_` olarak işaretlenir; aynı JSON dosyasına `status`, `completed_at` ve `error` alanları yazılır. Başarılı talep de `DONE_` adına geçirilirken aynı dosyaya `status`, `completed_at`, `rg_number` ve READY dosyasının Drive kimliği eklenir. Geçici ağ hataları sınırlı geri deneme uygular; kalıcı hata sonsuz döngü oluşturmaz.

## Kaynak arşivi sözleşmesi

Yeni Drive kökü `ATEZ-Gemini-Mevzuat-Radari` ve kök klasör kimliği `1xrSozns-2sMBJRUuY3JvglVdSnKX1hDc` olur. Tarih yapısı şöyledir:

```text
YYYY-MM-DD/
  sources/
    rg-<sayı>/
      index.html
      source-manifest.json
      doc-01/
        source.html|source.pdf
        manifest.json
        <ekler>
      _READY.json
  analyses/
  reports/
  deliveries/
```

`source-manifest.json` mevcut Gemini `documents` dizisini korur. Skill `documents[*].main_document` ve `documents[*].attachments` alanlarını kaynak kabul eder. Her dosya kaydı özgün URL, son URL, HTTP durumu, içerik türü, boyut, SHA-256, rol ve arşiv içi göreli yolu içerir.

`_READY.json`, yalnız toplam sayıyı değil doğrulanan her dosyanın göreli yolu, Drive dosya kimliği, boyutu ve SHA-256 değerini içerir. Skill klasör listesinden dosyayı bulur, ham içeriği indirir ve manifest değerleriyle yeniden doğrular. `_READY.json` en son yazıldığı için yarım arşiv hiçbir zaman hazır sayılmaz.

## İçerik okuma

- Drive bağlantısındaki `content` alanının boş olması hata değildir; skill ham `file_uri` veya sınırlı uyumluluk yanıtındaki base64 baytlarını kullanır.
- HTML dosyaları karakter kodlaması belirlenerek DOM ve tablolarıyla birlikte ayrıştırılır.
- PDF önce yerleşik metin katmanından okunur; metin yoksa veya yetersizse sayfalar OCR'dan geçirilir.
- GIF/JPG/PNG biçimindeki metinli ekler OCR'dan geçirilir.
- Bir dosya gerçekten çözümlenemiyorsa yalnız ilgili belge `SOURCE_EXTRACTION_BLOCKED` olur; doğrulanmış kaynak setinin tamamı otomatik olarak geçersiz sayılmaz.

## Ağ ve güvenlik

- Yalnız HTTPS ve iki resmî alan adı kabul edilir; yönlendirmelerin her adımı aynı izin listesinde kalmalıdır.
- Genel amaçlı sertifika doğrulamasını kapatma kaldırılır. Resmî sitenin tarayıcıda gerekli sertifika uyumluluğu Playwright bağlamıyla sınırlandırılır; Drive ve diğer bağlantılarda TLS doğrulaması açık kalır.
- İndirilen hata sayfası, giriş sayfası veya boş yanıt; HTTP 200 olsa bile belge kabul edilmez.
- Boyut, içerik türü ve temel dosya imzası denetlenir.

## Test ve geçiş

1. Birim testleri talep doğrulama, atomik sahiplenme, tekrar çalıştırmama, manifest üretimi, yönlendirme/alan adı sınırı ve READY sırasını kapsar.
2. Yerel loopback entegrasyon testi HTML, PDF ve ek dosya indirmeyi doğrular.
3. Gerçek kabul testi 2026-08-14 için Drive talebi oluşturarak Mac watcher'ın arşivi üretmesini ve skill'in arşivden en az bir belgeyi okuyabilmesini doğrular.
4. Kabul testi geçince eski `atez-mevzuat-radari-fetcher` workflow'u GitHub'da devre dışı bırakılır. Skill'deki eski Drive kökü ve GitHub Issue yolu kaldırılır.
5. Windows runner hizmeti ofis bilgisayarında durdurulup kaydı kaldırılır. Mac hattı hazır olmadan bu adım uygulanmaz.

## Başarı ölçütleri

- Sohbetten verilen tek rapor isteği, ek GitHub Issue işlemi olmadan Mac watcher'a ulaşır.
- Aynı tarih için en fazla bir etkin arşivleme çalışır.
- `_READY.json` yalnız tüm arşiv dosyaları Drive'da doğrulandıktan sonra oluşur.
- Skill HTML, PDF ve görsel ekleri okuyarak rapor üretir.
- Eski Windows workflow'u artık iş kabul etmez.
