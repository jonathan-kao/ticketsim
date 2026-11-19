# ticketsim - scenario report

> Written by `scripts/run_experiments.py`; every number comes from a run, none is typed in by hand.

Seeds 0-9 (10 runs per scenario, the same seeds for every scenario: the rows compare pairwise).  Every value is the mean over the seeds.

## 1. Scenarios

| scenario | what it changes |
|---|---|
| `baseline` | two venues on sale at once, crowd = 3x the stock, limit 2 |
| `demand_20x` | crowd = 20x the stock (venues scaled to 1/10, same ratio) |
| `demand_60x` | crowd = 60x the stock (venues scaled to 1/10, same ratio) |
| `demand_120x` | crowd = 120x the stock (venues scaled to 1/10, same ratio) |
| `limit_1` | purchase limit 1 per buyer and session |
| `limit_4` | purchase limit 4 per buyer and session |
| `payment_window_300` | unpaid orders expire after 5 minutes |
| `payment_window_900` | unpaid orders expire after 15 minutes |
| `batch_every_240` | returned seats go back on sale every 4 minutes |
| `batch_every_1440` | returned seats go back on sale every 24 minutes |
| `cells_1` | the seating plan is one single cell |
| `cells_200` | the seating plan has 200 cells |
| `uniform_decisions` | buyers decide uniformly over the payment window |

## 2. Outcome

| scenario | sell-through | first sell-out (s) | unsold | requests | refused share | refused: stale | platform requests | requests / ticket |
|---|---|---|---|---|---|---|---|---|
| `baseline` | 0.9999 | 24.2 | 0.0 | 374,421 | 0.891 | 333,804 | 4,637,479 | 97.4 |
| `demand_20x` | 1.0000 | 10.6 | 0.0 | 143,177 | 0.972 | 139,107 | 1,805,708 | 380.0 |
| `demand_60x` | 0.9999 | 7.5 | 0.0 | 271,731 | 0.985 | 267,654 | 3,520,512 | 739.6 |
| `demand_120x` | 0.9999 | 7.5 | 0.0 | 540,537 | 0.993 | 536,466 | 7,004,379 | 1471.8 |
| `limit_1` | 0.9999 | 26.8 | 0.1 | 384,565 | 0.876 | 336,951 | 4,759,073 | 100.0 |
| `limit_4` | 0.9999 | 21.8 | 0.0 | 364,391 | 0.905 | 329,976 | 4,517,332 | 94.8 |
| `payment_window_300` | 0.9997 | 24.2 | 0.1 | 374,470 | 0.891 | 333,865 | 4,638,037 | 97.5 |
| `payment_window_900` | 0.9995 | 24.2 | 0.0 | 371,796 | 0.892 | 331,497 | 4,604,910 | 97.5 |
| `batch_every_240` | 1.0000 | 24.2 | 0.1 | 374,427 | 0.891 | 333,803 | 4,637,570 | 97.4 |
| `batch_every_1440` | 1.0000 | 24.2 | 0.0 | 372,061 | 0.892 | 331,744 | 4,608,158 | 97.5 |
| `cells_1` | 0.9999 | 7.5 | 0.0 | 144,690 | 0.719 | 104,058 | 1,880,955 | 39.5 |
| `cells_200` | 0.9999 | 31.1 | 0.1 | 681,484 | 0.939 | 640,785 | 8,322,257 | 174.8 |
| `uniform_decisions` | 0.9999 | 24.2 | 0.1 | 391,827 | 0.896 | 351,213 | 4,846,386 | 101.8 |

- **sell-through**: tickets held at the end / seats.  Cancelled and unpaid orders count only if their seats were sold again.
- **first sell-out**: the first moment a session had no ticket left (a returned seat can put it back on sale).
- **refused: stale**: the request failed because the cell held fewer tickets than the buyer had seen - the main way requests fail in a rush.

## 3. Platform load

