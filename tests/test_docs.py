"""The documentation must agree with the code.

1. Every relative link resolves.
2. Every ``group.parameter`` the docs mention exists in the configuration.
3. Every parameter of the configuration is documented in docs/PARAMETERS.md.
4. Every indicator of the metric table in docs/MODEL.md exists in the summary.
5. No em dash: the project writes plain hyphens.
"""
from __future__ import annotations

import os
import re
import unittest
from typing import Dict

from ticketsim.schema import Config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(REPO, "docs")


def _texts() -> Dict[str, str]:
    out = {"README.md": open(os.path.join(REPO, "README.md"), encoding="utf-8").read()}
    for name in sorted(os.listdir(DOCS)):
        if name.endswith(".md"):
            with open(os.path.join(DOCS, name), "r", encoding="utf-8") as handle:
                out[f"docs/{name}"] = handle.read()
    return out


class DocumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.texts = _texts()

    def test_the_docs_exist(self):
        for name in ("README.md", "docs/MODEL.md", "docs/ENGINEERING.md", "docs/PARAMETERS.md"):
            self.assertIn(name, self.texts)

    def test_the_links_resolve(self):
        for name, text in self.texts.items():
            base = os.path.dirname(os.path.join(REPO, name))
            for link in re.findall(r"\]\(([^)#]+)\)", text):
                if link.startswith("http"):
                    continue
                self.assertTrue(os.path.exists(os.path.normpath(os.path.join(base, link))),
                                f"{name} → {link} does not exist")

    def test_every_mentioned_parameter_exists(self):
        config = Config().to_dict()
        for name, text in self.texts.items():
            for group, field in re.findall(r"`(scenario|behavior|model_constants)\.([a-z_]+)",
                                           text):
                self.assertIn(field, config[group], f"{name}: {group}.{field}")

    def test_every_parameter_is_documented(self):
        text = self.texts["docs/PARAMETERS.md"]
        config = Config().to_dict()
        for group in ("behavior", "model_constants"):
            for field in config[group]:
                self.assertIn(f"`{group}.{field}`", text)
        for field in ("buyers", "horizon"):
            self.assertIn(f"`scenario.{field}`", text)
        for field in ("seats", "per_buyer_limit", "open_at", "cells"):
            self.assertIn(f"`sessions[].{field}", text)

    def test_every_indicator_exists(self):
        from ticketsim import run
        from helpers import small_config
        summary = run(small_config(buyers=50, seats=40, horizon=60.0)).summary
        text = self.texts["docs/MODEL.md"]
        table = text[text.index("## 10. Indicators"):text.index("## 11.")]
        names = re.findall(r"^\| `([a-z_]+)`(?: / `([a-z_]+)`)*", table, flags=re.M)
        mentioned = set(re.findall(r"`([a-z_]+)`", "\n".join(
            line for line in table.splitlines() if line.startswith("| `"))))
        self.assertGreater(len(names), 15, "the indicator table could not be parsed")
        for ident in mentioned:
            if ident in ("tickets_held", "seats") or ident in summary:
                continue
            self.fail(f"docs/MODEL.md mentions `{ident}`, which is not in the summary")

    def test_no_em_dash(self):
        for name, text in self.texts.items():
            self.assertNotIn("\u2014", text, name)


if __name__ == "__main__":
    unittest.main()
