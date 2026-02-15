import argparse
import csv
import json
from pathlib import Path
from statistics import fmean

from sim_lab.config import StrategyParams, WorldConfig
from sim_lab.simulator import DeterministicSimulator


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--seed-from', type=int, required=True)
    p.add_argument('--seed-to', type=int, required=True)
    p.add_argument('--out-dir', required=True)
    args = p.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / 'per_seed_metrics.csv'
    chunk_path = out / 'chunks.jsonl'
    write_header = not csv_path.exists()

    sim = DeterministicSimulator(world=WorldConfig(n_steps=10_000), strategy_params=StrategyParams())
    rows = []
    for seed in range(args.seed_from, args.seed_to + 1):
        run = sim.run(seed=seed, n_steps=10_000, capture_steps=False, capture_events=False, run_id=f'full-{seed}')
        rows.append({
            'seed': seed,
            'edge_submission': run.total_edge_submission,
            'edge_normalizer': run.total_edge_normalizer,
            'avg_fee_bps_submission': run.avg_fee_bps_submission,
            'avg_bid_fee_bps_submission': run.avg_bid_fee_bps_submission,
            'avg_ask_fee_bps_submission': run.avg_ask_fee_bps_submission,
            'arb_volume_y_submission': run.arb_volume_y_submission,
            'retail_volume_y_submission': run.retail_volume_y_submission,
            'arb_trade_count_submission': run.arb_trade_count_submission,
            'retail_trade_count_submission': run.retail_trade_count_submission,
        })

    fields = [
        'seed',
        'edge_submission',
        'edge_normalizer',
        'avg_fee_bps_submission',
        'avg_bid_fee_bps_submission',
        'avg_ask_fee_bps_submission',
        'arb_volume_y_submission',
        'retail_volume_y_submission',
        'arb_trade_count_submission',
        'retail_trade_count_submission',
    ]

    with csv_path.open('a', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    chunk_summary = {
        'seed_from': args.seed_from,
        'seed_to': args.seed_to,
        'n_sims': len(rows),
        'mean_edge_submission': fmean(r['edge_submission'] for r in rows),
        'mean_avg_fee_bps_submission': fmean(r['avg_fee_bps_submission'] for r in rows),
    }
    with chunk_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(chunk_summary) + '\n')

    print(json.dumps(chunk_summary, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
