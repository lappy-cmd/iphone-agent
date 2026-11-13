"""GeminiLLM.converse against a fake client that returns real google-genai response objects."""
import pytest
from google.genai import types

from agent.llm import GeminiLLM, LLMError
from agent.persona import TOOLS


def _resp(part: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))]
    )


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((list(contents), config))
        return self.responses.pop(0)


def make_llm(responses):
    llm = GeminiLLM.__new__(GeminiLLM)  # skip creating a real client
    llm._model = "test-model"
    llm._client = type("C", (), {})()
    llm._client.models = FakeModels(responses)
    return llm


def test_converse_runs_tool_then_answers():
    llm = make_llm([
        _resp(types.Part(function_call=types.FunctionCall(name="list_reminders", args={}))),
        _resp(types.Part(text="you have none")),
    ])
    seen = []

    def call_tool(name, args):
        seen.append((name, args))
        return {"reminders": []}

    history = [{"role": "model", "text": "stale"}, {"role": "user", "text": "any reminders?"}]
    assert llm.converse("SYS", history, TOOLS, call_tool) == "you have none"
    assert seen == [("list_reminders", {})]

    first_contents, cfg = llm._client.models.calls[0]
    assert [c.role for c in first_contents] == ["user"]  # leading model turn dropped
    assert cfg.system_instruction == "SYS"
    assert cfg.automatic_function_calling.disable is True
    names = {d.name for d in cfg.tools[0].function_declarations}
    assert names == {t["name"] for t in TOOLS}

    second_contents, _ = llm._client.models.calls[1]
    assert [c.role for c in second_contents] == ["user", "model", "user"]
    fr = second_contents[-1].parts[0].function_response
    assert fr.name == "list_reminders" and fr.response == {"reminders": []}


def test_converse_errors():
    llm = make_llm([_resp(types.Part(text=""))])
    with pytest.raises(LLMError):
        llm.converse("SYS", [{"role": "user", "text": "hi"}], TOOLS, lambda n, a: {})

    loop = _resp(types.Part(function_call=types.FunctionCall(name="list_reminders", args={})))
    llm = make_llm([loop] * 6)
    with pytest.raises(LLMError, match="too many"):
        llm.converse("SYS", [{"role": "user", "text": "hi"}], TOOLS, lambda n, a: {})

    class Boom:
        def generate_content(self, **kw):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    llm = make_llm([])
    llm._client.models = Boom()
    with pytest.raises(LLMError, match="429"):
        llm.converse("SYS", [{"role": "user", "text": "hi"}], TOOLS, lambda n, a: {})


# ---- Claude ----
from types import SimpleNamespace as NS  # noqa: E402

import anthropic  # noqa: E402
import httpx2  # noqa: E402

from agent.llm import ClaudeLLM, FallbackLLM  # noqa: E402


def _msg(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop)


def _text(t):
    return NS(type="text", text=t)


def _tool(name, args, id_="tu_1"):
    return NS(type="tool_use", name=name, input=args, id=id_)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        if isinstance(self.msg, Exception):
            raise self.msg
        return self.msg


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def stream(self, **kw):
        import copy

        self.calls.append(copy.deepcopy(kw))
        return FakeStream(self.responses.pop(0))


def make_claude(responses):
    llm = ClaudeLLM.__new__(ClaudeLLM)
    llm._anthropic = anthropic
    llm._model = "claude-test"
    fake = FakeMessages(responses)
    llm._client = NS(beta=NS(messages=fake))
    return llm, fake


def test_claude_runs_tool_then_answers():
    llm, fake = make_claude([
        _msg(_text("checking"), _tool("list_reminders", {}), stop="tool_use"),
        _msg(_text("you have none")),
    ])
    seen = []

    def call_tool(name, args):
        seen.append((name, args))
        return {"reminders": []}

    history = [
        {"role": "model", "text": "stale"},
        {"role": "user", "text": "any reminders?"},
        {"role": "model", "text": "none"},
        {"role": "user", "text": "sure?"},
    ]
    assert llm.converse("SYS", history, TOOLS, call_tool) == "you have none"
    assert seen == [("list_reminders", {})]

    first = fake.calls[0]
    assert first["model"] == "claude-test" and first["system"] == "SYS"
    assert first["fallbacks"] == "default"
    assert [m["role"] for m in first["messages"]] == ["user", "assistant", "user"]
    assert {t["name"] for t in first["tools"]} == {t["name"] for t in TOOLS}
    assert all("input_schema" in t for t in first["tools"])

    second = fake.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user", "assistant", "user"]
    result = second[-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1"
    assert result["content"] == '{"reminders": []}'


def test_claude_errors():
    hi = [{"role": "user", "text": "hi"}]
    llm, _ = make_claude([_msg(_text(""))])
    with pytest.raises(LLMError, match="empty"):
        llm.converse("SYS", hi, TOOLS, lambda n, a: {})

    llm, _ = make_claude([_msg(stop="refusal")])
    with pytest.raises(LLMError, match="declined"):
        llm.converse("SYS", hi, TOOLS, lambda n, a: {})

    llm, _ = make_claude([_msg(_tool("list_reminders", {}), stop="tool_use")] * 6)
    with pytest.raises(LLMError, match="too many"):
        llm.converse("SYS", hi, TOOLS, lambda n, a: {})

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(402, request=req)
    err = anthropic.APIStatusError("credit balance too low", response=resp, body=None)
    llm, _ = make_claude([err])
    with pytest.raises(LLMError, match="402"):
        llm.converse("SYS", hi, TOOLS, lambda n, a: {})


def test_claude_generate_code_uses_schema():
    body = '{"name": "app", "summary": "s", "files": [{"path": "/a.py", "content": "x"}]}'
    llm, fake = make_claude([_msg(_text(body))])
    out = llm.generate_code("a thing")
    assert out["files"] == [{"path": "a.py", "content": "x"}]
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"


class Scripted:
    def __init__(self, fail=False, tool_first=False):
        self.fail, self.tool_first, self.calls = fail, tool_first, 0

    def converse(self, system, history, tools, call_tool, max_steps=6):
        self.calls += 1
        if self.tool_first:
            call_tool("draft_email", {})
        if self.fail:
            raise LLMError("boom")
        return "ok"

    def style_profile(self, msgs, prev):
        if self.fail:
            raise LLMError("boom")
        return "style"


def test_fallback_uses_backup_only_before_any_tool_ran():
    primary, backup = Scripted(fail=True), Scripted()
    assert FallbackLLM(primary, backup).converse("S", [], [], lambda n, a: {}) == "ok"
    assert backup.calls == 1

    primary, backup = Scripted(fail=True, tool_first=True), Scripted()
    with pytest.raises(LLMError):
        FallbackLLM(primary, backup).converse("S", [], [], lambda n, a: {})
    assert backup.calls == 0  # never repeat an action

    assert FallbackLLM(Scripted(fail=True), Scripted()).style_profile([], "") == "style"
    with pytest.raises(LLMError):
        FallbackLLM(Scripted(fail=True), Scripted(fail=True)).style_profile([], "")
