# Theoretical Optimal Control Note  
**AMM Challenge — Mean Edge Maximization**

## Objective

Per the strategy specification :contentReference[oaicite:0]{index=0}, the objective is:

\[
\max_{\pi} \; \mathbb{E}\left[\sum_{t=1}^{T} \text{Edge}_t \right]
\]

- \(T = 10{,}000\) steps per simulation  
- Evaluated over 1000+ simulations  
- Variance and tail metrics are irrelevant  

Only **expected cumulative edge** matters.

---

## Edge Structure

Per step:

\[
\text{Edge}_t
=
\underbrace{\lambda \cdot s(f_t) \cdot \mathbb{E}[Q] \cdot f_t}_{\text{Retail Capture}}
-
\underbrace{\text{Arb Loss}_t}_{\text{Toxic Flow}}
\]

Where:

- \( \lambda \in [0.6,1.0] \) — dominant cross-run driver  
- \( s(f_t) \) — routing share vs fixed 30bps competitor  
- \( f_t \) — chosen fee  
- \( Q \) — retail trade size  

Arbitrage enforces the no-arb bounds:

\[
p_t
\in
\left[
\text{spot} \cdot (1 - f^{bid}),
\;
\frac{\text{spot}}{1 - f^{ask}}
\right]
\]

Mispricing combined with inventory exposure generates arbitrage loss.

---

## Partial Observability

True state:

\[
S_t = (p_t, \lambda, \sigma)
\]

Observed:

- Only trades that hit our AMM  
- Zero-trade steps are invisible  
- First trade per timestamp may be arbitrage  

This forms a **Partially Observed Markov Decision Process (POMDP)**.

Belief state approximation:

\[
B_t =
(
\lambda_{\hat{}},
\text{arb}_{\hat{}},
p_{low},
p_{high},
p_{step},
\text{inventory}
)
\]

---

## Dynamic Control Problem

Control policy:

\[
\pi : B_t \rightarrow (f^{bid}_t, f^{ask}_t)
\]

Bellman equation:

\[
V(B_t)
=
\max_{f_t}
\mathbb{E}
\left[
g(B_t,f_t) + V(B_{t+1})
\right]
\]

where:

\[
g(B_t,f_t)
=
\mathbb{E}[\text{Retail Capture}]
-
\mathbb{E}[\text{Arb Loss}]
\]

Exact dynamic programming is infeasible; a one-step lookahead approximation is used.

---

## First-Principles Fee Tradeoff

Ignoring inventory for intuition:

\[
\max_f
\;
\lambda \cdot s(f - f_c) \cdot f
-
\kappa \cdot \text{staleness}(B_t,f)
\]

First-order condition:

\[
\lambda (s + f s')
=
\kappa \frac{\partial \text{staleness}}{\partial f}
\]

Implications:

- Higher \( \lambda \) → higher optimal base fee  
- Higher toxicity → lower fee  
- Volatility has minor effect (narrow σ range)  
- Inventory tilt only dominates under large imbalance  

This aligns with the design priorities in :contentReference[oaicite:1]{index=1}.

---

## Large-Imbalance Override

After a retail skew:

\[
f^{required}_{side}
=
\max\left(0, 1 - \frac{p_{step}}{\text{pool price}}\right)
\]

If:

\[
f^{required} > f^{tilt}
\]

then arbitrage protection overrides normal constraints.

---

## Practical Approximation

Per :contentReference[oaicite:2]{index=2}:

\[
f_t
=
\arg\max_{f \in \mathcal{F}}
\left(
\mathbb{E}[\text{edge}_{t+1} \mid B_t,f]
+
\hat{V}(\phi)
\right)
\]

Feature vector:

\[
\phi =
[
\lambda_{\hat{}},
\text{arb}_{\hat{}},
\text{stale magnitude},
\text{inventory}
]
\]

Implementation characteristics:

- Discrete candidate grid search  
- Bounded fee jumps  
- Boundary-anchored fair price filter  
- Hard no-arb override  

---

## Core Insight

Mean-edge maximization reduces to:

\[
\textbf{Maximize retail spread capture given inferred } \lambda
\quad
\textbf{while strictly controlling arbitrage toxicity.}
\]

All other components are second order.

The implemented controller is a gas-constrained approximation of this optimal control problem under censored observation.