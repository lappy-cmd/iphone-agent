"""Gemini wrapper. Every method raises LLMError on any failure (quota, network, bad JSON)."""
from __future__ import annotations

import json
from typing import Any, Callable


class LLMError(Exception):
    pass


PLAIN = (
    "Your output is delivered as an iMessage text: plain text only, no markdown "
    "(no **, #, tables or code fences)."
)


def _jsonable(value: Any) -> dict:
    """Function responses must be JSON objects."""
    try:
        json.dumps(value)
    except TypeError:
        value = json.loads(json.dumps(value, default=str))
    return value if isinstance(value, dict) else {"result": value}


class GeminiLLM:
    def __init__(self, api_key: str, model: str):
        from google import genai  # imported lazily so tests don't need the package

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def _call(self, contents, config):
        try:
            return self._client.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except Exception as exc:  # quota (429), network, auth...
            raise LLMError(str(exc)) from exc

    def _generate(self, prompt: str, json_mode: bool = False) -> str:
        from google.genai import types

        cfg = types.GenerateContentConfig(
            system_instruction=PLAIN,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        text = (self._call(prompt, cfg).text or "").strip()
        if not text:
            raise LLMError("empty response from Gemini")
        return text

    def _json(self, prompt: str) -> dict:
        raw = self._generate(prompt, json_mode=True)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Gemini returned invalid JSON: {raw[:200]}") from exc

    # ---- conversation with tools ----
    def converse(
        self,
        system: str,
        history: list[dict],
        tools: list[dict],
        call_tool: Callable[[str, dict], dict],
        max_steps: int = 6,
    ) -> str:
        """Run a function-calling loop. history: [{"role": "user"|"model", "text": str}, ...]."""
        from google.genai import types

        decls = [
            types.FunctionDeclaration(
                name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]
            )
            for t in tools
        ]
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        while history and history[0]["role"] != "user":
            history = history[1:]
        contents = [
            types.Content(role=m["role"], parts=[types.Part.from_text(text=m["text"])])
            for m in history
        ]
        for _ in range(max_steps):
            resp = self._call(contents, cfg)
            calls = resp.function_calls or []
            if not calls:
                text = (resp.text or "").strip()
                if not text:
                    raise LLMError("empty response from Gemini")
                return text
            contents.append(resp.candidates[0].content)
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name=c.name, response=_jsonable(call_tool(c.name, dict(c.args or {})))
                        )
                        for c in calls
                    ],
                )
            )
        raise LLMError("too many tool steps")

    # ---- single-shot tasks ----
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

    def style_profile(self, user_messages: list[str], previous: str) -> str:
        """Describe how the user texts, so replies can mirror it."""
        sample = "\n".join(f"- {m}" for m in user_messages)
        prompt = (
            "These are recent text messages a person sent to their assistant:\n"
            f"{sample}\n\n"
            f"Previous notes on their style (may be empty): {previous or '(none)'}\n\n"
            "In at most 5 short lines, describe HOW they text so the assistant can mirror it: "
            "typical length, capitalization, punctuation, emoji use, slang/abbreviations, tone. "
            "Update the previous notes rather than starting over. Don't describe the content of "
            "the messages, only the style."
        )
        return self._generate(prompt)

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
