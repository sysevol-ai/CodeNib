# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from ..agent.rerank_agent import RerankAgent
from ..index.embedding.vector_store import CodeVectorStore
from ..llm.litellm_chat import LiteLLMChat
from ..log_utils import get_logger

logger = get_logger(__name__)


@dataclass
class RerankContext:
    """Shared context carrying the rerank agent and configuration."""

    llm: Optional[LiteLLMChat] = None
    agent: Optional[RerankAgent] = None
    embedding_store: Optional[CodeVectorStore] = None
    top_k: Optional[int] = None
    candidate_top_k: Optional[int] = None
    window_size: Optional[int] = None
    window_step: Optional[int] = None
    listwise_format: Literal["structured", "rankgpt"] = "structured"

    def ensure_agent(self) -> RerankAgent:
        if self.agent is None:
            if self.llm is None:
                raise RuntimeError("Rerank agent requested but no LLM was provided.")
            logger.info(
                "Creating rerank agent.",
                extra={
                    "model": self.llm.model,
                    "listwise_format": self.listwise_format,
                },
            )
            self.agent = RerankAgent(llm=self.llm, listwise_format=self.listwise_format)
        return self.agent
