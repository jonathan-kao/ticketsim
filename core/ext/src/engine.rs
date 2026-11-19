//! The discrete event engine (`ticketsim/engine.py`; docs/MODEL.md).
//!
//! This is a **bit for bit** port: the same events in the same order, the same
//! keyed random draws, the same float arithmetic (docs/ENGINEERING.md).  The
//! golden fixtures (`tests/golden/*.json`) and the differential test against
//! the Python reference engine (`tests/test_backends.py`) are the gate.
//!
//! The engine reaches the Python side through the `host` object that
//! `advance()` receives (`ticketsim/rust_engine.py`):
//!
//! * `host.trace`, `host.observers` - where traced events go;
//! * `host._crowd_cache` - crowds shared between runs of the same seed.
//!
//! The engine keeps no reference to the host between two calls, so the Python
//! side can hold the engine without creating a reference cycle.

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap, VecDeque};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};

use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

use ticketsim_world::crowd::{Crowd, CrowdSpec, Tier};
use ticketsim_world::rng;
use ticketsim_world::session::{Buyers, CellDef, CellKey, Order, OrderState, Session};

// Event priorities (lower runs first inside one timestamp).  The gaps belong
// to event kinds this model does not have; the numbers of the others are kept
// so their order is the same as in the Python reference.
const PRIO_SALE_OPEN: u64 = 0;
const PRIO_BUYER_WAVE: u64 = 1;
const PRIO_CLEAR: u64 = 2;
const PRIO_BUYER_GRANT: u64 = 7;
const PRIO_SETTLE: u64 = 8;
const PRIO_BUYER_OBSERVE: u64 = 9;

/// `round(x, ndigits)` of a Python float: the correctly rounded decimal with
/// `ndigits` places (exact ties go to the even digit), read back as a float.
pub fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse().unwrap_or(x)
}

/// The simulation clock, readable from Python at any moment - also while the
/// engine is in the middle of an event (an observer).
#[pyclass(frozen, module = "ticketsim.ticketsim_core")]
pub struct Clock {
    bits: AtomicU64,
}

#[pymethods]
impl Clock {
    #[getter]
    fn now(&self) -> f64 {
        f64::from_bits(self.bits.load(AtomicOrdering::Relaxed))
    }
}

/// A crowd that runs of the same seed share (the value of the crowd cache).
#[pyclass(frozen, module = "ticketsim.ticketsim_core")]
pub struct CrowdHandle {
    pub crowd: Arc<Crowd>,
}

#[pymethods]
impl CrowdHandle {
    fn __len__(&self) -> usize {
        self.crowd.len()
    }

    fn desired(&self, index: usize) -> i32 {
        self.crowd.desired[index]
    }

    fn first_delay(&self, index: usize) -> f64 {
        self.crowd.first_delay[index]
    }

    fn hot(&self, index: usize) -> bool {
        self.crowd.hot[index]
    }

    fn next_delay(&self, index: usize, retries: u64) -> f64 {
        self.crowd.next_delay(index, retries, &mut Vec::new())
    }
}

#[derive(Clone, Copy, Debug)]
enum Event {
    SaleOpen(u32),
    BuyerWave(u32),
    BuyerObserve {
        session: u32,
        buyer: u32,
    },
    BuyerGrant {
        session: u32,
        buyer: u32,
        cell: u32,
        qty: i32,
        observed: i32,
    },
    Clear {
        session: u32,
        cycle: u32,
    },
    Settle(u32),
    Expire(u32),
}

/// A heap entry, ordered by `(time, priority, sequence)`.  The sequence is
/// unique, so the event itself never takes part in the comparison.
struct Entry {
    time: f64,
    /// `priority << 56 | sequence`
    order: u64,
    event: Event,
}

impl PartialEq for Entry {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for Entry {}

impl PartialOrd for Entry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Entry {
    /// Reversed: `BinaryHeap` is a max-heap and the earliest event must come first.
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .time
            .partial_cmp(&self.time)
            .unwrap_or(Ordering::Equal)
            .then(other.order.cmp(&self.order))
    }
}

enum BuyerRequest {
    /// the buyer has what it wanted, or hit the purchase limit
    Done,
    /// nothing to buy right now: wait for the tickets to come back
    Empty,
    Ok {
        cell: u32,
        qty: i64,
        observed: i64,
    },
}

#[derive(Default)]
struct Metrics {
    refunded_tickets: i64,
    cleared_tickets: i64,
    tickets_sold: i64,
    buyer_attempts: i64,
    buyer_successes: i64,
    buyer_failures: i64,
    buyer_stale_failures: i64,
    buyer_cell_short_failures: i64,
    buyer_retries: i64,
    buyer_orders: i64,
    buyer_parked: i64,
    buyer_wakes: i64,
    buyer_capped: i64,
    buyer_fingerprint: u64,
    buyer_inventory_reads: i64,
    rule_violations: i64,
}

/// What the engine borrows from the Python side for the length of one `advance()`.
struct Host<'py> {
    py: Python<'py>,
    trace: Bound<'py, PyList>,
    observers: Bound<'py, PyList>,
    crowd_cache: Bound<'py, PyDict>,
}

