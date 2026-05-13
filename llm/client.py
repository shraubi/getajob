import json
import litellm
from tenacity import retry, stop_after_attempt, wait_exponential

litellm.drop_params = True


def _build_messages(system: str | None, messages: list) -> list:
    if system:
        return [{"role": "system", "content": system}] + messages
    return messages


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def chat(
    model: str,
    messages: list,
    system: str | None = None,
    tools: list | None = None,
    **kwargs,
) -> litellm.ModelResponse:
    params: dict = {
        "model": model,
        "messages": _build_messages(system, messages),
        **kwargs,
    }
    if tools:
        params["tools"] = tools
        params["tool_choice"] = "auto"
    return await litellm.acompletion(**params)


def extract_tool_calls(response: litellm.ModelResponse) -> list:
    msg = response.choices[0].message
    return msg.tool_calls or []


def build_assistant_turn(response: litellm.ModelResponse) -> dict:
    msg = response.choices[0].message
    turn: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        turn["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return turn


def build_tool_result(tool_call_id: str, result) -> dict:
    content = result if isinstance(result, str) else json.dumps(result)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
