// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";
contract Strategy is AMMStrategyBase {
    uint256 constant FEE_BPS = 20;
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
