If you are doing changes, commit the current local git state before new edits.
There are multiple agents/humans working in parallel, so do not drop files or edits without commits.
Use [.aiignore](../.aiignore).

Use Docker + `amm-match` for validation/runs.
Preferred image digest: `ea4a105ed63f318889f8766cd1991469ca48f7149af98132b4209d6b9302f559`.
If that digest is unavailable locally, use `amm-challenge:latest`.

There are no unit/integration tests here; validation is done with `amm-match`.

How to run (from repo root, PowerShell):
1. Validate a strategy:
   `docker run --rm -v "${PWD}:/app" -w /app amm-challenge:latest amm-match validate Strat/Strategy_v2.sol`
2. Run a quick smoke benchmark:
   `docker run --rm -v "${PWD}:/app" -w /app amm-challenge:latest amm-match run Strat/Strategy_v2.sol --simulations 50`
3. Run a fuller benchmark:
   `docker run --rm -v "${PWD}:/app" -w /app amm-challenge:latest amm-match run Strat/Strategy_v2.sol --simulations 1000`
