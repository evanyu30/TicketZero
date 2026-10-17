import copy
import json

from app.agent import run_tool_loop

TOOLS = [{"type": "function", "function": {"name": "add", "description": "Add two integers",
          "parameters": {"type": "object", "properties": {"a": {"type": "integer"},
          "b": {"type": "integer"}}, "required": ["a", "b"]}}}]


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id, name, args):
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, json.dumps(args))

    def model_dump(self, **kwargs):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, **kwargs):
        data = {"role": "assistant", "content": self.content,
                "tool_calls": [tc.model_dump() for tc in self.tool_calls] if self.tool_calls else None}
        if kwargs.get("exclude_none"):
            data = {k: v for k, v in data.items() if v is not None}
        return data


class FakeChoice:
    def __init__(self, message, finish_reason):
        self.message = message
        self.finish_reason = finish_reason
        self.index = 0


class FakeResponse:
    def __init__(self, content=None, tool_calls=None, finish_reason="stop"):
        self.choices = [FakeChoice(FakeMessage(content, tool_calls), finish_reason)]


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


class FakeChat:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)


class FakeClient:
    def __init__(self, responses):
        self.chat = FakeChat(responses)


def _field(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def test_assistant_tool_calls_message_precedes_tool_results():
    fake = FakeClient([
        FakeResponse(tool_calls=[FakeToolCall("call_1", "add", {"a": 2, "b": 3})],
                     finish_reason="tool_calls"),
        FakeResponse(content="5"),
    ])
    out = run_tool_loop(fake, "test-model", "What is 2 + 3?", TOOLS, {"add": lambda a, b: a + b})
    assert out == "5"
    second = fake.chat.completions.calls[1]["messages"]
    assert [_field(m, "role") for m in second] == ["user", "assistant", "tool"]
    tool_calls = _field(second[1], "tool_calls")
    assert tool_calls, "assistant message must carry its tool_calls"
    assert _field(tool_calls[0], "id") == "call_1"
    assert _field(second[2], "tool_call_id") == "call_1"
    assert "5" in str(_field(second[2], "content"))


def test_length_finish_retries_with_larger_max_tokens():
    fake = FakeClient([
        FakeResponse(content=None, finish_reason="length"),
        FakeResponse(content="done"),
    ])
    out = run_tool_loop(fake, "test-model", "hi", TOOLS, {}, max_tokens=256)
    assert out == "done"
    calls = fake.chat.completions.calls
    assert len(calls) == 2
    assert calls[1]["max_tokens"] > calls[0]["max_tokens"]
