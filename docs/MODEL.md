# The model

The rules of the simulated world, one mechanism at a time.  Every rule names
the code that implements it; the Python reference (`ticketsim/engine.py`) and
the Rust core (`core/ext/src/engine.rs`) implement the same rules bit for bit.
The parameters are listed in [`PARAMETERS.md`](PARAMETERS.md); how the engine
is built is in [`ENGINEERING.md`](ENGINEERING.md).

## 1. Time and events

The world is a discrete event simulation: a heap of `(time, priority,
sequence)`.  Two events at the same time run in priority order, two events of
the same priority in the order they were scheduled.  A run is therefore
reproducible bit for bit.

| Priority | Event | What happens |
|---:|---|---|
| 0 | `sale_open` | a session opens; its crowd is built |
| 1 | `buyer_wave` | the whole crowd looks at the inventory page |
| 2 | `clear` | a batch: the seats of expired orders go back on sale |
| 7 | `buyer_grant` | the platform answers one request |
| 8 | `settle` / `expire` | a buyer decides about an order / an unpaid order expires |
| 9 | `buyer_observe` | a woken or topping-up buyer looks at the page again |

The run starts when the first session opens and stops at `scenario.horizon`.
Nothing before the opening is modelled.

## 2. Sessions and the seating plan

A session is one event on sale: a number of seats, a purchase limit per buyer
(`per_buyer_limit`) and a sale window (`open_at`, `close_at`).

The seats are split into **cells** - one per area of the seating plan,
optionally one per ticket type.  A cell has a capacity, a label, an optional
price and a **heat** (how popular it is).  The plan is either written by hand
(`SessionSpec.cells`) or split automatically into `area_count` cells
(`schema.py :: resolve_cells`):

- `zipf` gives a few big cells and many small ones (weight `1 / rank^spread`),
  `uniform` gives cells of equal size;
- the weights are shuffled with a session-keyed seed, and the rounding
  remainder goes to the cells with the largest fractional part, so the
  capacities always add up to the seats exactly;
- the heat is `area_heat_weight × (1 − capacity / largest capacity)`: small
  cells are the popular ones.

### The ticket pool

Every cell keeps three numbers: **on sale**, **pending** (in an unpaid
order) and **paid**.  The invariant, checked by both engines and by the tests:

    capacity = on sale + pending + paid      (for every cell)

A request takes tickets from one cell, all or nothing (`pool.py :: take`).

## 3. The crowd

`scenario.buyers` is shared out over the sessions in proportion to their
stock (`count_jitter` adds ±10 % to the realised count).  Each buyer is drawn
once, in arrival order (`crowd.py :: _draws`):

| Property | How it is drawn |
|---|---|
| speed tier | `buyer_mix`: a share, a median delay, a log-normal spread and a hard floor per tier |
| first delay | `max(floor, median × exp(N(0,1) × spread))` |
| desired quantity | `buyer_qty_weights`, truncated at the session's purchase limit |
| hot or not | a keyed draw against `hot_buyer_frac` |

The delay is the time from looking at the page to submitting a request.  The
delay before every later attempt is a fresh draw from the same tier, keyed by
the buyer and the attempt number, so it does not depend on what other buyers
did.

The **whole crowd arrives at the opening** - everybody is waiting on the
page when the sale starts.

## 4. One attempt

An attempt (`engine.py :: _buyer_plan`, `_buyer_request`):

1. **Look.**  The buyer reads the inventory page.  If the sale window is
   over, the buyer is done.  If the buyer already holds `per_buyer_limit`
   tickets, or as many as it wanted, it is done.
2. **Choose a cell.**  A *hot* buyer takes the most popular cell that still
   has tickets; the others pick uniformly among the cells that have tickets
   (a keyed draw per buyer and attempt).  If nothing is left, the buyer
   **waits** (see §5).
3. **Read the number.**  The page shows the exact stock of a small cell and
   only the purchase limit for a bigger one ("available").  So the buyer sees
   `min(stock, per_buyer_limit)`.
4. **Ask.**  The quantity is `min(wanted, quota left, what the page shows)`.
   Nobody asks for more than it saw - a self check counts violations.
5. **Wait for the answer.**  The request is submitted after the buyer's
   delay and answered `poll_seconds` later.  The answer is decided **at that
   moment**: the cell must still have the tickets then.

The observation, the submit and the answer are one event in the engine (see
[`ENGINEERING.md`](ENGINEERING.md), "Collapsing the event chain"); the order
of everything that happens is the same as with three separate events.

A request fails when the cell has fewer tickets than asked.  It is a **stale**
failure when the buyer had seen more than the cell held at the answer - the
typical failure of a rush, where hundreds of buyers aim at the same few
tickets.

## 5. Losing, waiting and waking

