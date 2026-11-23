from src.llm_lib.model_factory import ModelFactory
from src.llm_models.gemini_instance import GeminiModel
from src.llm_models.claude_instance import ClaudeModel
from src.llm_models.openrouter_instance import OpenRouterModel

# Import the subjective scoring model implementation
from src.llm_models.sbj_score_model import SubjectiveScoreModel_Instance
from typing import Any, Optional

from src.config_manager import config
import logging

class CustomModelFactory(ModelFactory):
    def __init__(self):
        # Initialize the base factory first
        super().__init__()

        # Register Gemini models using model_types dict (CVDP style)
        self.model_types["gemini"] = self._create_gemini_instance
        self.model_types["gemini-2.5-flash"] = self._create_gemini_instance
        self.model_types["gemini-2.5-pro"] = self._create_gemini_instance
        self.model_types["claude-sonnet-4-6"] = self._create_claude_instance
        self.model_types["claude-3.7-sonnet"] = self._create_openrouter_instance

         # Register subjective scoring model
        self.model_types["sbj_score"] = self._create_sbj_score_instance
        
        logging.info("Custom model factory initialized with additional model support")

    def _create_gemini_instance(self, model_name, context, key=None, **kwargs):
        """Create a Gemini model instance"""
        # `model_name` is the string passed via -m / DEFAULT_MODEL (e.g., gemini-2.5-flash)
        return GeminiModel(context=context, key=key, model=model_name, **kwargs)
    
    def _create_claude_instance(self, model_name, context, key=None, **kwargs):
        """Create a Claude model instance"""
        return ClaudeModel(context=context, key=key, model=model_name, **kwargs)

    def _create_openrouter_instance(self, model_name, context, key=None, **kwargs):
        """Create an OpenRouter model instance"""
        return OpenRouterModel(context=context, key=key, model=model_name, **kwargs)
    
        # Implementation for subjective scoring model
    def _create_sbj_score_instance(self, model_name: str, context: Any, key: Optional[str] = None, **kwargs) -> Any:
        scoring_model = (
            kwargs.pop("scoring_model", None)
            or config.get("SUBJECTIVE_SCORING_MODEL")
            or "claude-sonnet-4-6"
        )
        return SubjectiveScoreModel_Instance(
            context=context,
            key=key,
            model=scoring_model,
            **kwargs,
        )
    