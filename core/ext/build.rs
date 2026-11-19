fn main() {
    // An extension module must not link libpython: the interpreter that
    // imports it provides the symbols (macOS needs `-undefined dynamic_lookup`).
    pyo3_build_config::add_extension_module_link_args();
}
