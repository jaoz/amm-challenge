#!/usr/bin/env bash
set -euo pipefail

cd /app

cat > Strat/fixed_fee_template.sol <<'SOL'
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";
contract Strategy is AMMStrategyBase {
    uint256 constant FEE_BPS = __FEE__;
    function afterInitialize(uint256, uint256) external pure override returns (uint256, uint256) {
        uint256 f = bpsToWad(FEE_BPS);
        return (f, f);
    }
    function afterSwap(TradeInfo calldata) external pure override returns (uint256, uint256) {
        uint256 f = bpsToWad(FEE_BPS);
        return (f, f);
    }
    function getName() external pure override returns (string memory) { return "FixedTmp"; }
}
SOL

for f in 5 10 15 20 25 30 35 40 50 60 70 80 100 120 150 200 300; do
  sed "s/__FEE__/$f/g" Strat/fixed_fee_template.sol > Strat/fixed_${f}.sol
  out=$(/opt/venv/bin/amm-match run Strat/fixed_${f}.sol --simulations 120 | tail -n 1)
  echo "${f}bps => ${out}"
done
