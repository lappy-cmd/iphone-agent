"""Gmail API access: read recent mail, send mail, notifications, and Google Voice texts (via email)."""
from __future__ import annotations

import base64
import re
from email.message import EmailMessage

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
NOTIFY_SUBJECT_PREFIX = "[agent]"
VOICE_DOMAIN = "txt.voice.google.com"
# Google Voice appends this footer to forwarded texts; the message is everything before it.
_VOICE_FOOTER = re.compile(
    r"\n\s*(YOUR ACCOUNT\b|To respond to this text message|Google Voice\s*<)", re.IGNORECASE
)


def voice_text(body: str) -> str:
    """The text message inside a Google Voice notification email body."""
    body = body.replace("\r\n", "\n")
    m = _VOICE_FOOTER.search(body)
    return (body[: m.start()] if m else body).strip()


def _plain_body(payload: dict) -> str:
    """First text/plain part of a Gmail API message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _plain_body(part)
        if text:
            return text
    return ""


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
                userId="me",
                q=f"newer_than:2d -in:sent -in:chats -from:{VOICE_DOMAIN}",
                maxResults=max_results,
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

    # ---- Google Voice (texts forwarded to Gmail; replying to the email sends a text) ----
    def voice_messages(self, after_epoch: int) -> list[dict]:
        """Google Voice text notifications received after `after_epoch`, oldest first."""
        svc = self._svc()
        listing = self._call(
            svc.users().messages().list(
                userId="me", q=f"from:{VOICE_DOMAIN} after:{int(after_epoch)}", maxResults=20
            )
        )
        out = []
        for m in listing.get("messages", []):
            msg = self._call(svc.users().messages().get(userId="me", id=m["id"], format="full"))
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            out.append(
                {
                    "id": msg["id"],
                    "thread_id": msg.get("threadId", ""),
                    "internal_ms": int(msg.get("internalDate", 0)),
                    "from": headers.get("from", ""),
                    "subject": headers.get("subject", ""),
                    "message_id": headers.get("message-id", ""),
                    "text": voice_text(_plain_body(msg.get("payload", {}))),
                }
            )
        return sorted(out, key=lambda x: x["internal_ms"])

    def voice_reply(self, to: str, subject: str, text: str, thread_id: str = "", in_reply_to: str = "") -> None:
        """Reply to a Google Voice notification email; Google Voice delivers it as a text."""
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = self._my_email
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(text)
        body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            body["threadId"] = thread_id
        self._call(self._svc().users().messages().send(userId="me", body=body))
