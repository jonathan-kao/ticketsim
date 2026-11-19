//! Randomness, bit for bit the same as `ticketsim/rng.py` (docs/ENGINEERING.md).
//!
//! * keyed hashes: `blake2b(key, digest_size=8)` read as a big-endian integer,
//!   divided by 2^64 (`hash_uniform`, `HashStream`);
//! * the two crowd channels: CPython's `random.Random` (MT19937, `random()`,
//!   `uniform()`, `gauss()` with its cached second value).
//!
//! The transcendental functions come from the platform's libm - the very
//! symbols CPython's `math` module calls - so a run reproduces the Python
//! numbers exactly on the same platform.

use std::hint::black_box;

mod libm {
    unsafe extern "C" {
        pub safe fn exp(x: f64) -> f64;
        pub safe fn log(x: f64) -> f64;
        pub safe fn cos(x: f64) -> f64;
        pub safe fn sin(x: f64) -> f64;
    }
}

/// `math.exp`
#[inline]
pub fn exp(x: f64) -> f64 {
    libm::exp(x)
}

/// `math.log` (natural logarithm of a positive float)
#[inline]
pub fn ln(x: f64) -> f64 {
    libm::log(x)
}

/// `math.cos`
#[inline]
pub fn cos(x: f64) -> f64 {
    libm::cos(x)
}

/// `math.sin`.  The argument is hidden from the optimiser so that a `sin` and
/// a `cos` of the same value are never merged into one `sincos` call (CPython
/// calls them separately; the merged routine is not guaranteed to round alike).
#[inline]
pub fn sin(x: f64) -> f64 {
    libm::sin(black_box(x))
}

pub const TWO_POW_64: f64 = 18446744073709551616.0;
pub const TWO_PI: f64 = 2.0 * std::f64::consts::PI;

// --------------------------------------------------------------------------------------
// BLAKE2b with an 8 byte digest and no key
// --------------------------------------------------------------------------------------
const IV: [u64; 8] = [
    0x6a09e667f3bcc908,
    0xbb67ae8584caa73b,
    0x3c6ef372fe94f82b,
    0xa54ff53a5f1d36f1,
    0x510e527fade682d1,
    0x9b05688c2b3e6c1f,
    0x1f83d9abfb41bd6b,
    0x5be0cd19137e2179,
];

