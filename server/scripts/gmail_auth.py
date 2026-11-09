"""One-time Gmail sign-in. Run on your LAPTOP (it opens a browser):

    python scripts/gmail_auth.py path/to/credentials.json

Produces token.json — copy it to the server next to .env.
"""
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

if __name__ == "__main__":
    creds_file = sys.argv[1] if len(sys.argv) > 1 else "credentials.json"
    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    with open("token.json", "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    print("Saved token.json — copy it to the server (same folder as .env).")
