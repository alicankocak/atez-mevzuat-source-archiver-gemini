import sys
from pathlib import Path
from src.reporter import ReportGenerator
from src.mailer import ReportMailer

def main():
    data = {
        "report_id": "240515-01",
        "resmi_gazete_sayisi": "32547",
        "formatted_date": "15 Mayıs 2024, Çarşamba",
        "overview": "15 Mayıs 2024 tarihli Resmî Gazete kapsamında; tarım ürünlerinin ticari kalite denetim standartları revize edilmiş, ithalatta haksız rekabetin önlenmesine ilişkin tebliğ ile damping önlemi getirilmiş ve tıbbi tanı kitleri ithal tebliği yürürlükten kaldırılmıştır.",
        "stat_new": 1,
        "stat_changed": 1,
        "stat_removed": 1,
        "cards": [
            {
                "type": "Değişiklik",
                "title": "Bazı Tarım Ürünlerinin İhracatında ve İthalatında Ticari Kalite Denetimi Tebliği",
                "teblig_no": "Ürün Güvenliği ve Denetimi: 2024/36",
                "summary": "Tarım ürünleri ticari kalite denetim listelerinde teknik düzenlemeler yapılmıştır.",
                "who_affected": [
                    "Tarım ve gıda ürünleri ithalatçı ve ihracatçıları",
                    "Gümrük Müşavirleri ve dış ticaret operasyon ekipleri"
                ],
                "dates": [{"label": "Yürürlük Tarihi", "text": "Yayımı tarihinde (15.05.2024) yürürlüğe girmiştir."}],
                "note": "Denetime tabi ürünlerin TAREKS başvurularında güncel parametreler aranacaktır.",
                "source_url": "https://www.resmigazete.gov.tr/eskiler/2024/05/20240515-3.htm",
                "source_label": "32547 Sayılı Resmî Gazete"
            },
            {
                "type": "Yeni Tebliğ",
                "title": "İthalatta Haksız Rekabetin Önlenmesine İlişkin Tebliğ",
                "teblig_no": "No: 2024/17",
                "summary": "Belirli menşeli ürünlerin ithalatında dampinge karşı kesin önlem uygulanmasına karar verilmiştir.",
                "who_affected": [
                    "İlgili GTİP kapsamındaki ürünleri ithal eden firmalar",
                    "Yetkilendirilmiş Gümrük Müşavirleri"
                ],
                "dates": [{"label": "Yürürlük Tarihi", "text": "Yayımı tarihinde yürürlüğe girmiştir."}],
                "note": "Serbest dolaşıma giriş beyannamelerinde dampinge karşı ek mali yükümlülük tahakkuk ettirilecektir.",
                "source_url": "https://www.resmigazete.gov.tr/eskiler/2024/05/20240515-4.htm",
                "source_label": "32547 Sayılı Resmî Gazete"
            },
            {
                "type": "Kaldırma",
                "title": "Tıbbi Tanı Kitlerinin İthaline İlişkin Tebliğ (İthalat: 2024/19)'in Yürürlükten Kaldırılmasına Dair Tebliğ",
                "teblig_no": "İthalat: 2024/19",
                "summary": "Tıbbi tanı kitleri ithalatına ilişkin önceki kısıtlayıcı düzenleme yürürlükten kaldırılmıştır.",
                "who_affected": [
                    "Medikal ve sağlık sektörü ithalatçıları",
                    "Gümrük Müşavirliği firmaları"
                ],
                "dates": [{"label": "Yürürlük Tarihi", "text": "15.05.2024 tarihi itibarıyla yürürlükten kaldırılmıştır."}],
                "note": "Beyanname tescillerinde önceki tebliğ kapsamında aranan özel izin şartı aranmayacaktır.",
                "source_url": "https://www.resmigazete.gov.tr/eskiler/2024/05/20240515-5.htm",
                "source_label": "32547 Sayılı Resmî Gazete"
            }
        ]
    }

    print("1. HTML ve Tek Sayfa PDF Raporu Oluşturuluyor...")
    gen = ReportGenerator("2024-05-15")
    html_p = gen.generate_html(data)
    pdf_p = gen.generate_pdf(html_p)
    print(f"✅ HTML oluşturuldu: {html_p}")
    print(f"✅ PDF oluşturuldu: {pdf_p}")

    print("\n2. Google Drive'a Yükleniyor...")
    links = gen.upload_reports_to_drive()
    print(f"✅ Drive Yükleme Başarılı:")
    for k, v in links.items():
        print(f"  - {k}: {v}")

    print("\n3. E-posta Grubu Çözümleme Testi...")
    mailer = ReportMailer()
    resolved = mailer.resolve_recipients("test1")
    print(f"✅ 'test1' grubu alıcıları: {resolved}")

if __name__ == "__main__":
    main()
