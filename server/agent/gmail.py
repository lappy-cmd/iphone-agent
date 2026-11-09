"""Gmail API access: read recent mail, send mail, send '[agent]' notifications to yourself."""
from __future__ import annotations

import base64
from email.message import EmailMessage

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
NOTIFY_SUBJECT_PREFIX = "[agent]"


class GmailAuthError(Exception):
    pass


class GmailError(Exception):
    pass


class Gmail:
    def __init__(self, token_path: str, my_email: str, notify_to: str = ""):
        self._token_path = token_path
        self._my_email = my_email
        self._notify_to = notify_to or my_email
        self._service = None

    def _svc(self):
        if self._service is not None:
            return self._service
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        try:
            creds = Credentials.from_authorized_user_file(self._token_path, SCOPES)
            if not creds.valid:
                creds.refresh(Request())
                with open(self._token_path, "w", encoding="utf-8") as fh:
                    fh.write(creds.to_json())
        except (FileNotFoundError, RefreshError, ValueError) as exc:
            raise GmailAuthError(str(exc)) from exc
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def _call(self, request):
        from google.auth.exceptions import RefreshError

        try:
            return request.execute()
        except RefreshError as exc:
            self._service = None
            raise GmailAuthError(str(exc)) from exc
        except Exception as exc:
            raise GmailError(str(exc)) from exc

    def recent(self, max_results: int = 25) -> list[dict]:
        svc = self._svc()
        listing = self._call(
            svc.users().messages().list(
                userId="me", q="newer_than:2d -in:sent -in:chats", maxResults=max_results
            )
        )
        out = []
        for m in listing.get("messages", []):
            msg = self._call(
                svc.users().messages().get(
                    userId="me",
                    id=m["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            if headers.get("Subject", "").startswith(NOTIFY_SUBJECT_PREFIX):
                continue  # skip our own notification emails
            out.append(
                {
                    "from": headers.get("From", ""),
                    "subject": headers.get("Subject", "(no subject)"),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                    "unread": "UNREAD" in msg.get("labelIds", []),
                }
            )
        return out

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = self._my_email
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self._call(self._svc().users().messages().send(userId="me", body={"raw": raw}))

    def notify_self(self, text: str) -> None:
        """Email yourself; the iPhone 'Agent Out' automation turns it into an iMessage."""
        first_line = text.splitlines()[0][:60] if text else ""
        self.send(self._notify_to, f"{NOTIFY_SUBJECT_PREFIX} {first_line}", text)
