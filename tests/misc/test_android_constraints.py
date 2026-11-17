"""Keep component wheels on the same audited Android dependency snapshot."""
import hashlib
import json
import re
import unittest
from pathlib import Path

SUPPORT = Path(__file__).resolve().parents[2] / "scripts/build_support"


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def constraints():
    result = {}
    for line in (SUPPORT / "android_dependency_constraints.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        key = canonical(name)
        if key in result:
            raise ValueError(f"duplicate dependency: {key}")
        result[key] = version
    return result


class ConstraintTests(unittest.TestCase):
    def test_audited_closure_has_322_unique_pins(self):
        self.assertEqual(len(constraints()), 322)

    def test_all_component_pins_match_same_closure(self):
        pins = constraints()
        parser = json.loads((SUPPORT / "android_parser_sources.json").read_text())
        language = json.loads((SUPPORT / "android_language_pack_sources.json").read_text())
        rust = json.loads((SUPPORT / "android_rust_sources.json").read_text())
        sources = [*parser["packages"], language["python_source"],
                   *language["integration_wheels"], *rust["packages"], *rust["support_wheels"]]
        for source in sources:
            with self.subTest(name=source["name"]):
                self.assertEqual(pins[canonical(source["name"])], source["version"])

    def test_published_and_source_build_queues_partition_the_same_closure(self):
        published = json.loads((SUPPORT / "android_published_wheels.json").read_text())
        rows = [*published["wheels"], *published["missing"]]
        actual = {canonical(row["name"]): row["version"] for row in rows}
        self.assertEqual(len(rows), len(actual))
        self.assertEqual(actual, constraints())
        self.assertEqual(len(published["wheels"]), 286)
        self.assertEqual(len(published["missing"]), 36)
        self.assertEqual(published["constraints_sha256"], hashlib.sha256(
            (SUPPORT / "android_dependency_constraints.txt").read_bytes()).hexdigest())

    def test_pydantic_core_pair_is_the_audited_exact_pair(self):
        pins = constraints()
        self.assertEqual(pins["pydantic"], "2.14.3")
        self.assertEqual(pins["pydantic-core"], "2.50.4")


if __name__ == "__main__":
    unittest.main()