/// Emit one traced event (`engine.py :: _trace`).  The fields are only built
/// when somebody consumes them.
macro_rules! trace {
    ($self:ident, $host:ident, $kind:literal $(, $key:literal => $value:expr)* $(,)?) => {
        if $self.wants_trace($host) {
            let item = $self.trace_item($host, intern!($host.py, $kind))?;
            $( item.set_item(intern!($host.py, $key), $value)?; )*
            $self.emit_trace($host, item)?;
        }
    };
}

fn get<'py, T>(dict: &Bound<'py, PyDict>, name: &str) -> PyResult<T>
where
    T: for<'a> FromPyObject<'a, 'py>,
    for<'a> <T as FromPyObject<'a, 'py>>::Error: Into<PyErr>,
{
    match dict.get_item(name)? {
        Some(value) => value.extract::<T>().map_err(Into::into),
        None => Err(PyKeyError::new_err(format!(
            "missing engine parameter {name:?}"
        ))),
    }
}

pub struct Engine {
    // -- parameters -----------------------------------------------------------------------
    seed_text: String,
    horizon: f64,
    clear_period: f64,
    payment_window: f64,
    refund_prob: f64,
    clear_prob: f64,
    wake_budget_per_ticket: f64,
    settle_uniform: bool,
    settle_early_frac: f64,
    settle_early_range: (f64, f64),
    settle_late_range: (f64, f64),
    poll_seconds: f64,
    buyer_mix: Vec<Tier>,
    buyer_qty_weights: Vec<f64>,
    count_jitter: f64,
    hot_buyer_frac: f64,
    /// the crowd cache key of every session (built by the Python side)
    crowd_keys: Vec<Py<PyAny>>,
    trace_enabled: bool,
    trace_limit: usize,
    trace_buyers: bool,
    trace_exact: bool,

    // -- state ----------------------------------------------------------------------------
    now: f64,
    clock: Py<Clock>,
    heap: BinaryHeap<Entry>,
    seq: u64,
    started: bool,
    finished: bool,
    done: bool,
    pub events_processed: u64,
    pub(crate) sessions: Vec<Session>,
    session_keys: Vec<Py<PyString>>,
    session_index: HashMap<String, u32>,
    pub(crate) orders: Vec<Order>,
    /// observations deferred to the end of their timestamp (`_flush_picks`)
    pending_picks: Vec<(u32, u32)>,
    pending_time: f64,
    pool_history: Vec<(f64, u32, i64)>,
    metrics: Metrics,
    scratch: Vec<u8>,
}

impl Engine {
    // ----------------------------------------------------------------------------------
    // construction
    // ----------------------------------------------------------------------------------
    pub fn new(py: Python<'_>, cfg: &Bound<'_, PyDict>) -> PyResult<Engine> {
        let seed_text: String = get(cfg, "seed")?;
        let mut sessions = Vec::new();
        let mut session_keys = Vec::new();
        let mut session_index = HashMap::new();
        let mut crowd_keys = Vec::new();
        let session_list: Bound<'_, PyList> = get(cfg, "sessions")?;
        for item in session_list.iter() {
            let spec = item.cast::<PyDict>()?;
            let key: String = get(spec, "key")?;
            let mut cells = Vec::new();
            let cell_list: Vec<(i64, i64, String, i64, f64)> = get(spec, "cells")?;
            for (area, ticket_type, label, capacity, heat) in cell_list {
                cells.push(CellDef {
                    key: (area, ticket_type),
                    label,
                    capacity,
                    heat,
                });
            }
            if session_index
                .insert(key.clone(), sessions.len() as u32)
                .is_some()
            {
                return Err(PyValueError::new_err(format!(
                    "duplicate session key {key:?}"
                )));
            }
            session_keys.push(PyString::new(py, &key).unbind());
            crowd_keys.push(get::<Py<PyAny>>(spec, "crowd_key")?);
            sessions.push(Session::new(
                key,
                get(spec, "per_buyer_limit")?,
                get(spec, "open_at")?,
                get(spec, "close_at")?,
                get(spec, "buyers")?,
                cells,
            ));
        }

        let mix: Vec<(String, f64, f64, f64, f64)> = get(cfg, "buyer_mix")?;
        let buyer_mix = mix
            .into_iter()
            .map(|(name, share, median, spread, floor)| Tier {
                name,
                share,
                median,
                spread,
                floor,
            })
            .collect();
        let settle_kind: String = get(cfg, "buyer_settle_kind")?;
        let trace_limit: i64 = get::<i64>(cfg, "trace_limit")?.max(0);
        Ok(Engine {
            horizon: get(cfg, "horizon")?,
            clear_period: get(cfg, "clear_period")?,
            payment_window: get(cfg, "payment_window")?,
            refund_prob: get(cfg, "refund_prob")?,
            clear_prob: get(cfg, "clear_prob")?,
            wake_budget_per_ticket: get(cfg, "wake_budget_per_ticket")?,
            settle_uniform: settle_kind == "uniform",
            settle_early_frac: get(cfg, "buyer_settle_early_frac")?,
            settle_early_range: get(cfg, "buyer_settle_early_range")?,
            settle_late_range: get(cfg, "buyer_settle_late_range")?,
            poll_seconds: get(cfg, "poll_seconds")?,
            buyer_mix,
            buyer_qty_weights: get(cfg, "buyer_qty_weights")?,
            count_jitter: get(cfg, "count_jitter")?,
            hot_buyer_frac: get(cfg, "hot_buyer_frac")?,
            crowd_keys,
            trace_enabled: get(cfg, "trace_enabled")?,
            trace_limit: trace_limit as usize,
            trace_buyers: get(cfg, "trace_buyers")?,
            trace_exact: get(cfg, "trace_exact")?,
            seed_text,
            now: 0.0,
            clock: Py::new(
                py,
                Clock {
                    bits: AtomicU64::new(0f64.to_bits()),
                },
            )?,
            heap: BinaryHeap::new(),
            seq: 0,
            started: false,
            finished: false,
            done: false,
            events_processed: 0,
            sessions,
            session_keys,
            session_index,
            orders: Vec::new(),
            pending_picks: Vec::new(),
            pending_time: 0.0,
            pool_history: Vec::new(),
            metrics: Metrics::default(),
            scratch: Vec::with_capacity(128),
        })
    }

