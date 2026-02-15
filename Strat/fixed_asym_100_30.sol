// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";
contract Strategy is AMMStrategyBase {
    uint256 constant BID_BPS = 100;
    uint256 constant ASK_BPS = 30;
    function afterInitialize(uint256, uint256) external pure override returns (uint256, uint256) {
        return (bpsToWad(BID_BPS), bpsToWad(ASK_BPS));
    }
    function afterSwap(TradeInfo calldata) external pure override returns (uint256, uint256) {
        return (bpsToWad(BID_BPS), bpsToWad(ASK_BPS));
    }
    function getName() external pure override returns (string memory) { return "FixedAsymTmp"; }
}
