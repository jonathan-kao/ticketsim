# Engineering

How the simulator is built, and why.  The rules of the world are in
[`MODEL.md`](MODEL.md).

## 1. The event engine

A plain binary heap of `(time, priority, sequence)`.  The sequence is a
global counter, so two events never compare equal and the event itself never
takes part in the ordering - the order is fully determined, and a run is a
pure function of its configuration.

### Collapsing the event chain

A buyer's attempt used to be three events: *observe* → *submit* → *answer*.
Nothing can change the outcome between the observation and the submit (the
buyer has already decided), so the engine now schedules the **answer
directly** at `now + delay + poll_seconds`.  The order of everything that
affects the world is unchanged; two thirds of the events are gone.

`tests/test_equivalence.py` keeps the naive version alive: a reference world
with one object per buyer and the explicit three-event chain.  The engine must
match it exactly (pool trajectory, attempts, successes, failures).

### Deferring the retry observation

A buyer that lost looks at the page again - at the **end** of the moment its
request was refused, after every other answer of that moment.  That used to
be an event of its own (priority 9) per lost request: the biggest consumer of
events in a rush.  Now the lost buyers are collected and observed together
just before the engine moves past that moment (`_flush_picks`).  The point on
the timeline is the same, so the results are the same.

One subtlety: the engine has to run these deferred observations **before**
taking the next event from the heap, not after - an observation can schedule
an answer that comes before the next queued event when events are sparse.
The engine peeks at the heap first.

### Waking budget

Seats that come back wake `released × wake_budget_per_ticket` waiting buyers,
not all of them.  Waking a crowd of 100,000 for one returned ticket would turn
every cancellation into 100,000 events, and real buyers do not all refresh the
page at the same instant either.

## 2. Randomness and paired comparisons

The crowd is drawn from two Mersenne Twister channels per session, in arrival
order.  **Every other random number is a keyed hash** (BLAKE2b, 8 bytes, read
as a big-endian integer and divided by 2⁶⁴) of the entity it belongs to:
`"{seed}:b:{session}:{buyer}:cellpick:{attempt}"`,
`"{seed}:order:b:{session}:{buyer}:refund"`, and so on.  A `HashStream` adds a
counter for entities that need several draws.

Why: with draw-order randomness, changing any rule shifts every random
number after the first divergence, and two scenarios become two unrelated
samples.  With keyed randomness, the same buyer makes the same choices in
both, and the difference between them is the effect of the rule - a paired
comparison with far less noise.  `tests/test_invariants.py` checks it: a
change of the payment window changes the outcome but not the crowd's
fingerprint.

The crowd is cached per `(seed, session, crowd parameters)` and shared between
the runs of one seed (`crowd_cache`).  The cache key holds **every** value the
crowd depends on; a smaller key would silently reuse the buyers of another
world (`tests/test_runner.py`).

## 3. Two crowd implementations

`ObjectCrowd` keeps one object per buyer: easy to read and debug.
`SyntheticCrowd` keeps the same data in parallel `array`s - about 20 bytes per
buyer instead of 280, and nothing for the garbage collector to walk.  Both
must produce **identical** runs (`tests/test_crowd.py`, event by event).

## 4. The Rust world core

### Scope

The world - the event loop, the crowd, the sessions, the cell pools, the
orders - runs in Rust.  The configuration, the scenarios, the summary, the
runner and every interface stay in Python.  The summary in particular exists
once (`summary.py`) and is fed by both engines, so the counting rules cannot
drift apart.

```text
core/
  world/   ticketsim-world   keyed RNG, CPython's Mersenne Twister, the crowd,
                             sessions and cell pools - no Python dependency,
                             so `cargo test` runs without an interpreter
  ext/     ticketsim-core    the event engine and the PyO3 boundary (cdylib)
```

### The boundary

`Core.advance(host, max_events)` runs events and returns.  During the call the
engine borrows three things from the Python object: the trace list, the
observers and the crowd cache.  It keeps no reference to them afterwards, so
the Python side can hold the engine without a reference cycle.  The clock is a
separate frozen object backed by an atomic, readable from inside an observer
while the engine is mid-event.

The boundary is in process (PyO3, abi3 for Python ≥ 3.9): one extension
module serves every Python version, and a run crosses it once per
`advance()` call plus once per traced event when somebody consumes the trace.

### Bit for bit, not "about the same"

The acceptance criterion of the port is **identical output**: the same events
in the same order with the same float values.  These are the places a port
gets wrong without anybody noticing, and how the Rust side handles them:

