import anthropic


class AnthropicProvider:
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

    def complete(self, system_prompt: str, messages: list[dict]) -> tuple[str, int, int]:
        """Returns (reply_text, input_tokens, output_tokens)."""
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system_prompt,
            messages=messages,
        )
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        return response.content[0].text, input_tokens, output_tokens
