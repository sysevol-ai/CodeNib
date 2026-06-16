# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

.PHONY: install scip dev test web-start web-stop web-restart web-reclaim web-status web-logs web-follow

install:
	pip install -e .

scip:
	pip install -e ".[scip]"
	./setup-scip.sh

dev:
	pip install -e ".[dev,test]"

test:
	pytest

web-start:
	./scripts/dev_web.sh start

web-stop:
	./scripts/dev_web.sh stop

web-restart:
	./scripts/dev_web.sh restart

web-reclaim:
	CODEMINER_WEB_RECLAIM_PORTS=1 ./scripts/dev_web.sh restart

web-status:
	./scripts/dev_web.sh status

web-logs:
	./scripts/dev_web.sh logs

web-follow:
	./scripts/dev_web.sh follow
