"""Minimal tool-calling loop used by the support agent."""
import json


def run_tool_loop(client, model, user_msg, tools, tool_impls, max_tokens=1024, max_steps=6):
    """Run chat completions, executing tool calls, until the model returns a final answer.

    Returns the final answer text.
    """
    messages = [{"role": "user", "content": user_msg}]
    for _ in range(max_steps):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        message = choice.message
        if message.tool_calls:
            for call in message.tool_calls:
                impl = tool_impls[call.function.name]
                args = json.loads(call.function.arguments or "{}")
                result = impl(**args)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )
            continue
        return message.content
    return None
