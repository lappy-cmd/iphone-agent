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
