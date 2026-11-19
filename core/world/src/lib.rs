//! The world model of ticketsim, bit for bit the same as the Python reference
//! (`ticketsim/rng.py`, `crowd.py`, `session.py`, `pool.py`, `order.py`).
//!
//! No Python here: the event engine and the Python boundary live in the
//! `ticketsim-core` crate (`../ext`).

pub mod crowd;
pub mod rng;
pub mod session;