- **A buyer that lost tries again** at the end of the same moment: it looks
  at the page again and, if something is on sale, sends a new request after
  its own delay.  There is no retry limit.
- **A buyer that finds nothing waits** (it is *parked*).  It does not poll.
- **Seats coming back wake waiting buyers**: `released × wake_budget_per_ticket`
  of them (at least one), in the order they started waiting.  Waking the whole
  crowd for a single returned ticket would be both unrealistic and a stampede
  of millions of events.

## 6. Orders

The first granted request of a buyer opens an **order**.  A buyer that got
fewer tickets than it wanted (the cell only had one left, say) comes back
after its delay and **tops up**: later grants merge into the open order, from
any cell.  An order therefore records its tickets per cell.

A buyer never holds more than `per_buyer_limit` tickets in a session, top-ups
included.  A cancelled or expired order does not give the quota back.

## 7. What a buyer does with an order

When an order is opened, the buyer's **decision time** is drawn
(`buyer_settle`: by default 30 % decide within two minutes, 70 % just before
the ten-minute payment deadline).  At that moment one keyed draw per order
decides:

| Outcome | Probability | What happens to the seats |
|---|---|---|
| **cancel** | `refund_prob` | back on sale at once, waiting buyers are woken |
| **never pay** | `clear_prob` (of the rest) | the order expires `payment_window` seconds after it was placed; the seats then wait in limbo for the next **batch** |
| **pay** | the rest | sold for good; a paid order is never taken back |

**Batches.**  Every `clear_period` seconds after the opening, the seats of all
orders that expired since the last batch go back on sale together
(`engine.py :: _on_clear`).  Large batches wake many buyers at once - the
second rush of a sale.

## 8. Sale windows

A session opens at `open_at`; its crowd arrives then.  If `close_at` is set,
no buyer starts a new attempt after it.  Requests sent before the close are
still answered.

## 9. Randomness

Two Mersenne Twister channels per session draw the crowd (`buyers:<session>`
for the tier and the first delay, `qty:<session>` for the quantity).
**Everything else is a keyed hash** of the entity it belongs to - the retry
delay of buyer 7's third attempt, the cell pick of that attempt, the decision
about order `b:arena:42` - so it does not depend on the order in which things
happen (`rng.py`).

This makes comparisons **paired**: two scenarios with the same seed and the
same crowd parameters see the same buyers making the same choices, and differ
only where the rule that changed makes them differ.

## 10. Indicators

`RunResult.summary` (`summary.py :: run_summary`):

| Indicator | Meaning |
|---|---|
| `seats` | the stock of all sessions |
| `tickets_sold` | every ticket ever granted, resales of returned seats included |
| `tickets_held` | tickets in orders that still exist (pending or paid) at the end |
| `sell_through` | `tickets_held / seats` |
| `stranded_stock` | tickets still on sale at the end |
| `sessions_sold_out` / `first_sold_out_at` | how many sessions ran out of stock, and when the first did |
| `refunded` / `cleared` | tickets returned by cancellation / in batches |
| `buyer_attempts` / `buyer_successes` / `buyer_failures` | the requests and their answers |
| `buyer_failure_rate` | failures / requests |
| `buyer_stale_failures` | failures where the buyer had seen more than the cell held |
| `buyer_cell_short_failures` | failures where the cell held less than the request |
| `buyer_retries` | attempts after a lost request |
| `buyer_parked` / `buyer_wakes` | how often buyers had to wait / were woken |
| `buyer_capped` | buyers stopped by the purchase limit (0 by construction: the desired quantity is already truncated; a self check) |
| `buyer_fingerprint` | a rolling digest of the crowd's draws (reproducibility check) |
| `traffic` / `traffic_total` | the platform load by stage, see §11 |
| `requests_per_second` / `requests_per_ticket` | the load per simulated second / per granted ticket |
| `conservation_ok` | the pool invariant held in every cell |
| `rule_violations` | requests for more than the buyer saw (must stay 0) |

## 11. Platform load

Every request the crowd sends is counted by stage (`summary.py ::
traffic_summary`):

| Stage | What | Count |
|---|---|---|
| `inventory` | a look at the inventory page | one per observation |
| `submit` | a purchase request | one per attempt |
| `poll` | waiting for the answer | attempts × `poll_seconds / poll_interval` |
| `checkout` | paying for an order | one per order |

The stages are counted, not weighted: how expensive each request is for the
platform is a separate question.  The web UI shows the load over time
(requests per second, averaged over two seconds; a request's polls are
counted when it is sent).

## 12. What the model leaves out

- Arrivals over time: the whole crowd is there at the opening.
- Queues, waiting rooms and request rate limits: every request reaches the
  platform.
- Prices and preferences beyond "popular cell first" and "any cell".
- Buyers giving up: a buyer keeps trying until it has what it wants, the
  session is sold out (then it waits), or the sale window closes.
