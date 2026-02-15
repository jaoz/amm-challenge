#!/usr/bin/env bash
set -euo pipefail

cd /app

cat > Strat/fixed_asym_template.sol <<'SOL'
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";
contract Strategy is AMMStrategyBase {
    uint256 constant BID_BPS = __BID__;
    uint256 constant ASK_BPS = __ASK__;
    function afterInitialize(uint256, uint256) external pure override returns (uint256, uint256) {
        return (bpsToWad(BID_BPS), bpsToWad(ASK_BPS));
    }
    function afterSwap(TradeInfo calldata) external pure override returns (uint256, uint256) {
        return (bpsToWad(BID_BPS), bpsToWad(ASK_BPS));
    }
    function getName() external pure override returns (string memory) { return "FixedAsymTmp"; }
}
SOL

for bid in 20 30 40 50 60 70 80 100 120; do
  for ask in 20 30 40 50 60 70 80 100 120; do
    sed -e "s/__BID__/$bid/g" -e "s/__ASK__/$ask/g" Strat/fixed_asym_template.sol > Strat/fixed_asym_${bid}_${ask}.sol
    out=$(/opt/venv/bin/amm-match run Strat/fixed_asym_${bid}_${ask}.sol --simulations 80 | tail -n 1)
    echo "bid=${bid} ask=${ask} => ${out}"
  done
done
