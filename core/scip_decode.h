/*
 * SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

// Umbrella header for the core SCIP decoders. Individual language decoders
// live in `scip_decode_{python,go,rust,ruby,ts}.h`.

#include "scip_decode_base.h"
#include "scip_decode_common.h"
#include "scip_decode_go.h"
#include "scip_decode_python.h"
#include "scip_decode_ruby.h"
#include "scip_decode_rust.h"
#include "scip_decode_ts.h"
#include "scip_decoder_registry.h"

namespace codeminer::core {
// Backward-compat alias (old tests may still reference this name).
using SCIPGraphDecoder = SCIPPythonDecoder;
} // namespace codeminer::core
