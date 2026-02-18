## Cursor research workspace

This folder is the **main workspace** for the optimization / research phase.

### Layout
- `Cursor/runs/`: each optimization run lives in its own subfolder (self-contained, restartable).
- `Cursor/strategies/`: strategy snapshots (champions, promoted candidates, etc.).
- `Cursor/research_journal.md`: running notes + conclusions across runs.
- `Cursor/tools/`: pinned copies of key helper scripts used for this phase.

### Conventions
- Keep any launch/restart instructions inside each run’s `.ai.md`.
- Prefer `py -3.10 ...` for all python commands (project uses `amm_sim_rs`).
- Run folder names should begin with a UTC timestamp (to-the-second) followed by a short strategy/description, e.g.:
  - `20260218T114601Z_theo1_ms_guard_w7_q6r16_s0275_mc4_seed42000-42011_h43000-43005_smoke`