| What | Requirement | In Rust | Locked by |
|---|---|---|---|
| keyed hash | the same key gives the same `u64` as `hashlib.blake2b(digest_size=8)` | an own BLAKE2b, no dependency | `cargo test`, `test_backends :: test_keyed_hash` |
| hash streams | the k-th draw of a stream is the key followed by `":k"` | `HashStream`, `stream_uniform` | golden fixtures |
| event order | `(time, priority, sequence)` | `priority << 56 \| sequence` in one `u64` | golden fixtures |
| floats | the same operations in the same order, no epsilon | transcribed formula by formula; `exp`, `log`, `cos`, `sin` call the platform's libm, the very symbols CPython's `math` uses | golden, `test_crowd` |
| rounding | `int(round(x))` rounds half to even; `round(x, n)` is correctly rounded | `round_ties_even()`; `py_round` formats and parses | `test_backends :: test_round` (6,000+ values) |
| iteration order | wherever Python relies on dict order | insertion-ordered `Vec`s (`alive`, `limbo`, orders by id) | `test_backends` |
| sort stability | `sorted(key=...)` is stable | `sort_by` (stable) and a precomputed rank | the hand-written seating plan cases |
| crowd channels | CPython's `random.Random(seed)` | `PyRandom`: MT19937 with CPython's `init_by_array` seeding | `cargo test`, `test_crowd` |
| `random.gauss` | CPython's algorithm, including its cached second value | `PyRandom::gauss`; `sin` and `cos` kept apart so the compiler cannot fuse them into `sincos` | `cargo test`, `test_backends :: test_crowd` |

The transcendental functions come from the platform's libm, so the two
engines agree on the same platform and change **together** on another one.
The golden fixtures were written on macOS/arm64.

### Choosing the engine

`Engine(config, backend=...)` builds the Rust engine by default and the Python
reference on request (`backend="python"` or `TICKETSIM_BACKEND=python`).
There is **no silent fallback**: if the extension is not built, importing the
Rust engine fails with the build instructions.  A fallback would make a
missing build look like a performance regression.

The Python reference stays.  It is the readable specification of every rule,
and it is what the differential test compares the Rust core against; a
mechanism change is made in both.

## 5. The executable specification

`tests/golden/*.json` stores the **complete event stream** of nine small
scenarios - every observation, request and answer of every buyer, every
refund, expiry and batch - plus the pool trajectory and the summary, in a
language-independent form: floats as their IEEE-754 bytes (`f64:...`), sorted
keys, a sha256 of the payload (`ticketsim/golden.py`).

Comments rot; a fixture does not.  A refactoring is done when
`scripts/make_golden.py --check` passes on both engines.  The cases are
chosen so that every mechanism happens at least once: the opening rush,
top-ups, high demand, limits, many returned seats, both decision laws, a
single cell, staggered sale windows and a hand-written plan with ticket types.

## 6. Testing

| Layer | What it guarantees |
|---|---|
| `test_backends.py` | Rust = Python, event by event, on 52 configurations, plus the building blocks (hash, rounding, crowd draws) |
| golden fixtures | the behaviour does not change by accident |
| `test_equivalence.py` | the optimised engine = a naive agent-per-buyer simulation |
| `test_crowd.py` | the two crowd implementations agree |
| `test_invariants.py`, `test_cells.py`, `test_settlement.py` | conservation, limits, ticket accounting, order life cycle |
| `test_engine.py`, `test_traffic.py`, `test_runner.py` | the summary, the load counting, caching and parallelism change nothing |
| `test_live.py`, `ui_harness.js` | the UI bundle is the same simulation as a batch run; the page renders it |
| `test_perf.py` | performance bounds (best of three, so a busy machine does not fail them) |
| `test_docs.py` | the docs only mention parameters and indicators that exist |

## 7. Performance

| What | Why it matters |
|---|---|
| one event per attempt (collapsed chain) | the attempt loop is the whole cost of a rush |
| deferred retry observations | one fewer event per lost request |
| waking budget | a returned ticket costs a few events, not a crowd |
| parallel arrays for the crowd | 14× less memory than one object per buyer |
| the Rust core | 11-17× faster than the Python reference |
| a process pool over seeds, every variant of a seed sharing the crowd | a report of 130 runs in about 4 s |

## 8. The web UI

The UI does not stream the simulation.  It sends the parameters once; the
backend runs the whole scenario and returns one bundle (stock and counters
every 0.25 simulated seconds, the true stock of every cell every 2 s, the
batches, the summary).  The browser plays it back locally: playing, pausing,
seeking and zooming never touch the network.  A full run takes well under a
second, so streaming would only add re-rendering on every poll.

The server is the standard library's `http.server` - no dependencies.  The
page is one HTML file with inline SVG charts and no build step.

## 9. Toolchain

`mise.toml` pins the Rust version and installs it **inside the project**
(`.rustup/`, `.cargo/`, both git-ignored).  `mise run build` builds the
extension with `PYO3_BUILD_EXTENSION_MODULE` set (an extension module must not
link libpython) and copies it into the package atomically - a running
interpreter may have the old file mapped.  `mise run test` runs the Rust unit
tests, the Python suite and the golden check on both engines.  CI
(`.github/workflows/ci.yml`) runs the same task plus the UI harness on
macOS/arm64, the platform the fixtures were written on.
