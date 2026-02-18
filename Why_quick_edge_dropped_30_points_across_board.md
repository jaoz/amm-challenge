# Why quick edge dropped 30 points across the board

## What is happening
- `quick` evaluations are noisy and use small sample size (`quick_sims=50`).
- In the optimizer, each evaluation uses `build_base_config(seed=None)`, so scenario draws change between runs and between candidates.
- This means absolute quick-edge levels can shift materially (sometimes tens of points) even when strategy quality is similar.

## Why this can look like seed mismatch
- A base strategy can be selected under one random scenario mix.
- A later run can evaluate all new candidates under a different random scenario mix.
- If the new mix is harder, every quick score can drop "across the board" without an actual strategy regression.

## Evidence from current pipeline
- Pre-normalized vs normalized strategy comparison showed zero drift when evaluated on fixed seeds and identical settings.
- So the large quick-edge shift is not from numeric-format normalization (`WAD / d` or `* BPS` to integer), but from stochastic evaluation mix.

## Practical fix
- Use fixed seed baskets for quick scoring and keep them constant within a run.
- Keep a separate holdout seed basket for refine/selection.
- Compare candidates and incumbent on the same seed set (common-random-numbers approach).
- Increase `quick_sims` when large ranking instability is observed.
