"""Gemini wrapper. Every method raises LLMError on any failure (quota, network, bad JSON)."""
from __future__ import annotations

import json
from datetime import datetime


class LLMError(Exception):
    pass


SYSTEM = (
    "You are a personal assistant whose replies are delivered as iMessage texts. "
    "Be concise and plain-text (no markdown headings, no tables). "
    "Never include the literal text 'agent:' in replies."
)


class GeminiLLM:
    def __init__(self, api_key: str, model: str):
        from google import genai  # imported lazily so tests don't need the package

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def _generate(self, prompt: str, json_mode: bool = False) -> str:
        from google.genai import types

        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        try:
            resp = self._client.models.generate_content(
                model=self._model, contents=prompt, config=cfg
            )
        except Exception as exc:  # quota (429), network, auth...
            raise LLMError(str(exc)) from exc
        text = (resp.text or "").strip()
        if not text:
            raise LLMError("empty response from Gemini")
        return text

    def _json(self, prompt: str) -> dict:
        raw = self._generate(prompt, json_mode=True)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Gemini returned invalid JSON: {raw[:200]}") from exc

    # ---- tasks ----
    def chat(self, text: str) -> str:
        return self._generate(text)

    def summarize_emails(self, emails: list[dict]) -> str:
        if not emails:
            return "No new email in the last 2 days."
        listing = "\n\n".join(
            f"From: {e['from']}\nSubject: {e['subject']}\nDate: {e['date']}\n"
            f"Unread: {e['unread']}\nPreview: {e['snippet']}"
            for e in emails
        )
        prompt = (
            "Here are my recent emails. Text me a short summary: first the IMPORTANT items "
            "(deadlines, school, money, people waiting on a reply, security alerts) with what "
            "I need to do, then one line counting the rest (newsletters/promos). Max ~12 lines.\n\n"
            + listing
        )
        return self._generate(prompt)

    def draft_email(self, to: str, instruction: str) -> dict:
        prompt = (
            f"Draft an email to {to}. What it should say: {instruction}\n"
            'Return JSON: {"subject": str, "body": str}. Natural tone, sign off without a name '
            "placeholder (no [Your Name])."
        )
        d = self._json(prompt)
        if not isinstance(d.get("subject"), str) or not isinstance(d.get("body"), str):
            raise LLMError("draft missing subject/body")
        return {"subject": d["subject"], "body": d["body"]}

    def parse_reminder(self, text: str, now_local: datetime) -> dict:
        prompt = (
            f"Current local date/time: {now_local.strftime('%A %Y-%m-%d %H:%M')}.\n"
            f"Reminder request: {text!r}\n"
            "Extract the task and the local date/time it is due. If no time is given, use 09:00. "
            "If only a weekday is given, use the next occurrence. "
            'Return JSON: {"task": str, "due_local": "YYYY-MM-DDTHH:MM"}'
        )
        d = self._json(prompt)
        try:
            due = datetime.fromisoformat(d["due_local"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError(f"bad reminder parse: {d}") from exc
        return {"task": str(d.get("task") or text), "due_local": due}

    def generate_code(self, request: str) -> dict:
        prompt = (
            "Write a small, complete, runnable project for this request:\n"
            f"{request}\n\n"
            "Include a README.md explaining setup and how to run it, and a requirements file if "
            "needed. Never hard-code secrets; read them from environment variables.\n"
            'Return JSON: {"name": "kebab-case-folder-name", "summary": "1-2 sentences", '
            '"files": [{"path": "relative/path", "content": "..."}]}'
        )
        d = self._json(prompt)
        files = d.get("files")
        if not isinstance(files, list) or not files:
            raise LLMError("no files generated")
        clean = []
        for f in files:
            path = str(f.get("path", "")).replace("\\", "/").lstrip("/")
            if not path or ".." in path.split("/"):
                continue
            clean.append({"path": path, "content": str(f.get("content", ""))})
        if not clean:
            raise LLMError("no valid file paths generated")
        return {
            "name": str(d.get("name") or "generated-project"),
            "summary": str(d.get("summary") or ""),
            "files": clean,
        }
