"""Offline, stdlib-only smoke-driver tests; no installed parser wheels required.

Run with unittest discover -s tests/misc -p test_android_parser_smoke.py.
"""
import importlib.util
import io
import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "android_parser_smoke", ROOT / "scripts/build_support/android_parser_smoke.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class SmokeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.manifest = json.loads(smoke.DEFAULT_MANIFEST.read_text())
        self.pins = {p["name"]: p["version"] for p in self.manifest["packages"]}
        self.output = self.directory / "results.json"
        self.runtime = SimpleNamespace(Language=mock.Mock(side_effect=lambda capsule: capsule),
                                       Parser=mock.Mock())
        self.root = SimpleNamespace(has_error=False, child_count=1, type="program")
        self.runtime.Parser.return_value.parse.return_value = SimpleNamespace(root_node=self.root)
        self.metadata = self.enterContext(mock.patch.object(
            smoke.importlib.metadata, "version", side_effect=self.pins.__getitem__
        ))
        self.import_module = self.enterContext(mock.patch.object(
            smoke.importlib, "import_module", side_effect=self.grammar_module
        ))
        self.enterContext(mock.patch.dict(sys.modules, {"tree_sitter": self.runtime}))
        self.enterContext(mock.patch.object(sys, "stdout", new=io.StringIO()))

    def grammar_module(self, name):
        language = name.removeprefix("tree_sitter_")
        return SimpleNamespace(**{
            binding: (lambda: "grammar capsule")
            for sample_language, binding, _ in smoke.SAMPLES if sample_language == language
        })

    def write_manifest(self):
        path = self.directory / "source-manifest.json"
        path.write_text(json.dumps(self.manifest))
        return path

    def test_default_manifest_checks_all_11_pins_and_reports_12_parses(self):
        self.assertEqual(smoke.DEFAULT_MANIFEST,
                         ROOT / "scripts/build_support/android_parser_sources.json")
        self.assertEqual(smoke.main(["--output", str(self.output)]), 0)
        self.assertEqual(self.metadata.call_count, 11)
        self.assertEqual({call.args[0] for call in self.metadata.call_args_list}, set(self.pins))
        report = json.loads(self.output.read_text())
        self.assertEqual(report["python_version"], platform.python_version())
        self.assertEqual(report["runtime_version"], self.pins["tree-sitter"])
        self.assertEqual(len(report["results"]), 12)
        self.assertEqual(self.runtime.Parser.call_count, 12)
        for result, (language, binding, source) in zip(report["results"], smoke.SAMPLES):
            self.assertEqual(result, {
                "language": language, "binding": binding,
                "version": self.pins["tree-sitter-" + language.replace("_", "-")],
                "root": "program",
            })
        self.assertEqual([call.args[0] for call in self.runtime.Parser.return_value.parse.call_args_list],
                         [source for _, _, source in smoke.SAMPLES])

    def test_explicit_artifact_manifest_is_used_instead_of_default(self):
        for package in self.manifest["packages"]:
            package["version"] = "9.8.7"
            self.pins[package["name"]] = "9.8.7"
        path = self.write_manifest()
        self.assertEqual(smoke.main(["--manifest", str(path), "--output", str(self.output)]), 0)
        report = json.loads(self.output.read_text())
        self.assertEqual(report["runtime_version"], "9.8.7")
        self.assertTrue(all(result["version"] == "9.8.7" for result in report["results"]))

    def test_mismatched_runtime_version_rejected_before_parsing_or_reporting(self):
        self.pins["tree-sitter"] = "0.0.0"
        with self.assertRaisesRegex(AssertionError, "version mismatch for tree-sitter: expected .*installed 0.0.0"):
            smoke.main(["--output", str(self.output)])
        self.import_module.assert_not_called()
        self.runtime.Parser.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_mismatched_grammar_version_rejected_before_parsing_or_reporting(self):
        self.pins["tree-sitter-typescript"] = "0.0.0"
        with self.assertRaisesRegex(AssertionError, "version mismatch for tree-sitter-typescript:"):
            smoke.main(["--output", str(self.output)])
        self.import_module.assert_not_called()
        self.runtime.Parser.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_manifest_must_have_exactly_the_11_unique_distributions(self):
        original = self.manifest["packages"]
        for packages in (original[:-1], original + [original[0]],
                         original[:-1] + [{"name": "other-parser", "version": "1.0"}]):
            with self.subTest(packages=packages):
                self.manifest["packages"] = packages
                path = self.write_manifest()
                with self.assertRaisesRegex(ValueError, "exactly the 11 parser distribution pins"):
                    smoke.main(["--manifest", str(path)])
        self.metadata.assert_not_called()
        self.runtime.Parser.assert_not_called()

    def test_invalid_parse_does_not_write_success_report(self):
        for has_error, child_count in ((True, 1), (False, 0)):
            with self.subTest(has_error=has_error, child_count=child_count):
                self.root.has_error = has_error
                self.root.child_count = child_count
                with self.assertRaisesRegex(AssertionError, "invalid parse for python/language"):
                    smoke.main(["--output", str(self.output)])
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
