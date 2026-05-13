# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..agent.extract_agent import KeywordExtractor
from ..llm.litellm_chat import LiteLLMChat
from ..log_utils import get_logger

logger = get_logger(__name__)


@dataclass
class TransformContext:
    """Container for shared transform resources."""

    llm: Optional[LiteLLMChat] = None
    keyword_extractor: Optional[KeywordExtractor] = None
    max_snippets: int = 5
    max_chars: int = 1800

    def ensure_keyword_extractor(self) -> KeywordExtractor:
        if self.keyword_extractor is None:
            if self.llm is None:
                raise RuntimeError(
                    "Keyword extractor requested but no LLM was provided."
                )
            logger.debug("Creating keyword extractor for transform kernel.")
            self.keyword_extractor = KeywordExtractor(llm=self.llm)
        return self.keyword_extractor