    pub fn clock(&self, py: Python<'_>) -> Py<Clock> {
        self.clock.clone_ref(py)
    }

    pub fn is_done(&self) -> bool {
        self.done
    }

    pub fn session_position(&self, key: &str) -> PyResult<usize> {
        self.session_index
            .get(key)
            .map(|&index| index as usize)
            .ok_or_else(|| PyKeyError::new_err(key.to_string()))
    }

    // ----------------------------------------------------------------------------------
    // event plumbing
    // ----------------------------------------------------------------------------------
    #[inline]
    fn set_now(&mut self, time: f64) {
        self.now = time;
        self.clock
            .get()
            .bits
            .store(time.to_bits(), AtomicOrdering::Relaxed);
    }

    #[inline]
    fn push(&mut self, time: f64, priority: u64, event: Event) {
        self.seq += 1;
        self.heap.push(Entry {
            time,
            order: (priority << 56) | self.seq,
            event,
        });
    }

    /// Push the opening events (`engine.py :: start`).
    pub fn start(&mut self) {
        if self.started {
            return;
        }
        for index in 0..self.sessions.len() {
            let open_at = self.sessions[index].open_at;
            self.push(open_at, PRIO_SALE_OPEN, Event::SaleOpen(index as u32));
        }
        self.started = true;
    }

    /// Process at most `max_events` events; `true` when the run is over.
    pub fn advance(&mut self, host: &Bound<'_, PyAny>, max_events: u64) -> PyResult<bool> {
        if self.done {
            return Ok(true);
        }
        let py = host.py();
        let host = Host {
            py,
            trace: host.getattr(intern!(py, "trace"))?.cast_into::<PyList>()?,
            observers: host
                .getattr(intern!(py, "observers"))?
                .cast_into::<PyList>()?,
            crowd_cache: host
                .getattr(intern!(py, "_crowd_cache"))?
                .cast_into::<PyDict>()?,
        };
        let host = &host;
        if !self.started {
            self.start();
        }
        let mut processed = 0u64;
        while processed < max_events {
            if !self.pending_picks.is_empty()
                && self
                    .heap
                    .peek()
                    .is_none_or(|next| next.time > self.pending_time)
            {
                // The deferred observations belong to the end of an earlier
                // moment, and the events they schedule may come before the next
                // one in the queue: run them before taking it.
                self.flush_picks(host)?;
                continue;
            }
            let Some(entry) = self.heap.pop() else { break };
            if entry.time > self.horizon {
                self.finished = true;
                break;
            }
            self.set_now(entry.time);
            self.dispatch(host, entry.event)?;
            processed += 1;
        }
        self.events_processed += processed;
        if self.finished || self.heap.is_empty() {
            if !self.pending_picks.is_empty() {
                self.flush_picks(host)?;
            }
            self.set_now(self.horizon);
            self.done = true;
            return Ok(true);
        }
        Ok(false)
    }

    fn dispatch(&mut self, host: &Host<'_>, event: Event) -> PyResult<()> {
        match event {
            Event::SaleOpen(session) => self.on_sale_open(host, session),
            Event::BuyerWave(session) => self.on_buyer_wave(host, session),
            Event::BuyerObserve { session, buyer } => self.buyer_plan(host, session, buyer),
            Event::BuyerGrant {
                session,
                buyer,
                cell,
                qty,
                observed,
            } => self.on_buyer_grant(host, session, buyer, cell, qty as i64, observed as i64),
            Event::Clear { session, cycle } => self.on_clear(host, session, cycle),
            Event::Settle(order) => self.on_settle(host, order),
            Event::Expire(order) => self.on_expire(host, order),
        }
    }

    /// Run the observations that were deferred to the end of their timestamp
    /// (`engine.py :: _flush_picks`).
    fn flush_picks(&mut self, host: &Host<'_>) -> PyResult<()> {
        if self.pending_picks.is_empty() {
            return Ok(());
        }
        let pending = std::mem::take(&mut self.pending_picks);
        let now = self.now;
        self.set_now(self.pending_time);
        for &(session, buyer) in &pending {
            self.buyer_plan(host, session, buyer)?;
        }
        self.set_now(now);
        Ok(())
    }

