import os
import logging
import json
import re
from typing import Optional, List

from anthropic import AnthropicVertex

from src.config_manager import config
from src.model_helpers import ModelHelpers
from src.llm_models.cleanup import output_cleanup

logging.basicConfig(level=logging.INFO)

class ClaudeModel:
    """Claude wrapper that mirrors the project's base_llm.py behavior."""

    def __init__(self, context, key=None, model="claude-sonnet-4-6", **kwargs):
        """
        Initialize a Claude LLM.
        
        Args:
            context: Not used for scoring models
            key: Not used (auth is via GCP WIF credentials), kept for API compatibility
            model: The Vertex model string (e.g. 'claude-sonnet-4-6')
        """
        self.model = model or config.get("DEFAULT_MODEL")
        self.context = context

        # Helpers used by the benchmark to build system prompts and parse outputs
        self.helper = ModelHelpers()

        self.temperature: float = config.get("TEMPERATURE", 0.0)
        self.debug: bool = config.get("LLM_INSTANCE_DEBUG", False)
        
        # GCP / Vertex config — from env or .env via config
        project_id = (
            os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
            or config.get("ANTHROPIC_VERTEX_PROJECT_ID")
            or "13524415676"
        )
        location = (
            os.getenv("ANTHROPIC_VERTEX_LOCATION")
            or config.get("ANTHROPIC_VERTEX_LOCATION")
            or "global"
        )
        gcp_credentials = (
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            or config.get("GOOGLE_APPLICATION_CREDENTIALS")
        )
 
        if gcp_credentials:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = gcp_credentials
        os.environ["GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES"] = "1"
 
        self.client = AnthropicVertex(region=location, project_id=project_id)

        # Timeout config (used when caller passes default)
        self.model_timeout = config.get("MODEL_TIMEOUT", 300)
        if not isinstance(self.model_timeout, int):
            self.model_timeout = 300

        logging.info(f"Created Claude model wrapper. Using model: {self.model}")

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

    def call_model(self, system_prompt: str, prompt: str, timeout: int) -> List[str]:
        """Provider-specific API call. Returns a list of candidate strings."""

        if self.debug:
            print(f"Requesting prompt using the model: {self.model}")
            print(f"System prompt: {system_prompt}")
            print(f"User prompt: {prompt}")
            print(f"Request parameters: model={self.model}, timeout={timeout}")
        
        kwargs = dict(
            model=self.model,
            max_tokens=int(os.getenv("CLAUDE_MAX_TOKENS") or config.get("CLAUDE_MAX_TOKENS", 4096)),
            temperature=self.temperature,
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )
        if system_prompt:
            kwargs["system"] = system_prompt

        #resp = self.client.messages.create(**kwargs)
        #text = (resp.content[0].text or "").strip()
        #return [text]
        with self.client.messages.stream(**kwargs) as stream:
            resp = stream.get_final_message()

        texts = []
        for block in resp.content:
            if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                texts.append(block.text.strip())

        text = "\n".join(t for t in texts if t).strip()

        if not text:
            raise ValueError("Empty response content from model")

        return [text]
    
    def _extract_first_json_object(self, text: str):
        text = text.strip()

        try:
            return json.loads(text)
        except Exception:
            pass

        fenced_json = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced_json:
            try:
                return json.loads(fenced_json.group(1))
            except Exception:
                pass

        fenced_any = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced_any:
            try:
                return json.loads(fenced_any.group(1))
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

    def _normalize_structured_response(
        self,
        content: str,
        schema: Optional[str],
        expected_single_file: bool,
    ) -> str:
        # Non toccare il direct-text single-file mode
        if expected_single_file:
            return content

        # Se non c'è schema, in generale non forzare JSON
        if schema is None:
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
        """Return (output, success) as expected by dataset_processor."""
        system_prompt = self._create_system_prompt(schema, category)

        # Use configured timeout if default
        if timeout == 60:
            timeout = self.model_timeout  # o config.get("MODEL_TIMEOUT", 60) se preferisci

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
                        logging.warning(f"Failed to write raw Claude response to {raw_log}: {raw_log_error}")

                content = output_cleanup(content, files, rtl_only=(expected_single_file and schema is None))
                content = self._normalize_structured_response(
                    content=content,
                    schema=schema,
                    expected_single_file=expected_single_file,
                )

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