"""AI wrappers (Claude, Gemini, and Claude-with-Gemini-backup).

Every method raises LLMError on any failure (quota, network, bad JSON, refusal).
"""
from __future__ import annotations

import json
from typing import Any, Callable


class LLMError(Exception):
    pass


PLAIN = (
    "Your output is delivered as an iMessage text: plain text only, no markdown "
    "(no **, #, tables or code fences)."
)

CODE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "summary", "files"],
    "additionalProperties": False,
}


PICK_SCHEMA = {
    "type": "object",
    "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
    "required": ["paths"],
    "additionalProperties": False,
}

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "commit_message": {"type": "string"},
        "files": CODE_SCHEMA["properties"]["files"],
    },
    "required": ["summary", "commit_message", "files"],
    "additionalProperties": False,
}

SELF_EDIT_RULES = (
    "You are changing the source code of a texting AI agent: a FastAPI server in server/agent "
    "(core.py = behaviour and tools, persona.py = system prompt and tool list, llm.py = AI backends), "
    "tests in server/tests, a Windows laptop location helper in helper/. The change goes live "
    "automatically only if all tests pass, so keep existing tests passing and add or update tests "
    "for new behaviour. Never weaken security: keep the owner-only checks, confirmations, key "
    "checks and secret handling. Never print, log or send secrets. Keep changes minimal and in the "
    "existing style."
)


def _jsonable(value: Any) -> dict:
    """Function responses must be JSON objects."""
    try:
        json.dumps(value)
    except TypeError:
        value = json.loads(json.dumps(value, default=str))
    return value if isinstance(value, dict) else {"result": value}


class _Tasks:
    """Single-shot jobs shared by every backend. Subclasses provide _generate and _json."""

    def _generate(self, prompt: str) -> str:
        raise NotImplementedError

    def _json(self, prompt: str, schema: dict) -> dict:
        raise NotImplementedError

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

    def choose_files(self, request: str, paths: list[str]) -> list[str]:
        """Pick the repo files needed to make a change to the agent's own code."""
        prompt = (
            f"{SELF_EDIT_RULES}\n\nRequested change:\n{request}\n\nRepository files:\n"
            + "\n".join(paths)
            + '\n\nReturn JSON {"paths": [...]} with the files you need to read to make this change '
            "(the ones to edit, their tests, and anything needed to understand them). At most 12."
        )
        picked = self._json(prompt, PICK_SCHEMA).get("paths") or []
        return [p for p in picked if p in paths][:12]

    def edit_code(self, request: str, files: dict[str, str]) -> dict:
        """Return {summary, commit_message, files: [{path, content}]} with FULL new contents."""
        listing = "\n\n".join(f"=== {path} ===\n{text}" for path, text in files.items())
        prompt = (
            f"{SELF_EDIT_RULES}\n\nRequested change:\n{request}\n\nCurrent files:\n\n{listing}"
            "\n\nReturn JSON with: summary (1-2 plain sentences for a text message), commit_message, "
            "and files = every file you change or create, each with its COMPLETE new content (not a "
            "diff). Leave out files you don't change."
        )
        d = self._json(prompt, EDIT_SCHEMA)
        changed = [
            {"path": str(f.get("path", "")), "content": str(f.get("content", ""))}
            for f in d.get("files") or []
            if f.get("path")
        ]
        if not changed:
            raise LLMError("no files changed")
        return {
            "summary": str(d.get("summary") or "").strip(),
            "commit_message": str(d.get("commit_message") or "agent: self edit").strip()[:200],
            "files": changed,
        }

    def generate_code(self, request: str) -> dict:
        prompt = (
            "Write a small, complete, runnable project for this request:\n"
            f"{request}\n\n"
            "Include a README.md explaining setup and how to run it, and a requirements file if "
            "needed. Never hard-code secrets; read them from environment variables.\n"
            'Return JSON: {"name": "kebab-case-folder-name", "summary": "1-2 sentences", '
            '"files": [{"path": "relative/path", "content": "..."}]}'
        )
        d = self._json(prompt, CODE_SCHEMA)
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


