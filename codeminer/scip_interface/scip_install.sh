# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -eu

cd "$(dirname "$0")/../.."
exec make scip-python-tool
