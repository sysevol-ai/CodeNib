# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.web.ports import validate_local_wiki_ports, validate_tcp_port


@pytest.mark.parametrize("port", [1, 80, 8_000, 65_535, "3000"])
def test_validate_tcp_port_accepts_socket_range(port) -> None:
    assert validate_tcp_port(port) == int(port)


@pytest.mark.parametrize("port", [0, -1, 65_536, True, 1.5, "not-a-port", None])
def test_validate_tcp_port_rejects_unusable_values(port) -> None:
    with pytest.raises(ValueError, match="between 1 and 65535"):
        validate_tcp_port(port)


def test_validate_local_wiki_ports_rejects_collision() -> None:
    with pytest.raises(ValueError, match="must be different"):
        validate_local_wiki_ports(frontend_port=3_000, api_port=3_000)
