//! Sessions, cell pools and orders (`ticketsim/session.py`, `pool.py`, `order.py`;
//! docs/MODEL.md).
//!
//! The pool of `pool.py` is folded into the session (one struct, one invariant).
//! Cells are addressed by their position in the session's cell list; the
//! `(area, ticket_type)` key only exists at the Python boundary.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use crate::crowd::Crowd;
use crate::rng;

pub type CellKey = (i64, i64);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OrderState {
    Pending,
    Paid,
    Closed,
}

impl OrderState {
    pub fn name(self) -> &'static str {
        match self {
            OrderState::Pending => "PENDING",
            OrderState::Paid => "PAID",
            OrderState::Closed => "CLOSED",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Order {
    pub session: u32,
    pub qty: i64,
    pub placed_at: f64,
    pub state: OrderState,
    /// the buyer who placed it (index into the session's crowd) - also the
    /// identity its random draws are keyed by: `b:<session>:<index+1>`
    pub buyer: u32,
    pub settle_at: Option<f64>,
    /// tickets per cell, in the order the cells were first bought from
    pub seats: Vec<(u32, i64)>,
    /// how many requests this order is made of
    pub requests: i64,
}

impl Order {
    pub fn add_seats(&mut self, cell: u32, qty: i64) {
        match self.seats.iter_mut().find(|(c, _)| *c == cell) {
            Some(entry) => entry.1 += qty,
            None => self.seats.push((cell, qty)),
        }
    }
}

/// One cell as resolved by `schema.resolve_cells`.
#[derive(Clone, Debug)]
pub struct CellDef {
    pub key: CellKey,
    pub label: String,
    pub capacity: i64,
    pub heat: f64,
}

/// The buyers of a session once its sale is open.
pub struct Buyers {
    pub crowd: Arc<Crowd>,
    /// buyers waiting for tickets to come back (FIFO)
    pub parked: VecDeque<u32>,
    /// attempts so far, per buyer
    pub attempts: Vec<i32>,
    /// tickets bought in this session, per buyer
    pub bought: Vec<i32>,
    /// the buyer's order id (0 = none)
    pub order: Vec<u32>,
}

pub struct Session {
    pub key: String,
    pub per_buyer_limit: i64,
    pub open_at: f64,
    pub close_at: f64,
    /// buyers of this session before the count jitter
    pub buyer_count: i64,
    pub cells: Vec<CellDef>,
    pub cell_index: HashMap<CellKey, u32>,

    // -- the pool (capacity == remaining + pending + paid, cell by cell) --
    pub remaining: Vec<i64>,
    pub pending: Vec<i64>,
    pub paid: Vec<i64>,
    pub total: i64,
    pub remaining_total: i64,
    pub pending_total: i64,
    pub paid_total: i64,

    // -- picking a cell --
    /// the cells that still have tickets, in the order they (re)appeared
    pub alive: Vec<u32>,
    alive_flag: Vec<bool>,
    /// cells by popularity (hottest first)
    hot_order: Vec<u32>,
    hot_rank: Vec<usize>,
    hot_ptr: usize,

    /// expired orders whose seats wait for the next batch (order ids)
    pub limbo: Vec<u32>,
    pub sold_out_at: Option<f64>,
    pub buyers: Option<Buyers>,

    // -- statistics --
    pub buyer_attempts: i64,
    pub buyer_successes: i64,
    pub buyer_failures: i64,
    pub stale_failures: i64,
    pub cell_short_failures: i64,
    pub refunded_tickets: i64,
    pub cleared_tickets: i64,
}

impl Session {
    pub fn new(
        key: String,
        per_buyer_limit: i64,
        open_at: f64,
        close_at: f64,
        buyer_count: i64,
        cells: Vec<CellDef>,
    ) -> Session {
        let count = cells.len();
        let capacity: Vec<i64> = cells.iter().map(|c| c.capacity).collect();
        let total: i64 = capacity.iter().sum();
        let mut cell_index = HashMap::with_capacity(count);
        for (index, cell) in cells.iter().enumerate() {
            cell_index.insert(cell.key, index as u32);
        }
        // sorted(cells, key=(-heat, capacity, key[0])) - a stable sort
        let mut hot_order: Vec<u32> = (0..count as u32).collect();
        hot_order.sort_by(|&a, &b| {
            let (ca, cb) = (&cells[a as usize], &cells[b as usize]);
            (-ca.heat)
                .partial_cmp(&(-cb.heat))
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(ca.capacity.cmp(&cb.capacity))
                .then(ca.key.0.cmp(&cb.key.0))
        });
        let mut hot_rank = vec![0usize; count];
        for (rank, &cell) in hot_order.iter().enumerate() {
            hot_rank[cell as usize] = rank;
        }
        let alive: Vec<u32> = (0..count as u32)
            .filter(|&c| capacity[c as usize] > 0)
            .collect();
        let alive_flag: Vec<bool> = capacity.iter().map(|&c| c > 0).collect();
        Session {
            key,
            per_buyer_limit,
            open_at,
            close_at,
            buyer_count,
            cell_index,
            remaining: capacity,
            pending: vec![0; count],
            paid: vec![0; count],
            total,
            remaining_total: total,
            pending_total: 0,
            paid_total: 0,
            alive,
            alive_flag,
            hot_order,
            hot_rank,
            hot_ptr: 0,
            limbo: Vec::new(),
            sold_out_at: None,
            buyers: None,
            buyer_attempts: 0,
            buyer_successes: 0,
            buyer_failures: 0,
            stale_failures: 0,
            cell_short_failures: 0,
            refunded_tickets: 0,
            cleared_tickets: 0,
            cells,
        }
    }

    pub fn is_open(&self, now: f64) -> bool {
        now >= self.open_at && (self.close_at == 0.0 || now < self.close_at)
    }

    /// What the area page shows for one cell: the exact number while the cell
    /// is small, the purchase limit otherwise.
    #[inline]
    pub fn observable(&self, cell: u32) -> i64 {
        self.remaining[cell as usize].min(self.per_buyer_limit)
    }

    /// Take `qty` tickets of one cell, all or nothing (no partial fills).
    pub fn sell(&mut self, cell: u32, qty: i64) -> bool {
        let at = cell as usize;
        if qty <= 0 || self.remaining[at] < qty {
            return false;
        }
        self.remaining[at] -= qty;
        self.pending[at] += qty;
        self.remaining_total -= qty;
        self.pending_total += qty;
        if self.remaining[at] <= 0 && self.alive_flag[at] {
            self.alive_flag[at] = false;
            let position = self.alive.iter().position(|&c| c == cell).unwrap();
            self.alive.remove(position);
        }
        true
    }

    /// A pending order was paid for: the seats stay with the buyer.
    pub fn checkout(&mut self, cell: u32, qty: i64) {
        let at = cell as usize;
        self.pending[at] -= qty;
        self.paid[at] += qty;
        self.pending_total -= qty;
        self.paid_total += qty;
    }

    /// Pending seats return to the cell they came from (refund or clearing).
    pub fn return_seats(&mut self, cell: u32, qty: i64) {
        let at = cell as usize;
        self.pending[at] -= qty;
        self.pending_total -= qty;
        self.remaining[at] += qty;
        self.remaining_total += qty;
        if !self.alive_flag[at] && self.remaining[at] > 0 {
            self.alive_flag[at] = true;
            self.alive.push(cell);
            let rank = self.hot_rank[at];
            if rank < self.hot_ptr {
                self.hot_ptr = rank; // a hot cell is available again
            }
        }
    }

    /// The most popular cell that still has tickets.
    pub fn pick_hot_cell(&mut self) -> Option<u32> {
        while self.hot_ptr < self.hot_order.len() {
            let cell = self.hot_order[self.hot_ptr];
            if self.remaining[cell as usize] > 0 {
                return Some(cell);
            }
            self.hot_ptr += 1;
        }
        None
    }

    /// A uniformly random cell that still has tickets, keyed by the buyer and
    /// the attempt (`b:<session>:<buyer>:cellpick:<attempt>`).
    pub fn pick_random_cell(
        &self,
        key_prefix: &[u8],
        buyer: u64,
        attempt: u64,
        scratch: &mut Vec<u8>,
    ) -> Option<u32> {
        if self.alive.is_empty() {
            return None;
        }
        scratch.clear();
        scratch.extend_from_slice(key_prefix);
        rng::push_int(scratch, buyer);
        scratch.extend_from_slice(b":cellpick:");
        rng::push_int(scratch, attempt);
        let u = rng::hash_uniform(scratch);
        Some(self.alive[(u * self.alive.len() as f64) as usize])
    }

    /// The conservation invariant (`pool.py :: check`).
    pub fn pool_ok(&self) -> bool {
        if self.remaining_total < 0 || self.pending_total < 0 || self.paid_total < 0 {
            return false;
        }
        let capacity: i64 = self.cells.iter().map(|c| c.capacity).sum();
        capacity == self.total
            && self.remaining.iter().sum::<i64>() == self.remaining_total
            && self.pending.iter().sum::<i64>() == self.pending_total
            && self.paid.iter().sum::<i64>() == self.paid_total
            && self.total == self.remaining_total + self.pending_total + self.paid_total
    }
}
