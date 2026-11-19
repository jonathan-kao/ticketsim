//! The crowd of buyers of one session (`ticketsim/crowd.py`, docs/MODEL.md).
//!
//! Parallel arrays, drawn in arrival order from the two Mersenne Twister
//! channels `buyers:<session>` and `qty:<session>` - the same draws, in the
//! same order, as `crowd.py :: _draws`.  A crowd only depends on the seed and
//! the crowd parameters, so runs that share them share one crowd (`Arc`).

use crate::rng::{self, PyRandom};

/// One speed tier of `model_constants.buyer_mix`: (name, share, median, sigma, floor).
#[derive(Clone, Debug)]
pub struct Tier {
    pub name: String,
    pub share: f64,
    pub median: f64,
    pub spread: f64,
    pub floor: f64,
}

/// Everything a crowd is a function of.
#[derive(Clone, Debug)]
pub struct CrowdSpec {
    /// `str(seed)`
    pub seed_text: String,
    pub session: String,
    /// buyers before the count jitter
    pub count: i64,
    pub mix: Vec<Tier>,
    pub qty_weights: Vec<f64>,
    pub count_jitter: f64,
    pub hot_buyer_frac: f64,
    /// the session's purchase limit (an order cannot exceed it)
    pub max_qty: i64,
}

#[derive(Debug)]
pub struct Crowd {
    /// how many tickets the buyer wants
    pub desired: Vec<i32>,
    /// delay from the opening observation to the first order
    pub first_delay: Vec<f64>,
    /// "best seats first" buyers (keyed by the buyer, not by the draw order)
    pub hot: Vec<bool>,
    tier: Vec<u8>,
    tier_median: Vec<f64>,
    tier_spread: Vec<f64>,
    tier_floor: Vec<f64>,
    /// `"{seed}:b:{session}:"` - the constant part of every buyer key
    pub key_prefix: Vec<u8>,
}

impl Crowd {
    pub fn build(spec: &CrowdSpec) -> Result<Crowd, String> {
        if spec.mix.is_empty() {
            return Err("buyer_mix is empty".to_string());
        }
        if spec.mix.len() > u8::MAX as usize {
            return Err("buyer_mix has too many tiers".to_string());
        }
        let key_prefix = format!("{}:b:{}:", spec.seed_text, spec.session).into_bytes();
        let mut rng_buyers = PyRandom::new(rng::channel_seed(
            &spec.seed_text,
            &format!("buyers:{}", spec.session),
        ));
        let mut rng_qty = PyRandom::new(rng::channel_seed(
            &spec.seed_text,
            &format!("qty:{}", spec.session),
        ));

        // the weights are truncated at the purchase limit and renormalised
        let keep = spec.max_qty.min(spec.qty_weights.len() as i64).max(1) as usize;
        let weights = &spec.qty_weights[..keep.min(spec.qty_weights.len())];
        let mut weight_total = 0.0;
        for weight in weights {
            weight_total += weight;
        }
        if weight_total == 0.0 {
            weight_total = 1.0;
        }

        let total = if spec.count != 0 {
            let jitter = spec.count_jitter;
            let scaled = spec.count as f64 * (1.0 + rng_buyers.uniform(-jitter, jitter));
            (scaled.round_ties_even() as i64).max(0) as usize
        } else {
            0
        };

        // SyntheticCrowd keeps the index of the first tier with the same name.
        let tier_index: Vec<u8> = spec
            .mix
            .iter()
            .map(|tier| spec.mix.iter().position(|t| t.name == tier.name).unwrap() as u8)
            .collect();

        let mut crowd = Crowd {
            desired: Vec::with_capacity(total),
            first_delay: Vec::with_capacity(total),
            hot: Vec::with_capacity(total),
            tier: Vec::with_capacity(total),
            tier_median: spec.mix.iter().map(|t| t.median).collect(),
            tier_spread: spec.mix.iter().map(|t| t.spread).collect(),
            tier_floor: spec.mix.iter().map(|t| t.floor).collect(),
            key_prefix,
        };
        let mut key = Vec::with_capacity(crowd.key_prefix.len() + 32);
        for index in 1..=total {
            let u = rng_buyers.random();
            let mut acc = 0.0;
            let mut chosen = spec.mix.len() - 1;
            for (position, tier) in spec.mix.iter().enumerate() {
                acc += tier.share;
                if u <= acc {
                    chosen = position;
                    break;
                }
            }
            let tier = &spec.mix[chosen];
            let raw = tier.median * rng::exp(rng_buyers.gauss() * tier.spread);
            let delay = if raw > tier.floor { raw } else { tier.floor };

            let u = rng_qty.random() * weight_total;
            let mut acc = 0.0;
            let mut desired = weights.len();
            for (position, weight) in weights.iter().enumerate() {
                acc += weight;
                if u <= acc {
                    desired = position + 1;
                    break;
                }
            }

            key.clear();
            key.extend_from_slice(&crowd.key_prefix);
            rng::push_int(&mut key, index as u64);
            key.extend_from_slice(b":hot");
            crowd
                .hot
                .push(rng::hash_uniform(&key) < spec.hot_buyer_frac);
            crowd.tier.push(tier_index[chosen]);
            crowd.desired.push(desired as i32);
            crowd.first_delay.push(delay);
        }
        Ok(crowd)
    }

    pub fn len(&self) -> usize {
        self.desired.len()
    }

    pub fn is_empty(&self) -> bool {
        self.desired.is_empty()
    }

    /// The delay before attempt number `retries + 2` (`crowd.py :: _draw_delay`):
    /// a fresh log-normal draw keyed by the buyer and the retry number.
    pub fn next_delay(&self, index: usize, retries: u64, scratch: &mut Vec<u8>) -> f64 {
        let tier = self.tier[index] as usize;
        scratch.clear();
        scratch.extend_from_slice(&self.key_prefix);
        rng::push_int(scratch, index as u64 + 1);
        scratch.extend_from_slice(b":submitdelay:");
        rng::push_int(scratch, retries);
        scratch.extend_from_slice(b":0");
        let first = rng::hash_uniform(scratch);
        let u1 = if first > 1e-12 { first } else { 1e-12 };
        let last = scratch.len() - 1;
        scratch[last] = b'1';
        let u2 = rng::hash_uniform(scratch);
        let z = rng::box_muller(u1, u2);
        let raw = self.tier_median[tier] * rng::exp(z * self.tier_spread[tier]);
        let floor = self.tier_floor[tier];
        if raw > floor { raw } else { floor }
    }
}
