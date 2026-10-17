"""Frozen acceptance tests. TicketZero's Repair agent must NOT edit this file."""
from app import TOOL_CALLS, ask, weather_card


def test_ask_uses_weather_tool():
    TOOL_CALLS.clear()
    reply = ask("What's the weather in San Francisco right now?")
    assert TOOL_CALLS, "get_weather was never called"
    assert "13.7" in reply, f"Reply did not report the tool's temperature: {reply!r}"


def test_weather_card_is_valid_json():
    card = weather_card("San Francisco")
    assert set(card) >= {"city", "temp_c", "advice"}, f"Missing keys: {card}"
    assert float(card["temp_c"]) == 13.7, f"Wrong temperature: {card}"
