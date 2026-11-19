#!/usr/bin/env python3
"""Build the Rust world core and install it into the ``ticketsim`` package.

    mise run build                      # the usual way (mise provides cargo)
    python3 scripts/build_core.py       # same thing, cargo must be on PATH
    python3 scripts/build_core.py --debug

``cargo build`` produces ``core/target/<profile>/libticketsim_core.{dylib,so}``; an
extension module is that very file under the name Python imports:
``ticketsim/ticketsim_core.abi3.so`` (stable ABI: one build serves every Python >= 3.9).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "core")


def built_library(profile: str) -> str:
    if sys.platform == "darwin":
        name = "libticketsim_core.dylib"
    elif sys.platform == "win32":
        name = "ticketsim_core.dll"
    else:
        name = "libticketsim_core.so"
    return os.path.join(CORE, "target", profile, name)


def installed_module() -> str:
    suffix = ".pyd" if sys.platform == "win32" else ".abi3.so"
    return os.path.join(ROOT, "ticketsim", "ticketsim_core" + suffix)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build ticketsim_core (the Rust world core)")
    parser.add_argument("--debug", action="store_true",
                        help="an unoptimised build with debug assertions (slow)")
    args = parser.parse_args(argv)

    cargo = shutil.which("cargo")
    if cargo is None:
        print("cargo not found: run this through mise (`mise run build`) or "
              "`mise exec -- python3 scripts/build_core.py`", file=sys.stderr)
        return 1
    env = dict(os.environ)
    # an extension module does not link libpython: the interpreter provides it
    env["PYO3_BUILD_EXTENSION_MODULE"] = "1"
    # build against the interpreter that will import the module
    env.setdefault("PYO3_PYTHON", sys.executable)
    command = [cargo, "build", "--manifest-path", os.path.join(CORE, "Cargo.toml"),
               "--package", "ticketsim-core", "--locked"]
    if not args.debug:
        command.append("--release")
    subprocess.run(command, check=True, env=env)

    source = built_library("debug" if args.debug else "release")
    target = installed_module()
    # replace the file instead of writing into it: a running interpreter may
    # have the old one mapped
    staging = target + ".new"
    shutil.copyfile(source, staging)
    os.replace(staging, target)
    print(f"installed {os.path.relpath(target, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
