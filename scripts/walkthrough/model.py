# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The model that writes and checks walkthrough text, called through litellm.

Every call asks for one JSON object. The default is the Wiki's own model,
DeepSeek V4 Flash; the key is read from ``DEEPSEEK_API_KEY`` when a call is
made, so building paths alone needs no key.
"""

from __future__ import annotations

import json
import os
import time

import litellm

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_API_BASE = "https://api.deepseek.com"
KEY_ENV = "DEEPSEEK_API_KEY"


class Model:
    """One chat model, asked for JSON.

    ``think`` turns on reasoning for planning calls only (choosing a goal, a
    hop or a path), with 6000 more output tokens for it.
    """

    def __init__(
        self,
        name: str = DEFAULT_MODEL,
        api_base: str = DEFAULT_API_BASE,
        think: bool = False,
        key_env: str = KEY_ENV,
    ) -> None:
        self.name = name
        self.api_base = api_base
        self.think = think
        self.key_env = key_env

    def _complete(self, system: str, user: str, **options) -> object:
        return litellm.completion(
            model=self.name,
            api_base=self.api_base,
            api_key=os.environ[self.key_env],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            **options,
        )

    def ask(
        self,
        system: str,
        user: str,
        max_tokens: int = 1400,
        temperature: float = 0.2,
        plan: bool = False,
        calls: list | None = None,
    ) -> dict:
        """Write text; ``plan=True`` marks a planning decision.

        Token counts and seconds are appended to ``calls`` when it is given.
        """

        thinking = plan and self.think
        if thinking:
            max_tokens = max_tokens + 6000
        t0 = time.time()
        r = self._complete(
            system,
            user,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
            timeout=300,
        )
        if calls is not None:
            u = r.usage
            calls.append(
                {
                    "in": u.prompt_tokens,
                    "out": u.completion_tokens,
                    "s": round(time.time() - t0, 1),
                }
            )
        return json.loads(r.choices[0].message.content)

    def check(self, system: str, user: str) -> dict:
        """Check text against source: no sampling, a shorter answer."""

        r = self._complete(
            system,
            user,
            temperature=0,
            max_tokens=900,
            extra_body={"thinking": {"type": "disabled"}},
            timeout=180,
        )
        return json.loads(r.choices[0].message.content)
