//! `ticketsim_core` - the world core of ticketsim as a Python extension module.
//!
//! The world (buyers, orders, settlement, batches of returned seats) runs
//! here; the configuration, the summary and every interface stay in Python
//! (docs/ENGINEERING.md).  The boundary is in-process (PyO3) and is crossed
//! once per `advance()` call, plus once per traced event when somebody
//! consumes the trace.
//!
//! The Python side of the boundary is `ticketsim/rust_engine.py`.

mod engine;

use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::engine::{Clock, CrowdHandle, Engine};
use ticketsim_world::crowd::{Crowd, CrowdSpec, Tier};
use ticketsim_world::rng;

/// One run of one scenario.
#[pyclass(module = "ticketsim.ticketsim_core")]
struct Core {
    engine: Engine,
}

#[pymethods]
impl Core {
    /// `params` is the flat parameter dict built by `rust_engine._core_params`.
    #[new]
    fn new(py: Python<'_>, params: &Bound<'_, PyDict>) -> PyResult<Self> {
        Ok(Core {
            engine: Engine::new(py, params)?,
        })
    }

    /// Push the opening events (idempotent).
    fn start(&mut self) {
        self.engine.start();
    }

    /// Process at most `max_events` events; `True` when the run is over.
    /// `host` provides the trace, the observers and the crowd cache (see `engine.rs`).
    fn advance(&mut self, host: &Bound<'_, PyAny>, max_events: u64) -> PyResult<bool> {
        self.engine.advance(host, max_events)
    }

    /// The simulation clock (readable at any moment, also from a callback).
    #[getter]
    fn clock(&self, py: Python<'_>) -> Py<Clock> {
        self.engine.clock(py)
    }

    #[getter]
    fn events_processed(&self) -> u64 {
        self.engine.events_processed
    }

    #[getter]
    fn done(&self) -> bool {
        self.engine.is_done()
    }

    /// The counters, under the field names of `ticketsim.metrics.Metrics`.
    fn metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.engine.metrics_dict(py)
    }

    /// Pool, statistics and limbo of one session.
    fn session<'py>(&self, py: Python<'py>, key: &str) -> PyResult<Bound<'py, PyDict>> {
        self.engine
            .session_dict(py, self.engine.session_position(key)?)
    }

    /// Tickets still on sale in one session.
    fn remaining_total(&self, key: &str) -> PyResult<i64> {
        Ok(self
            .engine
            .remaining_total(self.engine.session_position(key)?))
    }

    /// The orders of one session (the arguments of `ticketsim.order.Order`).
    fn orders<'py>(&self, py: Python<'py>, key: &str) -> PyResult<Bound<'py, PyList>> {
        self.engine
            .orders_list(py, self.engine.session_position(key)?)
    }

    /// `(round(t, 4), session, remaining)` after every change of a pool.
    fn pool_history<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        self.engine.pool_history_list(py)
    }

    /// Tickets bought by every buyer of one session (`None` before its sale opens).
    fn buyer_bought(&self, key: &str) -> PyResult<Option<Vec<i32>>> {
        Ok(self.engine.buyer_bought(self.engine.session_position(key)?))
    }
}

/// Build the crowd of one session (`crowd.py :: _draws`), for tests and tools.
#[pyfunction]
#[pyo3(signature = (seed, session, count, buyer_mix, qty_weights, count_jitter, hot_buyer_frac, max_qty))]
#[allow(clippy::too_many_arguments)]
fn build_crowd(
    seed: String,
    session: String,
    count: i64,
    buyer_mix: Vec<(String, f64, f64, f64, f64)>,
    qty_weights: Vec<f64>,
    count_jitter: f64,
    hot_buyer_frac: f64,
    max_qty: i64,
) -> PyResult<CrowdHandle> {
    let spec = CrowdSpec {
        seed_text: seed,
        session,
        count,
        mix: buyer_mix
            .into_iter()
            .map(|(name, share, median, spread, floor)| Tier {
                name,
                share,
                median,
                spread,
                floor,
            })
            .collect(),
        qty_weights,
        count_jitter,
        hot_buyer_frac,
        max_qty,
    };
    let crowd = Crowd::build(&spec).map_err(PyValueError::new_err)?;
    Ok(CrowdHandle {
        crowd: Arc::new(crowd),
    })
}

/// `int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "big")`
#[pyfunction]
fn hash_u64(data: &[u8]) -> u64 {
    rng::hash_u64(data)
}

/// `round(x, ndigits)` as the engine computes it (must equal Python's).
#[pyfunction]
fn py_round(x: f64, ndigits: usize) -> f64 {
    engine::py_round(x, ndigits)
}

#[pymodule]
fn ticketsim_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Core>()?;
    module.add_class::<Clock>()?;
    module.add_class::<CrowdHandle>()?;
    module.add_function(wrap_pyfunction!(build_crowd, module)?)?;
    module.add_function(wrap_pyfunction!(hash_u64, module)?)?;
    module.add_function(wrap_pyfunction!(py_round, module)?)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
