# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "start_web.sh"


def _run_start_web(
    tmp_path: Path,
    *,
    config_model: str,
    config_override: str | None = None,
    model_override: str | None = None,
    local_overlay: bool = False,
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "qa_config.yaml").write_text("model: gpt-4o\n", encoding="utf-8")
    if local_overlay:
        (tmp_path / "qa_config.local.yaml").write_text(
            "extends: qa_config.yaml\nmodel: local-profile\n",
            encoding="utf-8",
        )
    if config_override:
        (tmp_path / config_override).write_text(
            f"model: {config_model}\n",
            encoding="utf-8",
        )
    registry = tmp_path / "qa_registry.json"
    registry.write_text("{}\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    conda = fake_bin / "conda"
    conda.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"import codenib"*) exit 0 ;;
  *"from codenib.web.config import load_config"*)
    printf '%s\\n%s\\n\\n0\\n' "$FAKE_REGISTRY" "$FAKE_CONFIG_MODEL"
    exit 0
    ;;
  *"python -m codenib.web.app"*)
    printf 'LAUNCHED_MODEL=%s\\n' "${CODENIB_DEMO_MODEL:-}"
    exit 0
    ;;
esac
exit 99
""",
        encoding="utf-8",
    )
    conda.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    curl.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_REGISTRY": str(registry),
            "FAKE_CONFIG_MODEL": config_model,
            "LLM_HOST": "127.0.0.1",
            "CODENIB_DEMO_PROFILE": "local",
        }
    )
    env.pop("CODENIB_DEMO_CONFIG", None)
    env.pop("CODENIB_DEMO_MODEL", None)
    if config_override:
        env["CODENIB_DEMO_CONFIG"] = config_override
    if model_override:
        env["CODENIB_DEMO_MODEL"] = model_override

    return subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"config_model": "gpt-4o"}, "openai/qwen2.5-coder"),
        (
            {"config_model": "local-profile", "local_overlay": True},
            "local-profile",
        ),
        (
            {"config_model": "custom-profile", "config_override": "custom.yaml"},
            "custom-profile",
        ),
        (
            {"config_model": "gpt-4o", "model_override": "explicit-model"},
            "explicit-model",
        ),
    ],
)
def test_start_web_selects_the_documented_local_model(tmp_path, kwargs, expected):
    result = _run_start_web(tmp_path, **kwargs)

    assert result.returncode == 0, result.stderr
    assert f"Using local model profile: {expected}" in result.stdout
    assert f"LAUNCHED_MODEL={expected}" in result.stdout