    // -- tracing ---------------------------------------------------------------------------
    #[inline]
    fn wants_trace(&self, host: &Host<'_>) -> bool {
        (self.trace_enabled && host.trace.len() < self.trace_limit) || host.observers.len() > 0
    }

    fn trace_item<'py>(
        &self,
        host: &Host<'py>,
        kind: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let item = PyDict::new(host.py);
        item.set_item(intern!(host.py, "t"), py_round(self.now, 4))?;
        item.set_item(intern!(host.py, "kind"), kind)?;
        Ok(item)
    }

    fn emit_trace<'py>(&self, host: &Host<'py>, item: Bound<'py, PyDict>) -> PyResult<()> {
        if self.trace_exact {
            item.set_item(intern!(host.py, "t_exact"), self.now)?;
        }
        for observer in host.observers.iter() {
            observer.call1((&item,))?;
        }
        if self.trace_enabled && host.trace.len() < self.trace_limit {
            host.trace.append(item)?;
        }
        Ok(())
    }

    fn session_key<'py>(&self, py: Python<'py>, session: u32) -> &Bound<'py, PyString> {
        self.session_keys[session as usize].bind(py)
    }

    fn cell_key(&self, session: u32, cell: u32) -> CellKey {
        self.sessions[session as usize].cells[cell as usize].key
    }

    // ----------------------------------------------------------------------------------
    // world
    // ----------------------------------------------------------------------------------
    fn on_sale_open(&mut self, host: &Host<'_>, session: u32) -> PyResult<()> {
        trace!(self, host, "sale_open", "session" => self.session_key(host.py, session));
        self.start_buyer_stream(host, session)?;
        let open_at = self.sessions[session as usize].open_at;
        self.push(open_at, PRIO_BUYER_WAVE, Event::BuyerWave(session));
        let first_clear = open_at + self.clear_period;
        if first_clear <= self.horizon {
            self.push(first_clear, PRIO_CLEAR, Event::Clear { session, cycle: 1 });
        }
        Ok(())
    }

    /// The buyers of one session: built once per (seed, session, ...) and
    /// shared through the crowd cache.
    fn start_buyer_stream(&mut self, host: &Host<'_>, session: u32) -> PyResult<()> {
        let at = session as usize;
        let key = self.crowd_keys[at].bind(host.py);
        let crowd = match host.crowd_cache.get_item(key)? {
            Some(cached) => cached.cast::<CrowdHandle>()?.get().crowd.clone(),
            None => {
                let spec = CrowdSpec {
                    seed_text: self.seed_text.clone(),
                    session: self.sessions[at].key.clone(),
                    count: self.sessions[at].buyer_count,
                    mix: self.buyer_mix.clone(),
                    qty_weights: self.buyer_qty_weights.clone(),
                    count_jitter: self.count_jitter,
                    hot_buyer_frac: self.hot_buyer_frac,
                    max_qty: self.sessions[at].per_buyer_limit,
                };
                let crowd = Arc::new(Crowd::build(&spec).map_err(PyValueError::new_err)?);
                host.crowd_cache.set_item(
                    key,
                    Py::new(
                        host.py,
                        CrowdHandle {
                            crowd: crowd.clone(),
                        },
                    )?,
                )?;
                crowd
            }
        };
        let count = crowd.len();
        self.sessions[at].buyers = Some(Buyers {
            crowd,
            parked: VecDeque::new(),
            attempts: vec![0; count],
            bought: vec![0; count],
            order: vec![0; count],
        });
        Ok(())
    }

    /// The whole crowd reads the inventory page at the moment the sale opens.
    fn on_buyer_wave(&mut self, host: &Host<'_>, session: u32) -> PyResult<()> {
        let at = session as usize;
        let Some(buyers) = self.sessions[at].buyers.as_ref() else {
            return Ok(());
        };
        let crowd = buyers.crowd.clone();
        let open_at = self.sessions[at].open_at;
        let poll = self.poll_seconds;
        for index in 0..crowd.len() {
            // a rolling digest of the buyer draws (the reproducibility check)
            self.metrics.buyer_fingerprint = ((self.metrics.buyer_fingerprint as u128 * 1000003
                + (index as u128 + 1) * 7919
                + crowd.desired[index] as u128 * 17)
                % (1u128 << 61)) as u64;
            self.metrics.buyer_inventory_reads += 1;
            let (cell, qty, observed) = match self.buyer_request(session, index as u32) {
                BuyerRequest::Ok {
                    cell,
                    qty,
                    observed,
                } => (cell, qty, observed),
                BuyerRequest::Empty => {
                    self.park(host, session, index as u32)?;
                    continue;
                }
                BuyerRequest::Done => continue,
            };
            let delay = crowd.first_delay[index];
            self.sessions[at].buyers.as_mut().unwrap().attempts[index] += 1;
            self.sessions[at].buyer_attempts += 1;
            self.metrics.buyer_attempts += 1;
            if qty > observed {
                self.metrics.rule_violations += 1;
            }
            if self.trace_buyers {
                trace!(self, host, "buyer_req",
                       "session" => self.session_key(host.py, session), "buyer" => index,
                       "cell" => self.cell_key(session, cell), "qty" => qty,
                       "observed" => observed, "delay" => delay);
            }
            self.push(
                open_at + delay + poll,
                PRIO_BUYER_GRANT,
                Event::BuyerGrant {
                    session,
                    buyer: index as u32,
                    cell,
                    qty: qty as i32,
                    observed: observed as i32,
                },
            );
        }
        if self.trace_buyers {
            trace!(self, host, "buyer_wave",
                   "session" => self.session_key(host.py, session), "buyers" => crowd.len());
        }
        Ok(())
    }

    /// Observe the inventory page and schedule the platform's answer: one attempt
    /// is one event (`engine.py :: _buyer_plan`).
    fn buyer_plan(&mut self, host: &Host<'_>, session: u32, buyer: u32) -> PyResult<()> {
        self.metrics.buyer_inventory_reads += 1;
        let (cell, qty, observed) = match self.buyer_request(session, buyer) {
            BuyerRequest::Ok {
                cell,
                qty,
                observed,
            } => (cell, qty, observed),
            BuyerRequest::Empty => return self.park(host, session, buyer),
            BuyerRequest::Done => return Ok(()),
        };
        let at = session as usize;
        let index = buyer as usize;
        let buyers = self.sessions[at].buyers.as_mut().unwrap();
        let count = buyers.attempts[index];
        let delay = if count == 0 {
            buyers.crowd.first_delay[index]
        } else {
            buyers
                .crowd
                .next_delay(index, count as u64 - 1, &mut self.scratch)
        };
        buyers.attempts[index] = count + 1;
        self.sessions[at].buyer_attempts += 1;
        self.metrics.buyer_attempts += 1;
        if qty > observed {
            self.metrics.rule_violations += 1;
        }
        if self.trace_buyers {
            trace!(self, host, "buyer_req",
                   "session" => self.session_key(host.py, session), "buyer" => index,
                   "cell" => self.cell_key(session, cell), "qty" => qty,
                   "observed" => observed, "delay" => delay);
        }
        self.push(
            self.now + delay + self.poll_seconds,
            PRIO_BUYER_GRANT,
            Event::BuyerGrant {
                session,
                buyer,
                cell,
                qty: qty as i32,
                observed: observed as i32,
            },
        );
        Ok(())
    }

    /// What a buyer wants to buy right now (`engine.py :: _buyer_request`) -
    /// the hottest function of the simulation: once per attempt.
    fn buyer_request(&mut self, session: u32, buyer: u32) -> BuyerRequest {
        let index = buyer as usize;
        let sess = &mut self.sessions[session as usize];
        if sess.close_at != 0.0 && self.now >= sess.close_at {
            return BuyerRequest::Done; // the sale window is over
        }
        let buyers = sess.buyers.as_ref().unwrap();
        let bought = buyers.bought[index] as i64;
        let quota = sess.per_buyer_limit - bought;
        if quota <= 0 {
            self.metrics.buyer_capped += 1; // hit the purchase limit, stop
            return BuyerRequest::Done;
        }
        let desired = buyers.crowd.desired[index] as i64 - bought;
        if desired <= 0 {
            return BuyerRequest::Done;
        }
        if sess.remaining_total == 0 {
            return BuyerRequest::Empty; // nothing left at all
        }
        // the hot buyers take the most popular seats, the others pick at random
        let picked = if buyers.crowd.hot[index] {
            sess.pick_hot_cell()
        } else {
            let buyers = sess.buyers.as_ref().unwrap();
            sess.pick_random_cell(
                &buyers.crowd.key_prefix,
                buyer as u64 + 1,
                buyers.attempts[index] as u64,
                &mut self.scratch,
            )
        };
        let Some(cell) = picked else {
            return BuyerRequest::Empty;
        };
        let observed = sess.observable(cell);
        let mut qty = if desired < quota { desired } else { quota };
        if observed < qty {
            qty = observed;
        }
        if qty <= 0 {
            return BuyerRequest::Empty;
        }
        BuyerRequest::Ok {
            cell,
            qty,
            observed,
        }
    }

    /// A buyer that cannot buy waits until the session has tickets again.
    fn park(&mut self, host: &Host<'_>, session: u32, buyer: u32) -> PyResult<()> {
        self.sessions[session as usize]
            .buyers
            .as_mut()
            .unwrap()
            .parked
            .push_back(buyer);
        self.metrics.buyer_parked += 1;
        if self.trace_buyers {
            trace!(self, host, "buyer_park",
                   "session" => self.session_key(host.py, session), "buyer" => buyer);
        }
        Ok(())
    }

    /// Wake some of the waiting buyers when tickets come back (a budget per
    /// returned ticket, not the whole crowd).
    fn maybe_wake(&mut self, host: &Host<'_>, session: u32, released: i64) -> PyResult<()> {
        let sess = &mut self.sessions[session as usize];
        let Some(buyers) = sess.buyers.as_mut() else {
            return Ok(());
        };
        if buyers.parked.is_empty() {
            return Ok(());
        }
        if sess.remaining_total <= 0 {
            return Ok(()); // still nothing anybody could buy
        }
        let budget = ((released as f64 * self.wake_budget_per_ticket) as i64).max(1);
        let count = (budget as usize).min(buyers.parked.len());
        let waiting: Vec<u32> = buyers.parked.drain(..count).collect();
        for buyer in waiting {
            self.push(
                self.now,
                PRIO_BUYER_OBSERVE,
                Event::BuyerObserve { session, buyer },
            );
        }
        self.metrics.buyer_wakes += count as i64;
        if self.trace_buyers {
            trace!(self, host, "buyer_wake",
                   "session" => self.session_key(host.py, session), "count" => count,
                   "released" => released);
        }
        Ok(())
    }

    /// Keep the pool trajectory (the visualisation and the tests use it).
    fn record_pool(&mut self, session: u32) {
        let remaining = self.sessions[session as usize].remaining_total;
        self.pool_history.push((self.now, session, remaining));
    }

    /// The platform grants (or refuses) the buyer's tickets.
    fn on_buyer_grant(
        &mut self,
        host: &Host<'_>,
        session: u32,
        buyer: u32,
        cell: u32,
        qty: i64,
        observed: i64,
    ) -> PyResult<()> {
        let at = session as usize;
        let index = buyer as usize;
        let left = self.sessions[at].remaining[cell as usize];
        let ok = self.sessions[at].sell(cell, qty);
        if self.trace_buyers {
            trace!(self, host, "buyer_grant",
                   "session" => self.session_key(host.py, session), "buyer" => index,
                   "cell" => self.cell_key(session, cell), "qty" => qty,
                   "observed" => observed, "left" => left, "ok" => ok);
        }
        if !ok {
            let sess = &mut self.sessions[at];
            sess.buyer_failures += 1;
            self.metrics.buyer_failures += 1;
            if left < qty {
                sess.cell_short_failures += 1;
                self.metrics.buyer_cell_short_failures += 1;
            }
            if observed > left {
                sess.stale_failures += 1;
                self.metrics.buyer_stale_failures += 1;
            }
            // a buyer that lost looks again at the end of this timestamp
            self.metrics.buyer_retries += 1;
            if self.pending_picks.is_empty() {
                self.pending_time = self.now;
            }
            self.pending_picks.push((session, buyer));
            return Ok(());
        }
        self.sessions[at].buyer_successes += 1;
        self.metrics.buyer_successes += 1;
        self.metrics.buyer_orders += 1;
        self.metrics.tickets_sold += qty;
        let buyers = self.sessions[at].buyers.as_mut().unwrap();
        buyers.bought[index] += qty as i32;
        // the buyer's open order in this session, if it has one
        let open = match buyers.order[index] {
            0 => None,
            id if self.orders[id as usize - 1].state == OrderState::Pending => Some(id),
            _ => None,
        };
        match open {
            None => {
                let id = self.new_order(session, qty, buyer);
                let order = &mut self.orders[id as usize - 1];
                order.seats.push((cell, qty));
                order.requests = 1;
                self.sessions[at].buyers.as_mut().unwrap().order[index] = id;
                self.schedule_settle(id);
            }
            Some(id) => {
                let order = &mut self.orders[id as usize - 1];
                order.add_seats(cell, qty);
                order.qty += qty;
                order.requests += 1;
            }
        }
        self.record_pool(session);
        // the buyer tops the order up when it is short of what it wanted
        let sess = &self.sessions[at];
        let buyers = sess.buyers.as_ref().unwrap();
        let bought = buyers.bought[index] as i64;
        if bought < buyers.crowd.desired[index] as i64 && bought < sess.per_buyer_limit {
            let retries = (buyers.attempts[index] as i64 - 1).max(0) as u64;
            let delay = buyers.crowd.next_delay(index, retries, &mut self.scratch);
            self.push(
                self.now + delay,
                PRIO_BUYER_OBSERVE,
                Event::BuyerObserve { session, buyer },
            );
        }
        Ok(())
    }

    // ----------------------------------------------------------------------------------
    // orders
    // ----------------------------------------------------------------------------------
    /// Open an order; returns its id (ids start at 1).
    fn new_order(&mut self, session: u32, qty: i64, buyer: u32) -> u32 {
        self.orders.push(Order {
            session,
            qty,
            placed_at: self.now,
            state: OrderState::Pending,
            buyer,
            settle_at: None,
            seats: Vec::new(),
            requests: 0,
        });
        let sess = &mut self.sessions[session as usize];
        if sess.remaining_total == 0 && sess.sold_out_at.is_none() {
            sess.sold_out_at = Some(self.now);
        }
        self.orders.len() as u32
    }

    /// `"{seed}:order:b:{session}:{buyer+1}"` - the prefix of every draw of one order.
    fn order_prefix(&self, id: u32) -> Vec<u8> {
        let order = &self.orders[id as usize - 1];
        let session = &self.sessions[order.session as usize].key;
        format!(
            "{}:order:b:{session}:{}",
            self.seed_text,
            order.buyer as u64 + 1
        )
        .into_bytes()
    }

    fn schedule_settle(&mut self, id: u32) {
        let settle_at = self.now + self.settle_delay(id);
        self.orders[id as usize - 1].settle_at = Some(settle_at);
        self.push(settle_at, PRIO_SETTLE, Event::Settle(id));
    }

    /// When the buyer decides what to do with the order (its own time law).
    fn settle_delay(&mut self, id: u32) -> f64 {
        let mut prefix = self.order_prefix(id);
        prefix.extend_from_slice(b":settle");
        let scratch = &mut self.scratch;
        if self.settle_uniform {
            let (low, high) = (self.settle_early_range.0, self.settle_late_range.1);
            return low + (high - low) * rng::stream_uniform(&prefix, 0, scratch);
        }
        let (low, high) = if rng::stream_uniform(&prefix, 0, scratch) < self.settle_early_frac {
            self.settle_early_range
        } else {
            self.settle_late_range
        };
        low + (high - low) * rng::stream_uniform(&prefix, 1, scratch)
    }

    fn order_draw(&self, id: u32, name: &[u8]) -> f64 {
        let mut key = self.order_prefix(id);
        key.extend_from_slice(name);
        rng::hash_uniform(&key)
    }

    /// The buyer's order slot is freed when its order leaves PENDING.
    fn forget_buyer_order(&mut self, id: u32) {
        let order = &self.orders[id as usize - 1];
        if let Some(buyers) = self.sessions[order.session as usize].buyers.as_mut() {
            buyers.order[order.buyer as usize] = 0;
        }
    }

    /// The buyer's decision: cancel (refund), never pay, or pay.
    fn on_settle(&mut self, host: &Host<'_>, id: u32) -> PyResult<()> {
        let order = &self.orders[id as usize - 1];
        if order.state != OrderState::Pending {
            return Ok(());
        }
        let session = order.session;
        let at = session as usize;
        let qty = order.qty;
        let refund = self.order_draw(id, b":refund") < self.refund_prob;
        let unpaid = !refund && self.order_draw(id, b":unpaid") < self.clear_prob;
        if refund {
            for (cell, seats) in self.orders[id as usize - 1].seats.clone() {
                self.sessions[at].return_seats(cell, seats);
            }
            self.orders[id as usize - 1].state = OrderState::Closed;
            self.sessions[at].refunded_tickets += qty;
            self.metrics.refunded_tickets += qty;
            self.record_pool(session);
            self.maybe_wake(host, session, qty)?;
            self.forget_buyer_order(id);
            trace!(self, host, "refund",
                   "session" => self.session_key(host.py, session), "qty" => qty);
            return Ok(());
        }
        if unpaid {
            // never paid: the order is cancelled when the payment window ends
            // (counted from the moment the order was placed)
            let expires = self.orders[id as usize - 1].placed_at + self.payment_window;
            self.push(expires, PRIO_SETTLE, Event::Expire(id));
            trace!(self, host, "unpaid",
                   "session" => self.session_key(host.py, session), "qty" => qty);
            return Ok(());
        }
        for (cell, seats) in self.orders[id as usize - 1].seats.clone() {
            self.sessions[at].checkout(cell, seats);
        }
        self.orders[id as usize - 1].state = OrderState::Paid;
        self.forget_buyer_order(id);
        trace!(self, host, "checkout",
               "session" => self.session_key(host.py, session), "qty" => qty);
        Ok(())
    }

    /// The payment window ran out: the order is cancelled, its seats wait in
    /// limbo for the next batch.
    fn on_expire(&mut self, host: &Host<'_>, id: u32) -> PyResult<()> {
        let order = &mut self.orders[id as usize - 1];
        if order.state != OrderState::Pending {
            return Ok(());
        }
        order.state = OrderState::Closed;
        let session = order.session;
        let qty = order.qty;
        self.sessions[session as usize].limbo.push(id);
        self.forget_buyer_order(id);
        trace!(self, host, "expired",
               "session" => self.session_key(host.py, session), "qty" => qty);
        Ok(())
    }

    /// A batch: the seats of the expired orders go back on sale together.
    /// Paid orders are never touched.
    fn on_clear(&mut self, host: &Host<'_>, session: u32, cycle: u32) -> PyResult<()> {
        let at = session as usize;
        if !self.sessions[at].limbo.is_empty() {
            let limbo = std::mem::take(&mut self.sessions[at].limbo);
            let mut released = 0i64;
            for id in limbo {
                for (cell, seats) in self.orders[id as usize - 1].seats.clone() {
                    self.sessions[at].return_seats(cell, seats);
                    released += seats;
                }
            }
            self.sessions[at].cleared_tickets += released;
            self.metrics.cleared_tickets += released;
            self.record_pool(session);
            self.maybe_wake(host, session, released)?;
            trace!(self, host, "cleared",
                   "session" => self.session_key(host.py, session), "qty" => released);
        }
        let next_clear = self.sessions[at].open_at + (cycle as f64 + 1.0) * self.clear_period;
        if next_clear <= self.horizon {
            self.push(
                next_clear,
                PRIO_CLEAR,
                Event::Clear {
                    session,
                    cycle: cycle + 1,
                },
            );
        }
        Ok(())
    }

    // ----------------------------------------------------------------------------------
    // snapshots for the Python side
    // ----------------------------------------------------------------------------------
    /// Tickets of the orders of a session that still exist (pending or paid).
    pub fn held_tickets(&self, session: u32) -> i64 {
        self.orders
            .iter()
            .filter(|o| o.session == session && o.state != OrderState::Closed)
            .map(|o| o.qty)
            .sum()
    }

    /// The counters, under the field names of `ticketsim.metrics.Metrics`.
    pub fn metrics_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let m = &self.metrics;
        let out = PyDict::new(py);
        let sold_out = PyDict::new(py);
        let mut order: Vec<&Session> = self
            .sessions
            .iter()
            .filter(|s| s.sold_out_at.is_some())
            .collect();
        order.sort_by(|a, b| {
            a.sold_out_at
                .partial_cmp(&b.sold_out_at)
                .unwrap_or(Ordering::Equal)
        });
        for session in order {
            sold_out.set_item(&session.key, session.sold_out_at)?;
        }
        out.set_item("sold_out_at", sold_out)?;
        macro_rules! copy {
            ($($field:ident),* $(,)?) => { $( out.set_item(stringify!($field), m.$field)?; )* };
        }
        copy!(
            refunded_tickets,
            cleared_tickets,
            tickets_sold,
            buyer_attempts,
            buyer_successes,
            buyer_failures,
            buyer_stale_failures,
            buyer_cell_short_failures,
            buyer_retries,
            buyer_orders,
            buyer_parked,
            buyer_wakes,
            buyer_capped,
            buyer_fingerprint,
            buyer_inventory_reads,
            rule_violations,
        );
        Ok(out)
    }

    /// The pool, the statistics and the waiting seats of one session.
    pub fn session_dict<'py>(&self, py: Python<'py>, index: usize) -> PyResult<Bound<'py, PyDict>> {
        let s = &self.sessions[index];
        let out = PyDict::new(py);
        let per_cell = |values: &[i64]| -> PyResult<Bound<'py, PyDict>> {
            let dict = PyDict::new(py);
            for (cell, value) in s.cells.iter().zip(values) {
                dict.set_item(cell.key, *value)?;
            }
            Ok(dict)
        };
        let capacity: Vec<i64> = s.cells.iter().map(|c| c.capacity).collect();
        out.set_item("capacity", per_cell(&capacity)?)?;
        out.set_item("remaining", per_cell(&s.remaining)?)?;
        out.set_item("pending", per_cell(&s.pending)?)?;
        out.set_item("paid", per_cell(&s.paid)?)?;
        out.set_item("total", s.total)?;
        out.set_item("remaining_total", s.remaining_total)?;
        out.set_item("pending_total", s.pending_total)?;
        out.set_item("paid_total", s.paid_total)?;
        out.set_item("pool_ok", s.pool_ok())?;
        out.set_item("sold_out_at", s.sold_out_at)?;
        out.set_item("held", self.held_tickets(index as u32))?;
        let limbo = PyDict::new(py);
        for &id in &s.limbo {
            let seats = PyDict::new(py);
            for (cell, qty) in &self.orders[id as usize - 1].seats {
                seats.set_item(s.cells[*cell as usize].key, *qty)?;
            }
            limbo.set_item(id, seats)?;
        }
        out.set_item("limbo", limbo)?;
        macro_rules! copy {
            ($($field:ident),* $(,)?) => { $( out.set_item(stringify!($field), s.$field)?; )* };
        }
        copy!(
            buyer_attempts,
            buyer_successes,
            buyer_failures,
            stale_failures,
            cell_short_failures,
            refunded_tickets,
            cleared_tickets,
        );
        Ok(out)
    }

    /// One total of one session's pool - cheap enough to sample while running.
    pub fn remaining_total(&self, index: usize) -> i64 {
        self.sessions[index].remaining_total
    }

    /// The orders of one session, as the arguments of `ticketsim.order.Order`:
    /// `(id, qty, placed_at, state, settle_at, key, seats, requests, buyer_index)`.
    pub fn orders_list<'py>(&self, py: Python<'py>, index: usize) -> PyResult<Bound<'py, PyList>> {
        let s = &self.sessions[index];
        let out = PyList::empty(py);
        for (position, order) in self.orders.iter().enumerate() {
            if order.session as usize != index {
                continue;
            }
            let seats = PyDict::new(py);
            for (cell, qty) in &order.seats {
                seats.set_item(s.cells[*cell as usize].key, *qty)?;
            }
            out.append((
                position + 1,
                order.qty,
                order.placed_at,
                order.state.name(),
                order.settle_at,
                format!("b:{}:{}", s.key, order.buyer as u64 + 1),
                seats,
                order.requests,
                order.buyer,
            ))?;
        }
        Ok(out)
    }

    /// The pool trajectory: `(round(t, 4), session, remaining)` after every change.
    pub fn pool_history_list<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(py);
        for &(time, session, remaining) in &self.pool_history {
            out.append((py_round(time, 4), self.session_key(py, session), remaining))?;
        }
        Ok(out)
    }

    /// Tickets every buyer of a session bought (`None` before the sale opens).
    pub fn buyer_bought(&self, index: usize) -> Option<Vec<i32>> {
        self.sessions[index]
            .buyers
            .as_ref()
            .map(|b| b.bought.clone())
    }
}
