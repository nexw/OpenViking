"""Notice collection must use the actual maturin feature selection, not defaults."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.build_support import android_rust_common as common


class NoticeFeatureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "source"
        self.project.mkdir()

    def config(self, text):
        (self.project / "pyproject.toml").write_text(text)
        return common.maturin_cargo_options(self.project)

    def test_default_selection_does_not_invent_features(self):
        self.assertEqual(common.maturin_cargo_options(self.project), [])
        self.assertEqual(self.config('[build-system]\nbuild-backend = "maturin"\n'), [])

    def test_optional_python_dependency_uses_declared_feature_not_all_features(self):
        options = self.config('[tool.maturin]\nfeatures = ["python"]\n')
        self.assertEqual(options, ["--features", "python"])
        self.assertNotIn("--all-features", options)
        self.assertNotIn("mutate", " ".join(options))

    def test_dependency_features_and_no_default_features_are_preserved(self):
        options = self.config('[tool.maturin]\nfeatures = ["pyo3/extension-module", '
                              '"pyo3/abi3-py310"]\nno-default-features = true\n')
        self.assertEqual(options, ["--features", "pyo3/extension-module,pyo3/abi3-py310",
                                   "--no-default-features"])

    def test_all_features_only_when_explicitly_requested_by_upstream(self):
        self.assertEqual(self.config('[tool.maturin]\nall-features = true\n'), ["--all-features"])
        self.assertEqual(self.config('[tool.maturin]\nall-features = false\n'), [])

    def test_invalid_features_or_boolean_flags_fail_closed(self):
        for setting in ('features = "python"', 'features = ["--all-features"]',
                        'features = ["python,mutate"]', 'features = ["py thon"]',
                        'features = [1]', 'all-features = "false"', 'no-default-features = 1'):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                self.config('[tool.maturin]\n' + setting + '\n')

    def test_feature_selected_pyo3_notice_is_physically_copied_and_recorded(self):
        self.config('[tool.maturin]\nfeatures = ["python"]\n')
        (self.project / "Cargo.toml").write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
        (self.project / "LICENSE").write_bytes(b"Complete fixture project license.\n")
        dependency = self.root / "pyo3-source"
        dependency.mkdir()
        (dependency / "Cargo.toml").write_text('[package]\nname="pyo3"\nversion="0.26.0"\n')
        license_bytes = b"Complete fixture PyO3 MIT copyright/permission text; not a release license.\n"
        (dependency / "LICENSE-MIT").write_bytes(license_bytes)
        metadata = {"workspace_root": str(self.project), "packages": [
            {"name": "pyo3", "version": "0.26.0", "id": "fixture-pyo3",
             "source": "registry+fixture", "manifest_path": str(dependency / "Cargo.toml"),
             "license": "MIT OR Apache-2.0"}]}
        wheel = self.root / "fixture-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"inert unit fixture; subprocess unpack is mocked")
        directory = self.root / "work"
        directory.mkdir()
        dist_info = directory / "notice-unpacked" / "fixture" / "fixture-0.1.0.dist-info"

        def cargo(command, **kwargs):
            self.assertIn("--locked", command)
            self.assertIn("--filter-platform", command)
            self.assertEqual(command[-2:], ["--features", "python"])
            self.assertEqual(kwargs["cwd"], self.project)
            return json.dumps(metadata)

        def wheel_command(command, **kwargs):
            self.assertTrue(kwargs["check"])
            if "unpack" in command:
                dist_info.mkdir(parents=True)
            elif "pack" in command:
                copied, = dist_info.glob("licenses/rust/*pyo3*/LICENSE-MIT")
                self.assertEqual(copied.read_bytes(), license_bytes)
            else:
                self.fail(f"unexpected command: {command}")

        with mock.patch.object(common.subprocess, "check_output", side_effect=cargo), \
                mock.patch.object(common.subprocess, "run", side_effect=wheel_command):
            result = common.include_notices(self.project, wheel, directory, {}, self.project / "Cargo.toml")
        self.assertEqual(result["cargo_metadata_options"], ["--features", "python"])
        self.assertEqual(result["rust_packages"], 1)
        report = json.loads((dist_info / "ANDROID-THIRD-PARTY.json").read_text())
        self.assertEqual(report["cargo_metadata_options"], ["--features", "python"])
        package, = report["rust_packages"]
        self.assertEqual(package["name"], "pyo3")
        self.assertEqual(len(package["notices"]), 1)
        notice, = package["notices"]
        self.assertEqual(notice["sha256"], common.sha256(dependency / "LICENSE-MIT"))
        self.assertEqual((dist_info / notice["file"]).read_bytes(), license_bytes)


if __name__ == "__main__":
    unittest.main()
