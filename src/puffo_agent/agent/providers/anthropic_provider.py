from dataclasses import dataclass, field
from typing import Callable, Optional

import anthropic


@dataclass
class CompletionResult:
    """Rich result of one ``AnthropicProvider.complete()`` call.

    ``tool_calls`` records every executed ``tool_use`` block as
    ``{"name", "input", "is_error"}`` in execution order;
    ``assistant_text_parts`` keeps the raw text blocks so the
    chat-only adapter can mirror SDKAdapter's metadata contract.
    """

    reply_text: str
    input_tokens: int
    output_tokens: int
    tool_calls: list[dict] = field(default_factory=list)
    assistant_text_parts: list[str] = field(default_factory=list)


# ``dispatch(tool_name, tool_input) -> str`` — blocking callback that
# executes one tool call and returns its textual result. Raising maps
# to an ``is_error`` tool_result so the model can recover in-loop.
ToolDispatch = Callable[[str, dict], str]


class AnthropicProvider:
    # Marker read by ChatOnlyAdapter: this provider accepts the
    # ``tools``/``dispatch`` kwargs and returns a CompletionResult.
    supports_tools = True

    # Hard cap on model→tool round-trips per turn so a pathological
    # tool loop can't spin forever against the API.
    MAX_TOOL_ITERATIONS = 8

    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        # ``base_url`` routes completions through a proxy (e.g. a LiteLLM
        # virtual-key endpoint) instead of api.anthropic.com. Passed to
        # the client only when set so the default stays the vendor
        # endpoint — byte-for-byte unchanged when no base_url is given.
        if base_url:
            self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        else:
            self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        dispatch: Optional[ToolDispatch] = None,
    ) -> CompletionResult:
        """Run one turn, executing structured tool calls in-loop.

        With ``tools`` set, the request advertises them and the loop
        re-calls the API while ``stop_reason == "tool_use"``: each
        ``tool_use`` block is executed via ``dispatch`` and fed back
        as a ``tool_result``. Without ``tools`` this is a single
        plain completion (legacy chat-local behavior).
        """
        convo = [dict(m) for m in messages]
        request_kwargs: dict = {}
        if tools:
            request_kwargs["tools"] = tools

        total_input = 0
        total_output = 0
        text_parts: list[str] = []
        executed_calls: list[dict] = []

        for _ in range(self.MAX_TOOL_ITERATIONS):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=system_prompt,
                messages=convo,
                **request_kwargs,
            )
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            # Iterate blocks by type — never blind-index [0].text: a
            # tool_use (or future block type) first would crash/mislead.
            tool_use_blocks = []
            for block in response.content:
                block_type = getattr(block, "type", "")
                if block_type == "text":
                    text_parts.append(block.text)
                elif block_type == "tool_use":
                    tool_use_blocks.append(block)

            if (
                response.stop_reason != "tool_use"
                or not tool_use_blocks
                or dispatch is None
            ):
                break

            # Echo the assistant turn (tool_use blocks included), then
            # answer every tool_use with a matching tool_result.
            convo.append({"role": "assistant", "content": response.content})
            tool_results: list[dict] = []
            for block in tool_use_blocks:
                tool_input = dict(block.input or {})
                try:
                    output = str(dispatch(block.name, tool_input))
                    is_error = False
                except Exception as exc:  # noqa: BLE001 — feed back to model
                    output = f"{type(exc).__name__}: {exc}"
                    is_error = True
                executed_calls.append({
                    "name": block.name,
                    "input": tool_input,
                    "is_error": is_error,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    "is_error": is_error,
                })
            convo.append({"role": "user", "content": tool_results})

        reply_text = "\n".join(p for p in text_parts if p).strip()
        return CompletionResult(
            reply_text=reply_text,
            input_tokens=total_input,
            output_tokens=total_output,
            tool_calls=executed_calls,
            assistant_text_parts=text_parts,
        )
