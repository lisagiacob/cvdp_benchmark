import os
import logging
from typing import Optional, List

from google import genai
from google.genai import types

from src.config_manager import config
from src.model_helpers import ModelHelpers
from src.llm_models.cleanup import output_cleanup

logging.basicConfig(level=logging.INFO)


class GeminiModel:
    """Gemini wrapper that mirrors the project's base_llm.py behavior."""

    def __init__(self, context, key=None, model="gemini-2.5-flash", **kwargs):
        """
        Initialize a Gemini LLM.
        
        Args:
            context: Not used for scoring models
            key: Gemini API key (will fall back to GEMINI_API_KEY environment variable)
            model: The model version to use
        """
        self.model = model or config.get("DEFAULT_MODEL")
        self.context = context

        # Helpers used by the benchmark to build system prompts and parse outputs
        self.helper = ModelHelpers()

        self.temperature: float = config.get("TEMPERATURE", 0.0)
        self.debug: bool = config.get("LLM_INSTANCE_DEBUG", False)

        api_key = key or os.getenv("GEMINI_API_KEY") or config.get("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY (or GOOGLE_API_KEY) in environment")

        self.client = genai.Client(api_key=api_key)

        # Timeout config (used when caller passes default)
        self.model_timeout = config.get("MODEL_TIMEOUT", 60)
        if not isinstance(self.model_timeout, int):
            self.model_timeout = 60

        logging.info(f"Created Gemini model wrapper. Using model: {self.model}")

    def set_debug(self, debug: bool = True) -> None:
        self.debug = debug
        logging.info(f"Debug mode {'enabled' if debug else 'disabled'}")

    def _create_system_prompt(self, schema: Optional[str], category: Optional[int]) -> str:
        return self.helper.create_system_prompt(self.context, schema, category)

    def _concat_prompts(self, *prompts: str) -> str:
        return "\n\n----------------------------------------\n".join(prompts)

    # Log the combined system and user prompt to a file, ensuring atomic write.
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

    # Call the Gemini API with the combined system and user prompt, and return a list of candidate strings.
    def call_model(self, system_prompt: str, prompt: str, timeout: int) -> List[str]:
        """Provider-specific API call. Returns a list of candidate strings."""
        # The google-genai SDK does not currently expose an explicit per-request timeout
        # in the same way as httpx; we keep `timeout` for API compatibility.
        content = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

        if self.debug:
            print(f"Requesting prompt using the model: {self.model}")
            print(f"System prompt: {system_prompt}")
            print(f"User prompt: {prompt}")
            print(f"Request parameters: model={self.model}, timeout={timeout}")

        resp = self.client.models.generate_content(
            model=self.model,
            contents=content,
            config=types.GenerateContentConfig(
                temperature=self.temperature,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                )
            ),
        )
        text = (resp.text or "").strip()
        return [text]

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
        """Return (output, success) as expected by dataset_processor."""
        system_prompt = self._create_system_prompt(schema, category)

        # Use configured timeout if default
        if timeout == 60:
            timeout = self.model_timeout  # o config.get("MODEL_TIMEOUT", 60) se preferisci

        expected_single_file = files and len(files) == 1 and schema is None

        logging.info(f"FILES={files}  SCHEMA={schema}")

        if prompt_log:
            self._log(prompt_log, system_prompt, prompt)

        try:
            responses = self.call_model(system_prompt, prompt, timeout)
            for content in responses:
                if not content or not isinstance(content, str) or not content.strip():
                    raise ValueError("Empty response content from model")

                content = content.strip()

                # Save raw model output before any cleanup for debugging
                if prompt_log:
                    try:
                        raw_log = f"{prompt_log}.raw_response.txt"
                        os.makedirs(os.path.dirname(raw_log), exist_ok=True)
                        temp_raw_log = f"{raw_log}.tmp"
                        with open(temp_raw_log, "w", encoding="utf-8") as f:
                            f.write(content)
                        os.replace(temp_raw_log, raw_log)
                    except Exception as raw_log_error:
                        logging.warning(f"Failed to write raw Gemini response to {raw_log}: {raw_log_error}")

                content = output_cleanup(content, files, rtl_only=(expected_single_file and schema is None))

                # Single-file direct-text mode: let the benchmark helper parse the model output
                # into the exact structure expected by CVDP.
                if expected_single_file and files:
                    parsed = self.helper.parse_model_response(content, files, expected_single_file)

                    # parse_model_response may already return (output, success)
                    if isinstance(parsed, tuple) and len(parsed) == 2 and isinstance(parsed[1], bool):
                        return parsed

                    # Otherwise assume success
                    return parsed, True

                parsed = self.helper.parse_model_response(content, files, expected_single_file)

                # parse_model_response può restituire (output, success)
                if isinstance(parsed, tuple) and len(parsed) == 2 and isinstance(parsed[1], bool):
                    output, ok = parsed
                else:
                    output, ok = parsed, True

                return output, ok

            raise ValueError("Model returned no responses")
        
        except Exception as e:
            logging.error(f"Unable to get response from model: {str(e)}")

            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                raise RuntimeError("LLM_QUOTA_EXCEEDED")

            return "", False