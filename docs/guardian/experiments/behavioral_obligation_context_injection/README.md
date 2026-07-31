# Behavioral-obligation context-injection experiment

This experiment injects one task-specific behavioral-obligation model into each
of the five original Python DeepSWE tasks.

The prompts are oracle-informed. They were constructed after inspecting
reference solutions, held-out tests, and the 120-trial obligation-attribution
study. Results are diagnostic evidence about the intervention mechanism, not
benchmark-valid scores.

## Matrix

- tasks: five original Python tasks;
- models: `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`;
- trials: four per task/model setting;
- reasoning effort: medium;
- total: 60 trials;
- global concurrency: 3;
- baseline: solo agent plus the matching task-specific prompt;
- output root:
  `data/deepswe_outputs_context_injection/behavioral_obligations_python5`.

The matrix uses `--task-context-dir`, which maps each task to
`prompts/<task>.md` without exposing the other four task models.

The completed matrix used the files in `prompts/`. Keep those files unchanged
so their recorded SHA-256 hashes remain reproducible. `prompts_v2/` contains
revised, more concrete obligations derived from the post-run investigation;
they have not yet been evaluated.

See `prompt_investigation.md` for the trial-level diagnosis and the rationale
for each revision.

See `v2_comparison_report.md` for the reduced 30-trial v2 comparison against
the original no-context baseline.
