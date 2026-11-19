# Parameter sensitivity

Scenario `baseline`, seeds 0-4: every value is the mean over the seeds.  Written by `scripts/sensitivity.py`.

| parameter | value | sell-through | first sell-out (s) | refused share | refused: stale | platform requests |
|---|---|---|---|---|---|---|
| `baseline` | - | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `scenario.buyers` | 42500 | 0.9944 | 109.5 | 0.874 | 279,839 | 3,893,316 |
| `scenario.buyers` | 127500 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `scenario.buyers` | 510000 | 0.9999 | 15.0 | 0.959 | 945,928 | 12,371,463 |
| `sessions[].per_buyer_limit` | 1 | 0.9999 | 26.9 | 0.876 | 336,548 | 4,754,285 |
| `sessions[].per_buyer_limit` | 2 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `sessions[].per_buyer_limit` | 4 | 0.9999 | 22.0 | 0.905 | 329,117 | 4,506,789 |
| `behavior.refund_prob` | 0.0 | 1.0000 | 24.2 | 0.875 | 275,514 | 3,917,054 |
| `behavior.refund_prob` | 0.03 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `behavior.refund_prob` | 0.1 | 0.9995 | 24.3 | 0.918 | 493,322 | 6,603,865 |
| `behavior.clear_prob` | 0.0 | 1.0000 | 24.3 | 0.891 | 306,020 | 4,253,575 |
| `behavior.clear_prob` | 0.08 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `behavior.clear_prob` | 0.2 | 0.9995 | 24.3 | 0.893 | 385,029 | 5,338,910 |
| `behavior.payment_window` | 300.0 | 0.9997 | 24.3 | 0.891 | 332,444 | 4,620,615 |
| `behavior.payment_window` | 600.0 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `behavior.payment_window` | 900.0 | 0.9995 | 24.3 | 0.891 | 329,860 | 4,584,960 |
| `behavior.clear_period` | 240.0 | 1.0000 | 24.3 | 0.891 | 332,110 | 4,616,775 |
| `behavior.clear_period` | 720.0 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `behavior.clear_period` | 1440.0 | 1.0000 | 24.3 | 0.891 | 330,124 | 4,588,411 |
| `behavior.buyer_settle.kind` | deadline_weighted | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `behavior.buyer_settle.kind` | uniform | 0.9999 | 24.2 | 0.897 | 355,101 | 4,892,632 |
| `behavior.hot_buyer_frac` | 0.0 | 0.9999 | 19.4 | 0.869 | 268,777 | 3,857,704 |
| `behavior.hot_buyer_frac` | 0.3 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `behavior.hot_buyer_frac` | 1.0 | 0.9987 | 299.2 | 0.988 | 3,226,371 | 39,341,321 |
| `behavior.wake_budget_per_ticket` | 1.0 | 0.9999 | 24.3 | 0.881 | 301,100 | 4,233,620 |
| `behavior.wake_budget_per_ticket` | 3.0 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `behavior.wake_budget_per_ticket` | 30.0 | 0.9999 | 24.3 | 0.938 | 616,630 | 8,159,232 |
| `model_constants.area_count` | 1 | 0.9999 | 7.5 | 0.720 | 104,183 | 1,881,748 |
| `model_constants.area_count` | 40 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `model_constants.area_count` | 200 | 0.9999 | 31.6 | 0.938 | 625,888 | 8,143,283 |
| `model_constants.area_capacity_shape` | zipf | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `model_constants.area_capacity_shape` | uniform | 0.9999 | 8.5 | 0.816 | 180,607 | 2,799,653 |
| `model_constants.area_capacity_spread` | 0.5 | 0.9999 | 17.9 | 0.874 | 280,941 | 4,003,320 |
| `model_constants.area_capacity_spread` | 1.1 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `model_constants.area_capacity_spread` | 2.0 | 1.0000 | 25.5 | 0.887 | 318,025 | 4,447,345 |
| `model_constants.area_heat_weight` | 0.0 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `model_constants.area_heat_weight` | 1.0 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `model_constants.poll_seconds` | 0.5 | 0.9999 | 12.2 | 0.876 | 288,314 | 1,131,395 |
| `model_constants.poll_seconds` | 5.0 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
| `model_constants.poll_seconds` | 20.0 | 0.9999 | 57.4 | 0.927 | 514,734 | 23,467,937 |
| `model_constants.count_jitter` | 0.0 | 0.9999 | 24.8 | 0.890 | 329,961 | 4,589,180 |
| `model_constants.count_jitter` | 0.1 | 0.9999 | 24.3 | 0.891 | 332,059 | 4,616,047 |