class ClaudeLLM(_Tasks):
    BETAS = ["server-side-fallback-2026-07-01"]  # if Claude declines, Anthropic retries on another model

    def __init__(self, api_key: str, model: str):
        import anthropic  # imported lazily so tests don't need the package

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def _call(self, effort: str = "low", max_tokens: int = 16000, **kw):
        a = self._anthropic
        if "haiku" not in self._model:  # Haiku has no server-side refusal fallback
            kw.update(betas=self.BETAS, fallbacks="default")
        try:
            with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=max_tokens,
                output_config={"effort": effort, **kw.pop("output_config", {})},
                **kw,
            ) as stream:
                msg = stream.get_final_message()
        except a.APIStatusError as exc:  # 400 bad request, 401 key, 402/403 credits, 429, 5xx
            raise LLMError(f"claude {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:  # network / timeout
            raise LLMError(f"claude connection: {exc}") from exc
        if msg.stop_reason == "refusal":
            raise LLMError("claude declined the request")
        return msg

    @staticmethod
    def _text(msg) -> str:
        return "".join(b.text for b in msg.content if b.type == "text").strip()

    def _generate(self, prompt: str) -> str:
        text = self._text(self._call(system=PLAIN, messages=[{"role": "user", "content": prompt}]))
        if not text:
            raise LLMError("empty response from Claude")
        return text

    def _json(self, prompt: str, schema: dict) -> dict:
        msg = self._call(
            effort="high",
            max_tokens=64000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if msg.stop_reason == "max_tokens":
            raise LLMError("project too large for one reply")
        raw = self._text(msg)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Claude returned invalid JSON: {raw[:200]}") from exc

    def converse(
        self,
        system: str,
        history: list[dict],
        tools: list[dict],
        call_tool: Callable[[str, dict], dict],
        max_steps: int = 6,
    ) -> str:
        """Run a tool-use loop. history: [{"role": "user"|"model", "text": str}, ...]."""
        defs = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]
        while history and history[0]["role"] != "user":
            history = history[1:]
        messages: list[dict] = [
            {"role": "assistant" if m["role"] == "model" else "user", "content": m["text"]}
            for m in history
            if m.get("text")
        ]
        for _ in range(max_steps):
            msg = self._call(system=system, tools=defs, messages=messages)
            calls = [b for b in msg.content if b.type == "tool_use"]
            if not calls:
                text = self._text(msg)
                if not text:
                    raise LLMError("empty response from Claude")
                return text
            if msg.stop_reason == "max_tokens":
                raise LLMError("reply cut off mid tool call")
            messages.append({"role": "assistant", "content": msg.content})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": c.id,
                            "content": json.dumps(_jsonable(call_tool(c.name, dict(c.input or {})))),
                        }
                        for c in calls
                    ],
                }
            )
        raise LLMError("too many tool steps")


class GeminiLLM(_Tasks):
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

    def _json(self, prompt: str, schema: dict) -> dict:
        raw = self._generate(prompt, json_mode=True)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Gemini returned invalid JSON: {raw[:200]}") from exc

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


class FallbackLLM:
    """Use `primary`; if it fails, retry on `backup`.

    A conversation only falls back if the primary failed before running any tool, so an
    action (like sending an email) can never happen twice.
    """

    def __init__(self, primary, backup, log: Callable[[str], None] | None = None):
        self.primary = primary
        self.backup = backup
        self._log = log or (lambda _msg: None)

    def converse(self, system, history, tools, call_tool, max_steps: int = 6) -> str:
        ran_tool = False

        def tracked(name, args):
            nonlocal ran_tool
            ran_tool = True
            return call_tool(name, args)

        try:
            return self.primary.converse(system, history, tools, tracked, max_steps)
        except LLMError as exc:
            if ran_tool:
                raise
            self._log(f"primary AI failed, using backup: {exc}")
            return self.backup.converse(system, history, tools, call_tool, max_steps)

    def _either(self, method: str, *args):
        try:
            return getattr(self.primary, method)(*args)
        except LLMError as exc:
            self._log(f"primary AI failed, using backup: {exc}")
            return getattr(self.backup, method)(*args)

    def summarize_emails(self, emails):
        return self._either("summarize_emails", emails)

    def style_profile(self, user_messages, previous):
        return self._either("style_profile", user_messages, previous)

    def generate_code(self, request):
        return self._either("generate_code", request)

    def choose_files(self, request, paths):
        return self._either("choose_files", request, paths)

    def edit_code(self, request, files):
        return self._either("edit_code", request, files)
