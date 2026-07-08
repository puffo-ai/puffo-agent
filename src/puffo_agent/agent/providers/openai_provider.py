from openai import OpenAI


class OpenAIProvider:
    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        # ``base_url`` routes completions through a proxy (e.g. a LiteLLM
        # virtual-key endpoint) instead of api.openai.com. Passed to the
        # client only when set so the default stays the vendor endpoint —
        # byte-for-byte unchanged when no base_url is given.
        if base_url:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            self.client = OpenAI(api_key=api_key)
        self.model = model

    def complete(self, system_prompt: str, messages: list[dict]) -> tuple[str, int, int]:
        """Returns (reply_text, input_tokens, output_tokens)."""
        all_messages = [{"role": "system", "content": system_prompt}] + messages
        response = self.client.chat.completions.create(
            model=self.model,
            messages=all_messages,
            max_tokens=2048,
        )
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        return response.choices[0].message.content, input_tokens, output_tokens
