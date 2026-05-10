import anthropic


class AnthropicProvider:
    def __init__(self, api_key: str, model: str):
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
