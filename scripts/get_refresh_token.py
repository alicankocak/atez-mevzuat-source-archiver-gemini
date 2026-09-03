import os
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]

def main():
    client_id = os.getenv("GOOGLE_CLIENT_ID") or input("Google Client ID girin: ").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or input("Google Client Secret girin: ").strip()

    if not (client_id and client_secret):
        print("Hata: Client ID ve Client Secret zorunludur.")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("\nGoogle Drive Yetkilendirme Başlatılıyor...")
    print("Tarayıcınızda açılacak sayfadan Google hesabınızı seçip onaylayın.\n")
    
    flow = InstalledAppFlow.from_client_config(
        client_config,
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    
    print("\n✅ Yetkilendirme Başarılı!\n")
    print("----------------------------------------------------------------")
    print(f"GOOGLE_REFRESH_TOKEN:\n{creds.refresh_token}\n")
    print("----------------------------------------------------------------")

    env_content = f"""# Google Drive Credentials
GOOGLE_CLIENT_ID={creds.client_id}
GOOGLE_CLIENT_SECRET={creds.client_secret}
GOOGLE_REFRESH_TOKEN={creds.refresh_token}
DRIVE_ROOT_FOLDER_ID=1TONr3xXlRbOfClJKPZJ_Q3mbFqzBEiTE
GITHUB_REPO=alicankocak/atez-mevzuat-source-archiver-gemini
"""
    env_path = Path("/Users/alican/Documents/Mevzuat-Monitor/.env")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_content)
    
    print(f"📁 .env dosyası oluşturuldu: {env_path}")
    print("Yukarıdaki GOOGLE_REFRESH_TOKEN değerini GitHub Secrets'a ekleyin.")

if __name__ == "__main__":
    main()
