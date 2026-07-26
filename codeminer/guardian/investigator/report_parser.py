# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Machine-readable test report parsing for Guardian probes."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from .types import TestStatus


@dataclass(frozen=True)
class ParsedTestReport:
    """The parser result, including whether a report existed and was valid."""

    parseable: bool
    tests: Dict[str, TestStatus] = field(default_factory=dict)
    error: str = ""


def parse_pytest_junit(path: str | Path) -> ParsedTestReport:
    """Parse pytest JUnit XML without consulting the pytest exit code."""

    report_path = Path(path)
    try:
        root = ET.parse(report_path).getroot()
    except (OSError, ET.ParseError) as exc:
        return ParsedTestReport(False, error=str(exc))

    if root.tag not in {"testsuite", "testsuites"}:
        return ParsedTestReport(False, error=f"unexpected root element {root.tag!r}")

    tests: Dict[str, TestStatus] = {}
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        file_name = case.get("file", "")
        prefix = file_name or classname
        node_id = f"{prefix}::{name}" if prefix else name
        if not node_id:
            continue
        if case.find("failure") is not None:
            status = TestStatus.FAILED
        elif case.find("error") is not None:
            status = TestStatus.ERROR
        elif case.find("skipped") is not None:
            status = TestStatus.SKIPPED
        else:
            status = TestStatus.PASSED
        tests[node_id] = status
    return ParsedTestReport(True, tests=tests)
