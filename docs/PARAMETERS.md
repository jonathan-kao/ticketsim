# Parameters

Every parameter of a run, its default and what it does.  All defaults are
**synthetic**: they describe a plausible large sale, not a measured one.

The *effect* column comes from [`results/SENSITIVITY.md`](../results/SENSITIVITY.md)
(the baseline scenario, seeds 0-4, one parameter changed at a time).  The
baseline sells out in 24.3 s; 89 % of the requests are refused; the platform
sees 4.6 M requests.

How to change them:

| Where | How |
|---|---|
| CLI | `python3 -m ticketsim run --config my.json` (`run --json out.json` writes a complete configuration to start from) |
| code | `Config` is a frozen dataclass tree: `with_scenario()`, `with_sessions()`, `with_behavior()`, `with_constants()`, `with_seed()` |
| web UI | the parameter form, or the JSON editor below it |
| new scenario | `ticketsim/scenarios.py` |

`python3 -m ticketsim constants --preset <name>` prints the values in force
with their sources.

## Scenario

| Parameter | Default | Meaning | Effect |
|---|---|---|---|
| `scenario.buyers` | 127,500 (baseline) | the crowd, shared out over the sessions by stock | 42,500 → first sell-out 110 s; 510,000 → 15 s and 12.4 M requests |
| `scenario.horizon` | 1800 | simulated seconds | |
| `sessions[].seats` | - | the stock (0 = the sum of a hand-written plan) | with the crowd, sets the demand ratio |
| `sessions[].per_buyer_limit` | 2 | tickets one buyer may buy in the session, in total | 1 → 26.9 s; 4 → 22.0 s (bigger orders sell out faster) |
| `sessions[].open_at` / `close_at` | 0 / 0 | the sale window (`close_at = 0`: until the horizon) | no new attempt after the close |
| `sessions[].cells` | none | a hand-written seating plan: label, capacity, price, heat, ticket type | replaces the automatic split |

## Buyers and platform rules (`behavior`)

| Parameter | Default | Meaning | Effect |
|---|---|---|---|
| `behavior.refund_prob` | 0.03 | share of orders cancelled at the decision time (seats back at once) | 0 → 3.9 M requests; 0.10 → 6.6 M |
| `behavior.clear_prob` | 0.08 | share of the rest never paid (seats back with a batch) | 0 → 4.3 M requests; 0.20 → 5.3 M |
| `behavior.payment_window` | 600 | seconds an unpaid order lives | 300 / 900 → hardly any change in the baseline |
| `behavior.clear_period` | 720 | seconds between two batches of returned seats | 240 / 1440 → hardly any change in the baseline |
| `behavior.buyer_settle` | deadline weighted | when buyers decide: `kind` (`deadline_weighted` / `uniform`), `early_frac`, `early_range`, `late_range` | `uniform` → 4.9 M requests |
| `behavior.hot_buyer_frac` | 0.30 | share of buyers who chase the most popular cell | 0 → 19.4 s, 3.9 M requests; 1.0 → 299 s, 39 M requests |
| `behavior.wake_budget_per_ticket` | 3 | waiting buyers woken per returned ticket | 1 → 4.2 M requests; 30 → 8.2 M |

## Model constants (`model_constants`)

| Parameter | Default | Meaning | Effect |
|---|---|---|---|
| `model_constants.buyer_mix` | fast 0.5 % / mid 5 % / slow 94.5 % | speed tiers: share, median delay, log-normal spread, floor | the hidden lever of every rush: who answers first |
| `model_constants.buyer_qty_weights` | 0.74 / 0.16 / 0.06 / 0.04 | how many tickets a buyer wants (1-4), truncated at the limit | |
| `model_constants.poll_seconds` | 5.0 | seconds between a request and its answer | 0.5 → 12.2 s, 1.1 M requests; 20 → 57 s, 23 M requests |
| `model_constants.poll_interval` | 0.5 | seconds between two polls while a request waits | only the load count |
| `model_constants.area_count` | 40 | cells of an automatic seating plan | 1 → 7.5 s, 1.9 M requests; 200 → 31.6 s, 8.1 M |
| `model_constants.area_capacity_shape` | `zipf` | `zipf` (few big, many small cells) or `uniform` | `uniform` → 8.5 s, 2.8 M requests |
| `model_constants.area_capacity_spread` | 1.1 | the zipf exponent | 0.5 → 17.9 s; 2.0 → 25.5 s |
| `model_constants.area_heat_weight` | 1.0 | how strongly small means popular | no effect on an automatic plan (the popularity order is by size either way); matters for hand-written heats |
| `model_constants.count_jitter` | 0.10 | ±share on the realised crowd size | |
| `model_constants.crowd_impl` | `synthetic` | `synthetic` (parallel arrays) or `object` (one object per buyer); identical results | memory only |
| `model_constants.seed` | 20261106 | the master seed | |

## Constants in the code

| Where | What | Value |
|---|---|---|
| `engine.py` | event priorities | see [`MODEL.md`](MODEL.md) §1 |
| `live.py` | UI sampling | stock every 0.25 s, cells every 2 s (simulated) |
| `scenarios.py` | `DEFAULT_RATIO` | 3 buyers per seat in the baseline |
| `scenarios.py` | demand scans | venues scaled to 1/10 (same ratio, a tenth of the cost) |
