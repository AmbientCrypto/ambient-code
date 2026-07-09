# Per-model tuning

Tuning in this CLI is **derived, not hand-coded**: `model_profile()` reads each
model's live catalog metadata (`context_length`, `max_output_length`, the
`reasoning` feature flag, structured-output capabilities) and derives its input
sizing, chunking, output budget, escalation ceiling, and JSON mode. Any model the
API adds is tuned automatically the moment it appears in the catalog — nothing to
update client-side.

Run `ambient models` to see what the catalog offers right now, and
`ambient models --all` to include curated-out entries. The derivation is covered
by catalog-driven invariants plus fuzz configs in CI, so a new model can never
land with an unsafe budget or chunk size.

## What the derivation does

- A hard output cap shrinks a model's single-shot pass to what reasoning + answer
  can actually fit, instead of marathoning toward an empty reply.
- A non-reasoning model spends no tokens thinking, so it gets a larger single-shot
  sized off its real context window.
- A model without structured-output support routes `--json` audits through the
  prompt-instruction + tolerant-parse path instead of a failing `response_format`.

## Selectability

Every catalog model is choosable: `ambient use <id>` (sticky default), `-m <id>`
(one call), or the interactive `ambient use` picker. An explicit `-m` always
works, even for a model the menus curate out.

## Defaults

The default model for every lane is `moonshotai/kimi-k2.7-code`. Any other model
stays fully selectable with `ambient use <id>` / `-m <id>`.