const SIGMA: [[usize; 16]; 10] = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
    [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
    [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
    [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
    [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
    [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
    [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
    [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
    [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
];

#[inline(always)]
fn mix(v: &mut [u64; 16], a: usize, b: usize, c: usize, d: usize, x: u64, y: u64) {
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(x);
    v[d] = (v[d] ^ v[a]).rotate_right(32);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(24);
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(y);
    v[d] = (v[d] ^ v[a]).rotate_right(16);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(63);
}

fn compress(h: &mut [u64; 8], block: &[u8; 128], count: u64, last: bool) {
    let mut m = [0u64; 16];
    for (word, chunk) in m.iter_mut().zip(block.as_chunks::<8>().0) {
        *word = u64::from_le_bytes(*chunk);
    }
    let mut v = [0u64; 16];
    v[..8].copy_from_slice(h);
    v[8..].copy_from_slice(&IV);
    v[12] ^= count;
    if last {
        v[14] = !v[14];
    }
    for round in 0..12 {
        let s = &SIGMA[round % 10];
        mix(&mut v, 0, 4, 8, 12, m[s[0]], m[s[1]]);
        mix(&mut v, 1, 5, 9, 13, m[s[2]], m[s[3]]);
        mix(&mut v, 2, 6, 10, 14, m[s[4]], m[s[5]]);
        mix(&mut v, 3, 7, 11, 15, m[s[6]], m[s[7]]);
        mix(&mut v, 0, 5, 10, 15, m[s[8]], m[s[9]]);
        mix(&mut v, 1, 6, 11, 12, m[s[10]], m[s[11]]);
        mix(&mut v, 2, 7, 8, 13, m[s[12]], m[s[13]]);
        mix(&mut v, 3, 4, 9, 14, m[s[14]], m[s[15]]);
    }
    for i in 0..8 {
        h[i] ^= v[i] ^ v[i + 8];
    }
}

/// `int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "big")`
pub fn hash_u64(data: &[u8]) -> u64 {
    let mut h = IV;
    h[0] ^= 0x0101_0000 ^ 8; // digest length 8, no key, fanout 1, depth 1
    let mut offset = 0usize;
    while data.len() - offset > 128 {
        let block: &[u8; 128] = data[offset..offset + 128].try_into().unwrap();
        offset += 128;
        compress(&mut h, block, offset as u64, false);
    }
    let mut block = [0u8; 128];
    block[..data.len() - offset].copy_from_slice(&data[offset..]);
    compress(&mut h, &block, data.len() as u64, true);
    // the digest is the little-endian h[0]; Python reads those bytes big-endian
    h[0].swap_bytes()
}

/// `hash_uniform_bytes(data)`: one uniform variate in [0, 1) keyed by `data`.
#[inline]
pub fn hash_uniform(data: &[u8]) -> f64 {
    hash_u64(data) as f64 / TWO_POW_64
}

/// Append the decimal form of `value` (what `str(int)` writes).
pub fn push_int(buf: &mut Vec<u8>, value: u64) {
    let mut digits = [0u8; 20];
    let mut at = digits.len();
    let mut rest = value;
    loop {
        at -= 1;
        digits[at] = b'0' + (rest % 10) as u8;
        rest /= 10;
        if rest == 0 {
            break;
        }
    }
    buf.extend_from_slice(&digits[at..]);
}

/// `stream_uniform(prefix, counter)`: the `counter`-th variate of a keyed stream.
pub fn stream_uniform(prefix: &[u8], counter: u64, scratch: &mut Vec<u8>) -> f64 {
    scratch.clear();
    scratch.extend_from_slice(prefix);
    scratch.push(b':');
    push_int(scratch, counter);
    hash_uniform(scratch)
}

/// `HashStream`: per-entity randomness, the same key always yields the same sequence.
#[derive(Clone, Debug)]
pub struct HashStream {
    prefix: Vec<u8>,
    counter: u64,
    scratch: Vec<u8>,
}

impl HashStream {
    /// `HashStream(seed, key)` - `seed_text` is `str(seed)`.
    pub fn new(seed_text: &str, key: &str) -> Self {
        let mut prefix = Vec::with_capacity(seed_text.len() + key.len() + 1);
        prefix.extend_from_slice(seed_text.as_bytes());
        prefix.push(b':');
        prefix.extend_from_slice(key.as_bytes());
        HashStream {
            prefix,
            counter: 0,
            scratch: Vec::new(),
        }
    }

    pub fn uniform(&mut self) -> f64 {
        let value = stream_uniform(&self.prefix, self.counter, &mut self.scratch);
        self.counter += 1;
        value
    }

    /// A standard normal variate (Box-Muller, two uniforms per call).
    pub fn normal(&mut self) -> f64 {
        let first = self.uniform();
        let u1 = if first > 1e-12 { first } else { 1e-12 };
        let u2 = self.uniform();
        box_muller(u1, u2)
    }
}

/// `math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)`
#[inline]
pub fn box_muller(u1: f64, u2: f64) -> f64 {
    (-2.0 * ln(u1)).sqrt() * cos(TWO_PI * u2)
}

/// `channel_seed(seed, channel)`: the 64 bit seed of a named channel.
pub fn channel_seed(seed_text: &str, channel: &str) -> u64 {
    let mut key = Vec::with_capacity(seed_text.len() + channel.len() + 1);
    key.extend_from_slice(seed_text.as_bytes());
    key.push(b':');
    key.extend_from_slice(channel.as_bytes());
    hash_u64(&key)
}

// --------------------------------------------------------------------------------------
// CPython's random.Random (MT19937)
// --------------------------------------------------------------------------------------
const N: usize = 624;
const M: usize = 397;

/// `random.Random(seed)` for a non-negative integer seed below 2^64.
pub struct PyRandom {
    state: [u32; N],
    index: usize,
    gauss_next: Option<f64>,
}

impl PyRandom {
    pub fn new(seed: u64) -> Self {
        let low = seed as u32;
        let high = (seed >> 32) as u32;
        let mut rng = PyRandom {
            state: [0; N],
            index: N,
            gauss_next: None,
        };
        if high == 0 {
            rng.init_by_array(&[low]);
        } else {
            rng.init_by_array(&[low, high]);
        }
        rng
    }

    fn init_genrand(&mut self, seed: u32) {
        self.state[0] = seed;
        for i in 1..N {
            let previous = self.state[i - 1];
            self.state[i] = 1812433253u32
                .wrapping_mul(previous ^ (previous >> 30))
                .wrapping_add(i as u32);
        }
        self.index = N;
    }

    fn init_by_array(&mut self, key: &[u32]) {
        self.init_genrand(19650218);
        let mt = &mut self.state;
        let mut i = 1usize;
        let mut j = 0usize;
        for _ in 0..N.max(key.len()) {
            mt[i] = (mt[i] ^ (mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_mul(1664525))
                .wrapping_add(key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= N {
                mt[0] = mt[N - 1];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
        }
        for _ in 0..N - 1 {
            mt[i] = (mt[i] ^ (mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_mul(1566083941))
                .wrapping_sub(i as u32);
            i += 1;
            if i >= N {
                mt[0] = mt[N - 1];
                i = 1;
            }
        }
        mt[0] = 0x8000_0000;
    }

    fn next_u32(&mut self) -> u32 {
        if self.index >= N {
            let mt = &mut self.state;
            for k in 0..N {
                let y = (mt[k] & 0x8000_0000) | (mt[(k + 1) % N] & 0x7fff_ffff);
                let mut next = mt[(k + M) % N] ^ (y >> 1);
                if y & 1 != 0 {
                    next ^= 0x9908_b0df;
                }
                mt[k] = next;
            }
            self.index = 0;
        }
        let mut y = self.state[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^= y >> 18;
        y
    }

    /// `random()`: 53 random bits in [0, 1).
    pub fn random(&mut self) -> f64 {
        let a = (self.next_u32() >> 5) as f64;
        let b = (self.next_u32() >> 6) as f64;
        (a * 67108864.0 + b) * (1.0 / 9007199254740992.0)
    }

    /// `uniform(a, b)`
    pub fn uniform(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.random()
    }

    /// `gauss(0.0, 1.0)`: CPython keeps the second value of every pair.
    pub fn gauss(&mut self) -> f64 {
        let z = match self.gauss_next.take() {
            Some(z) => z,
            None => {
                let x2pi = self.random() * TWO_PI;
                let g2rad = (-2.0 * ln(1.0 - self.random())).sqrt();
                self.gauss_next = Some(sin(x2pi) * g2rad);
                cos(x2pi) * g2rad
            }
        };
        0.0 + z * 1.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blake2b_known_values() {
        // python3 -c "import hashlib;print(int.from_bytes(hashlib.blake2b(b'...',digest_size=8).digest(),'big'))"
        assert_eq!(hash_u64(b""), 0xe4a6a0577479b2b4);
        assert_eq!(hash_u64(b"abc"), 0xd8bb14d833d59559);
        // block boundaries: exactly one block, one byte more, several blocks
        assert_eq!(hash_u64(&[b'x'; 128]), 0x901ce374e8e12402);
        assert_eq!(hash_u64(&[b'y'; 129]), 0x7c617a149f5301e6);
        assert_eq!(hash_u64(&[b'z'; 300]), 0x92920681f95ec7f0);
    }

    #[test]
    fn push_int_matches_str() {
        for value in [0u64, 7, 10, 99, 100, 20261106, u64::MAX] {
            let mut buf = Vec::new();
            push_int(&mut buf, value);
            assert_eq!(String::from_utf8(buf).unwrap(), value.to_string());
        }
    }

    #[test]
    fn mersenne_twister_matches_cpython() {
        // r = random.Random(5489); r.random(); r.gauss(0, 1); r.gauss(0, 1); r.uniform(-0.1, 0.1)
        let mut rng = PyRandom::new(5489);
        assert_eq!(rng.random(), 0.7876110167997803);
        assert_eq!(rng.gauss(), 2.208044982363061);
        assert_eq!(rng.gauss(), 1.5470253317412346);
        assert_eq!(rng.uniform(-0.1, 0.1), -0.083333677963295);
        // a seed wider than 32 bits (two key words) and the zero seed
        assert_eq!(PyRandom::new((1u64 << 40) + 7).random(), 0.6137037779936511);
        assert_eq!(PyRandom::new(0).random(), 0.8444218515250481);
    }
}
