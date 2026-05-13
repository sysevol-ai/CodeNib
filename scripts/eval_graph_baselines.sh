#!/bin/bash

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -e

echo "=== Graph Baseline ==="
python examples/graph_retrieve_baseline.py     --dataset swebench_lite     --split dev     --stage1-topk 20     --stage2-topk 200     --k-hop 3 --ppr --ppr-damping 0.85   --embedding     --result-path results/graph_dev_improved.json

echo "=== Running Embedding Baseline ==="
python examples/embedding_retrieve_baseline.py \
    --dataset swebench_lite \
    --split dev \
    --result-path results/embedding_dev.json

echo "=== Running BM25 Baseline ==="
python examples/bm25_retrieve_baseline.py \
    --dataset swebench_lite \
    --split dev \
    --result-path results/bm25_dev.json

echo "=== Plotting Comparison ==="
python -c "
import json
import matplotlib.pyplot as plt
from codeminer.eval.retrieval_eval import aggregate_metrics, average_metrics

COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c']
MARKERS = ['o', 's', '^']

def load(path):
    with open(path) as f:
        results = json.load(f)
    label = results[0].get('method', path) if results else path
    agg, count = {}, 0
    for e in results:
        if 'metrics' in e:
            aggregate_metrics(agg, e['metrics'])
            count += 1
    return label, average_metrics(agg, count), count

baselines = [
    load('results/bm25_dev.json'),
    load('results/graph_dev_improved.json'),
    load('results/embedding_dev.json'),
]

for label, _, count in baselines:
    print(f'{label}: {count} instances')

scopes = ['files', 'symbols']
metrics = ['accuracy', 'recall', 'precision']
fig, axes = plt.subplots(nrows=2, ncols=3, figsize=(14, 8), sharex=False)
fig.suptitle('Retrieval Baseline Comparison: ' + ' vs '.join(b[0] for b in baselines), fontsize=12)

for row, scope in enumerate(scopes):
    all_ks = set()
    for _, avg, _ in baselines:
        all_ks.update(int(k) for k in avg.get(scope, {}))
    ks = sorted(all_ks)
    for col, metric in enumerate(metrics):
        ax = axes[row][col]
        for i, (label, avg, _) in enumerate(baselines):
            scope_data = avg.get(scope, {})
            vals = [(scope_data.get(k) or scope_data.get(str(k)) or {}).get(metric, 0.0) for k in ks]
            ax.plot(ks, vals, color=COLORS[i], marker=MARKERS[i], label=label, linewidth=1.8)
        ax.set_title(f'{scope.capitalize()} — {metric.capitalize()}@k', fontsize=10)
        ax.set_xlabel('k')
        ax.set_ylabel(metric.capitalize())
        ax.set_ylim(0, 1.05)
        ax.set_xticks(ks)
        ax.legend(fontsize=8)
        ax.grid(True, linestyle='--', alpha=0.5)

plt.tight_layout()
plt.savefig('results/comparison.png', dpi=150, bbox_inches='tight')
print('Plot saved to results/comparison.png')
"

echo "=== Done ==="
