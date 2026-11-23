import os
import logging
import json
import re
from typing import Optional, List

import openai

from src.config_manager import config
from src.model_helpers import ModelHelpers
from src.llm_models.cleanup import output_cleanup

logging.basicConfig(level=logging.INFO)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Map short model names to OpenRouter model IDs
MODEL_ALIASES = {
    "claude-3.7-sonnet": "anthropic/claude-3.7-sonnet",
}


class OpenRouterModel:
    """Claude via OpenRouter using the OpenAI-compatible API."""

    def __init__(self, context, key=None, model="claude-3.7-sonnet", **kwargs):
        self.model_alias = model
        self.model = MODEL_ALIASES.get(model, model)
        self.context = context
        self.helper = ModelHelpers()
        self.temperature: float = config.get("TEMPERATURE", 0.0)
        self.debug: bool = config.get("LLM_INSTANCE_DEBUG", False)
        self.model_timeout = config.get("MODEL_TIMEOUT", 300)
        if not isinstance(self.model_timeout, int):
            self.model_timeout = 300

        api_key = (
            key
            or os.getenv("OPENROUTER_API_KEY")
            or config.get("OPENROUTER_API_KEY")
        )
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY not set")

        self.client = openai.OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
        logging.info(f"Created OpenRouter model wrapper. Using model: {self.model}")

    def set_debug(self, debug: bool = True) -> None:
        self.debug = debug

    def _create_system_prompt(self, schema: Optional[str], category: Optional[int]) -> str:
        return self.helper.create_system_prompt(self.context, schema, category)

    def _concat_prompts(self, *prompts: str) -> str:
        return "\n\n----------------------------------------\n".join(prompts)

    def _log(self, prompt_log: str, system_prompt: str, prompt: str) -> None:
        try:
            os.makedirs(os.path.dirname(prompt_log), exist_ok=True)
            temp_log = f"{prompt_log}.tmp"
            with open(temp_log, "w+") as f:
                f.write(self._concat_prompts(system_prompt, prompt))
            os.replace(temp_log, prompt_log)
        except Exception as e:
            logging.error(f"Failed to write prompt log to {prompt_log}: {str(e)}")
            raise

    def call_model(self, system_prompt: str, prompt: str, timeout: int) -> List[str]:
        if self.debug:
            print(f"Requesting prompt using the model: {self.model}")
            print(f"System prompt: {system_prompt}")
            print(f"User prompt: {prompt}")

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        max_tokens = int(os.getenv("CLAUDE_MAX_TOKENS") or config.get("CLAUDE_MAX_TOKENS", 4096))

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=self.temperature,
            timeout=timeout,
        )

        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("Empty response content from model")
        return [text]

    def _extract_first_json_object(self, text: str):
        text = text.strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        for pattern in [r"```json\s*(\{.*?\})\s*```", r"```\s*(\{.*?\})\s*```"]:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    pass
        decoder = json.JSONDecoder()
        for i, ch in enumerate(text):
            if ch == "{":
                try:
                    obj, _ = decoder.raw_decode(text[i:])
                    return obj
                except Exception:
                    continue
        return None

    def _normalize_structured_response(self, content: str, schema: Optional[str], expected_single_file: bool) -> str:
        if expected_single_file or schema is None:
            return content
        parsed_json = self._extract_first_json_object(content)
        if parsed_json is not None:
            return json.dumps(parsed_json)
        return content

    def prompt(
        self,
        prompt: str,
        schema: str = None,
        prompt_log: str = "",
        files: Optional[list] = None,
        timeout: int = 60,
        category: Optional[int] = None,
        **kwargs,
    ):
        system_prompt = self._create_system_prompt(schema, category)
        if timeout == 60:
            timeout = self.model_timeout

        expected_single_file = bool(files) and len(files) == 1 and schema is None
        logging.info(f"FILES={files}  SCHEMA={schema}")

        if prompt_log:
            self._log(prompt_log, system_prompt, prompt)

        try:
            responses = self.call_model(system_prompt, prompt, timeout)
            for content in responses:
                if not content or not isinstance(content, str) or not content.strip():
                    raise ValueError("Empty response content from model")
                content = content.strip()

                if prompt_log:
                    try:
                        raw_log = f"{prompt_log}.raw_response.txt"
                        os.makedirs(os.path.dirname(raw_log), exist_ok=True)
                        temp_raw_log = f"{raw_log}.tmp"
                        with open(temp_raw_log, "w", encoding="utf-8") as f:
                            f.write(content)
                        os.replace(temp_raw_log, raw_log)
                    except Exception as e:
                        logging.warning(f"Failed to write raw response to {raw_log}: {e}")

                content = output_cleanup(content, files, rtl_only=(expected_single_file and schema is None))
                content = self._normalize_structured_response(content, schema, expected_single_file)

                if expected_single_file and files:
                    parsed = self.helper.parse_model_response(content, files, expected_single_file)
                    if isinstance(parsed, tuple) and len(parsed) == 2 and isinstance(parsed[1], bool):
                        return parsed
                    return parsed, True

                parsed = self.helper.parse_model_response(content, files, expected_single_file)
                if isinstance(parsed, tuple) and len(parsed) == 2 and isinstance(parsed[1], bool):
                    return parsed
                return parsed, True

            raise ValueError("Model returned no responses")

        except Exception as e:
            logging.error(f"Unable to get response from model: {str(e)}")
            if "429" in str(e) or "quota" in str(e).lower():
                raise RuntimeError("LLM_QUOTA_EXCEEDED")
            return "", False
