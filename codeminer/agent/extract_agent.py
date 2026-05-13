# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Keyword extraction agent for problem statements.
This module extracts key terms from problem statements using llama_index.
"""

from typing import List

from pydantic import BaseModel, Field

from ..llm.litellm_chat import LiteLLMChat, human_message
from ..log_utils import get_logger

logger = get_logger(__name__)


# Define Pydantic model for structured output
class KeywordExtraction(BaseModel):
    """Model for keyword extraction output."""

    keywords: List[str] = Field(description="List of extracted keywords")


class KeywordExtractor:
    """Agent for extracting keywords from problem statements."""

    def __init__(
        self,
        llm: LiteLLMChat,
    ):
        """Initialize the keyword extractor.

        Args:
            llm: A configured LiteLLMChat instance.
        """
        self.llm = llm
        self.structured_llm = self.llm.with_structured_output(KeywordExtraction)

    def extract_keywords(self, problem_statement: str) -> KeywordExtraction:
        """
        Extract keywords from a problem statement.

        Args:
            problem_statement (str): The problem statement to extract keywords from

        Returns:
            KeywordExtraction: Structured output with extracted keywords
        """
        # Create prompt with detailed instructions
        prompt = (
            "You are a keyword extraction specialist. "
            "Your task is to extract important keywords "
            "from problem statements. Focus on identifying "
            "technical terms, function names, class names, "
            "modules, file paths, and concepts that would be "
            "useful for searching in a codebase. "
            "\n\n"
            "Guidelines for extraction:\n"
            "1. Extract file paths and file names "
            "(e.g., 'django/db/models/expressions.py'"
            "-> and 'expressions.py')\n"
            "2. Extract function and method names "
            "(e.g., 'separability_matrix', 'run_validators')\n"
            "3. Extract class names and module names\n"
            "4. Prefer precise terms over general ones\n"
            "5. Remove common stopwords and general "
            "programming terms\n"
            "\n\n"
            "Please extract the key technical terms and "
            "concepts from the following problem statement:"
            f"\n\n{problem_statement}\n\n"
            "Return only the essential terms that would be "
            "most useful for searching in a codebase."
        )

        # Use structured LLM to get output directly as a KeywordExtraction object
        input_msg = human_message(prompt)
        result = self.structured_llm.invoke([input_msg])
        logger.debug(f"Extracted keywords: {result}")
        return result


def extract_keywords_from_statement(
    problem_statement: str,
    llm: LiteLLMChat,
) -> KeywordExtraction:
    """
    Extract keywords from a problem statement.

    Args:
        problem_statement: The problem statement to extract keywords from.
        llm: A configured LiteLLMChat instance.

    Returns:
        KeywordExtraction: Structured output with extracted keywords
    """
    extractor = KeywordExtractor(llm=llm)
    return extractor.extract_keywords(problem_statement)
