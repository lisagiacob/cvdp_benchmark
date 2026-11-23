import os
import sys
import logging
import json
import re
from typing import Optional, Any

from anthropic import AnthropicVertex
from src.config_manager import config

logging.basicConfig(level=logging.INFO)


class SubjectiveScoreModel_Instance:
    """
    Subjective scoring model implemented with Claude on Vertex AI.
    """
    
    # `key` is unused for Vertex-based auth, kept for factory compatibility.
    def __init__(self, context: Any = None, key: Optional[str] = None, model: str = None):
        if model is None:
            model = config.get("SUBJECTIVE_SCORING_MODEL", "claude-sonnet-4-6")

        self.model = model
        self.debug = False
        self.context = context

        self.model_timeout = config.get("MODEL_TIMEOUT", 60)
        if not isinstance(self.model_timeout, int):
            self.model_timeout = 60

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
        logging.info(f"Created Subjective Scoring Model using Claude on Vertex. Using model: {self.model}")

    def set_debug(self, debug: bool = True) -> None:
        self.debug = debug
        logging.info(f"Debug mode {'enabled' if debug else 'disabled'}")

    def _extract_json(self, text: str) -> Optional[dict]:
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

        bare_json = re.search(r"(\{.*\})", text, re.DOTALL)
        if bare_json:
            try:
                return json.loads(bare_json.group(1))
            except Exception:
                pass

        return None

    def subjective_score(self, response: str, reference: str, problem_prompt: str = "") -> float:
        if not hasattr(self, "client"):
            raise ValueError("Claude Vertex client not initialized")

        system_prompt = """You are an expert evaluator of candidate responses against reference solutions.

Your task is to score how well a candidate response matches the reference solution on a scale from 0.0 to 1.0:
- 0.0 = no meaningful match
- 1.0 = perfect match

Evaluate ONLY with respect to the original problem prompt.

Consider:
1. Relevance
2. Semantic similarity
3. Completeness
4. Correctness
5. Style/format consistency when relevant

Return ONLY a valid JSON object in this exact format:
{
  "score": 0.85,
  "reasoning": "short explanation"
}
Do not include any extra text before or after the JSON.
"""

        user_prompt = f"""Evaluate the candidate response against the reference solution.

Original Problem:
{problem_prompt}

Reference Solution:
{reference}

Candidate Response:
{response}
"""

        if self.debug:
            logging.info(f"Subjective scoring request with model={self.model}")

        try:
            max_retries = 5

            for retry_count in range(max_retries):
                if retry_count > 0 and self.debug:
                    logging.info(f"Retry attempt {retry_count}")

                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=int(
                        os.getenv("CLAUDE_MAX_TOKENS")
                        or config.get("CLAUDE_MAX_TOKENS", 1024)
                    ),
                    temperature=0.0,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                    timeout=self.model_timeout,
                )

                texts = []
                for block in resp.content:
                    if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                        texts.append(block.text.strip())

                result_text = "\n".join(t for t in texts if t).strip()
                sys.stdout.flush()

                if self.debug:
                    logging.info(f"Scoring raw response: {result_text}")

                result = self._extract_json(result_text)
                if result is None:
                    continue

                if "score" in result and isinstance(result["score"], (int, float)) and "reasoning" in result:
                    score = float(result["score"])
                    score = max(0.0, min(1.0, score))
                    return score

            if self.debug:
                logging.error(f"Failed to get valid JSON after {max_retries} attempts")
            return 0.0

        except Exception as e:
            if self.debug:
                logging.error(f"Failed to calculate subjective score: {str(e)}")
            return 0.0


if __name__ == "__main__":
    scorer = SubjectiveScoreModel_Instance()
    scorer.set_debug(True)

    try:
        problem = "Write a function to calculate the factorial of a number."
        reference = """
def factorial(n):
    if n == 0 or n == 1:
        return 1
    else:
        return n * factorial(n-1)
"""
        response = """
def factorial(n):
    result = 1
    for i in range(1, n+1):
        result *= i
    return result
"""

        score = scorer.subjective_score(response, reference, problem)
        print(f"Subjective score: {score}/1.0")
    except Exception as e:
        print(f"Error: {e}")