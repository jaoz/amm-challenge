# Attribution and Derivation Note

The strategy in `baseline_sourced.sol` was derived from the public repository:

https://github.com/jiayaoqijia/amm-challenge-yq

This implementation was used as an initial structural baseline to accelerate experimentation and benchmarking.

The final `Strategy.sol` builds on that baseline and introduces the following extensions:

- A dynamic arbitrage shield that computes boundary-implied required fees and overrides standard tilt constraints when exposure exceeds tolerance.
- Enhanced trade classification using timestamp ordering and tail-based size signals.
- Improved handling of censored observations via timestamp-gap λ inference.
- Modified fee adaptation logic emphasizing mean-edge optimization.
- Tuning with independent seed validation.

The overall control loop structure remains related to the sourced baseline; however, the arbitrage-protection layer and tail-capture logic materially alter the strategy’s behavior and performance characteristics.