| scenario | inventory | submit | poll | checkout | total | requests / s |
|---|---|---|---|---|---|---|
| `baseline` | 478,232 | 374,421 | 3,744,209 | 40,617 | 4,637,479 | 2,576.4 |
| `demand_20x` | 226,689 | 143,177 | 1,431,772 | 4,070 | 1,805,708 | 1,003.2 |
| `demand_60x` | 527,399 | 271,731 | 2,717,306 | 4,076 | 3,520,512 | 1,955.8 |
| `demand_120x` | 1,054,402 | 540,537 | 5,405,369 | 4,071 | 7,004,379 | 3,891.3 |
| `limit_1` | 481,244 | 384,565 | 3,845,650 | 47,614 | 4,759,073 | 2,643.9 |
| `limit_4` | 474,619 | 364,391 | 3,643,907 | 34,415 | 4,517,332 | 2,509.6 |
| `payment_window_300` | 478,262 | 374,470 | 3,744,700 | 40,605 | 4,638,037 | 2,576.7 |
| `payment_window_900` | 474,856 | 371,796 | 3,717,959 | 40,298 | 4,604,910 | 2,558.3 |
| `batch_every_240` | 478,245 | 374,427 | 3,744,273 | 40,624 | 4,637,570 | 2,576.4 |
| `batch_every_1440` | 475,171 | 372,061 | 3,720,610 | 40,316 | 4,608,158 | 2,560.1 |
| `cells_1` | 248,736 | 144,690 | 1,446,897 | 40,632 | 1,880,955 | 1,045.0 |
| `cells_200` | 785,236 | 681,484 | 6,814,839 | 40,698 | 8,322,257 | 4,623.5 |
| `uniform_decisions` | 495,674 | 391,827 | 3,918,271 | 40,614 | 4,846,386 | 2,692.4 |

- One request is one HTTP-like call by a buyer: a look at the inventory page, a purchase request, a poll for its answer, or a checkout.  The stages are counted, not weighted.
- The polls are derived: every request waits `poll_seconds` for its answer and the client polls every `poll_interval` meanwhile.
- **requests / s** is averaged over the whole simulated time; the load during the opening rush is far higher (see the load chart of the web UI).

## 4. Parameters of the baseline

| parameter | value | source |
|---|---|---|
| buyers N | `127500` | scenario |
| sessions | `arena:20000, stadium:22500` | scenario |
| cells | `arena:40, stadium:40` | automatic split (area_count) or hand written |
| purchase limit | `arena:2, stadium:2` | scenario (platform rule) |
| horizon | `1800.0s` | scenario |
| refund_prob | `0.03` | synthetic assumption |
| clear_prob / clear_period | `0.08 / 720.0s` | synthetic assumption (platform rule) |
| payment_window | `600.0s` | synthetic assumption (platform rule) |
| decision time distribution | `deadline_weighted` | synthetic assumption |
| buyer retries | `unlimited, interval = the buyer's own speed` | design decision |
| hot buyer share | `30%` | synthetic assumption |
| wake budget per ticket | `3.0` | design decision |
| buyer speed mix | `fast 0.50%/0.8s(±0.3, ≥0.5s), mid 5.00%/1.8s(±0.5, ≥1.2s), slow 94.50%/4.5s(±1.0, ≥2.5s)` | synthetic assumption |
| order size | `limit 2: 1-2 tickets, mean 1.18 (w=[0.74, 0.16, 0.06, 0.04])` | synthetic assumption |
| buyer arrival | `the whole crowd at the opening` | design decision |
| poll_seconds / poll_interval | `5.0s / 0.5s` | synthetic assumption |
| cells (area_count) | `40` | synthetic assumption |
| capacity shape / spread | `zipf / 1.1` | synthetic assumption |
| heat weight | `1.0` | synthetic assumption |
| count_jitter | `±10%` | synthetic assumption |
| crowd implementation | `synthetic` | parallel arrays / one object per buyer |
| seed | `20261106` | reproducibility |
