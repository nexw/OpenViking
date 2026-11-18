"""Stdlib-only contracts; mocked PEP 517 hooks are NOT functional build evidence."""
import copy
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts.build_support import android_pure_wheels as pure

LICENSE = b"Complete fixture MIT license/copyright; not a real release license.\n"
LICENSES = {"LICENSE": hashlib.sha256(LICENSE).hexdigest()}


def make_wheel(folder, source, *, extra=None, tag="py3-none-any", purelib=True):
    name = source["name"].replace("-", "_")
    dist = f"{name}-{source['version']}.dist-info"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{source['version']}-{tag}.whl"
    files = {f"{name}/__init__.py": b"raise RuntimeError('fixture must never execute')\n",
             f"{dist}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {source['version']}\n"
                                  f"Requires-Python: {source['requires_python']}\n\n").encode(),
             f"{dist}/WHEEL": ("Wheel-Version: 1.0\nRoot-Is-Purelib: " + str(purelib).lower()
                                + f"\nTag: {tag}\n\n").encode(),
             f"{dist}/licenses/LICENSE": LICENSE, f"{dist}/RECORD": b""}
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


class PureBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = json.loads(pure.DEFAULT_MANIFEST.read_text())
        self.yaml, self.rapid = copy.deepcopy(self.manifest["packages"])
        self.guard = mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_manifest_uses_exact_constraint_versions_and_official_hashes(self):
        constraints = Path("scripts/build_support/android_dependency_constraints.txt").read_text()
        self.assertEqual({p["name"] for p in self.manifest["packages"]}, {"pyyaml", "rapidfuzz"})
        for source in self.manifest["packages"]:
            pure.validate_source(source)
            self.assertIn(f"{source['name']}=={source['version']}\n", constraints)
            self.assertNotIn("/tmp/", json.dumps(source))
            self.assertNotIn("nexw", json.dumps(source))

    def test_explicit_modes_are_not_compile_failure_or_runtime_fallbacks(self):
        self.assertEqual(self.yaml["environment"], {"PYYAML_FORCE_LIBYAML": "0"})
        self.assertEqual(self.rapid["config_settings"], {"wheel.cmake": "false"})
        for source in (self.yaml, self.rapid):
            self.assertNotIn("RAPIDFUZZ_BUILD_EXTENSION", source["environment"])
            self.assertNotIn("RAPIDFUZZ_IMPLEMENTATION", source["environment"])
            self.assertNotIn("wheel.tags", source["config_settings"])
            self.assertNotIn("wheel.platlib", source["config_settings"])

    def test_unreviewed_mode_version_backend_or_license_policy_rejected(self):
        for changes in ({"name": "unknown"}, {"version": "9.9"}, {"environment": {}},
                        {"backend": "fake"}, {"sha256": "not-a-hash"},
                        {"url": "http://files.pythonhosted.org/a.tar.gz"},
                        {"license_files": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                pure.validate_source({**self.yaml, **changes})
        with self.assertRaises(ValueError):
            pure.validate_source({**self.rapid, "config_settings": {"wheel.cmake": "true"}})

    def test_cross_settings_and_foreign_backend_selectors_do_not_leak(self):
        inherited = dict.fromkeys(("PYTHONPATH", "PYTHONHOME", "PYO3_CONFIG_FILE", "TSLP_OFFLINE", "_PYTHON_HOST_PLATFORM", "_PYTHON_SYSCONFIGDATA_NAME", "CC", "CFLAGS", "LDFLAGS", "SKBUILD_WHEEL_TAG", "RAPIDFUZZ_IMPLEMENTATION", "RAPIDFUZZ_BUILD_EXTENSION", "RUSTFLAGS", "SETUPTOOLS_SCM_PRETEND_VERSION"), "foreign")
        inherited.update(PATH="keep", PYYAML_FORCE_LIBYAML="1")
        env = pure.build_environment(self.yaml, inherited)
        self.assertEqual(env, {"PATH": "keep", "PYYAML_FORCE_LIBYAML": "0", "PYTHONNOUSERSITE": "1"})
        self.assertEqual(inherited["PYYAML_FORCE_LIBYAML"], "1")
        self.assertEqual(pure.build_environment(self.rapid, inherited),
                         {"PATH": "keep", "PYTHONNOUSERSITE": "1"})

    def test_real_pure_metadata_and_full_license_accepted_without_byte_changes(self):
        wheel = make_wheel(self.root, self.yaml)
        before = wheel.read_bytes()
        result = pure.inspect_pure_wheel(wheel, self.yaml, LICENSES)
        self.assertEqual(result["native_payloads"], [])
        self.assertEqual(result["tags"], ["py3-none-any"])
        self.assertEqual(result["licenses"][0]["sha256"], LICENSES["LICENSE"])
        self.assertEqual(wheel.read_bytes(), before)

    def test_native_payloads_cannot_hide_in_data_files_or_versioned_so(self):
        for name, value in (("data/blob", b"\x7fELFpayload"), ("data/windows", b"MZpayload"),
                            ("data/lib.a", b"!<arch>\n"), ("data/a.so.1", b"text"),
                            ("data/mac", b"\xcf\xfa\xed\xfepayload")):
            wheel = make_wheel(self.root, self.yaml, extra={name: value})
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "native payload"):
                pure.inspect_pure_wheel(wheel, self.yaml, LICENSES)

    def test_platform_wheel_or_modified_license_cannot_be_repaired_into_pure(self):
        wheel = make_wheel(self.root, self.yaml, tag="cp314-cp314-android_24_arm64_v8a", purelib=False)
        with self.assertRaisesRegex(ValueError, "pure Python"):
            pure.inspect_pure_wheel(wheel, self.yaml, LICENSES)
        wheel = make_wheel(self.root, self.yaml, extra={
            "pyyaml-6.0.3.dist-info/licenses/LICENSE": b"truncated or replaced"})
        with self.assertRaisesRegex(ValueError, "full upstream license"):
            pure.inspect_pure_wheel(wheel, self.yaml, LICENSES)

    def test_internal_tags_and_requires_python_cannot_drift(self):
        for entry, data in (("WHEEL", b"Root-Is-Purelib: true\nTag: py2.py3-none-any\n"),
                            ("METADATA", b"Name: pyyaml\nVersion: 6.0.3\nRequires-Python: >=3.15\n")):
            wheel = make_wheel(self.root, self.yaml, extra={f"pyyaml-6.0.3.dist-info/{entry}": data})
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                pure.inspect_pure_wheel(wheel, self.yaml, LICENSES)

    def test_backend_is_original_and_approved_cli_switches_reach_build(self):
        for source in (self.yaml, self.rapid):
            project = self.root / source["name"]
            project.mkdir()
            descriptor = "[build-system]\nbuild-backend = " + json.dumps(source["backend"]) + "\n"
            descriptor += "backend-path = " + json.dumps(source["backend_path"]) + "\n"
            (project / "pyproject.toml").write_text(descriptor)
            (project / "LICENSE").write_bytes(LICENSE)
            output = self.root / (source["name"] + "-out")
            output.mkdir()
            original = {p.name: p.read_bytes() for p in project.iterdir()}

            def hook(command, *, cwd, env, check, project=project, source=source):
                self.assertEqual(cwd, project)
                self.assertTrue(check)
                self.assertEqual(command[:5], [pure.sys.executable, "-m", "build", "--wheel", "--no-isolation"])
                if source["name"] == "rapidfuzz":
                    self.assertEqual(command[-2:], ["--config-setting", "wheel.cmake=false"])
                else:
                    self.assertEqual(env["PYYAML_FORCE_LIBYAML"], "0")
                return make_wheel(Path(command[command.index("--outdir") + 1]), source)

            with mock.patch.object(pure, "fetch_source", return_value=self.root / "inert.tar.gz"), \
                    mock.patch.object(pure, "unpack_source", return_value=project), \
                    mock.patch.object(pure.subprocess, "run", side_effect=hook) as run:
                result = pure.build_one(source, self.root / "cache", output)
            self.assertEqual(run.call_count, 1)
            self.assertEqual({p.name: p.read_bytes() for p in project.iterdir()}, original)
            self.assertEqual(pure.sha256(output / result["wheel"]), result["sha256"])

    def batch_fixture(self):
        manifest = self.root / "sources.json"
        manifest.write_text(json.dumps(self.manifest))
        return manifest, self.root / "cache", self.root / "out"

    def fake_build(self, source, cache, output):
        wheel = make_wheel(output, source)
        return pure.inspect_pure_wheel(wheel, source, LICENSES)

    def batch_patches(self):
        patcher = mock.patch.object(pure, "tool_versions", return_value=self.manifest["build_tools"])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(pure, "source_commit", return_value="a" * 40)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_batch_atomically_publishes_complete_manifest_and_unchanged_wheels(self):
        self.batch_patches()
        manifest, cache, output = self.batch_fixture()
        output.mkdir()
        with mock.patch.object(pure, "build_one", side_effect=self.fake_build):
            result = pure.build_batch(manifest, cache, output)
        self.assertEqual(len(result["wheels"]), 2)
        self.assertEqual(result["manifest_sha256"], pure.sha256(manifest))
        self.assertEqual(json.loads((output / "pure-wheels.json").read_text()), result)
        self.assertEqual(len(list(output.iterdir())), 3)
        self.assertFalse(list(self.root.glob(".android-pure-*")))

    def test_second_failure_never_leaves_partial_output_or_completion_marker(self):
        self.batch_patches()
        manifest, cache, output = self.batch_fixture()
        output.mkdir()

        def fail_second(source, cache, staged):
            if source["name"] == "rapidfuzz":
                raise RuntimeError("second build failed")
            return self.fake_build(source, cache, staged)

        with mock.patch.object(pure, "build_one", side_effect=fail_second), \
                self.assertRaisesRegex(RuntimeError, "second build failed"):
            pure.build_batch(manifest, cache, output)
        self.assertEqual(list(output.iterdir()), [])
        self.assertFalse(list(self.root.glob(".android-pure-*")))

    def test_stale_output_and_unreviewed_second_source_fail_before_any_build(self):
        manifest, cache, output = self.batch_fixture()
        output.mkdir()
        (output / "stale.whl").write_bytes(b"old")
        with mock.patch.object(pure, "build_one") as build, self.assertRaisesRegex(ValueError, "empty"):
            pure.build_batch(manifest, cache, output)
        build.assert_not_called()
        self.manifest["packages"][1]["config_settings"] = {"wheel.cmake": "true"}
        manifest.write_text(json.dumps(self.manifest))
        with mock.patch.object(pure, "build_one") as build, self.assertRaisesRegex(ValueError, "reviewed"):
            pure.build_batch(manifest, cache, self.root / "fresh")
        build.assert_not_called()

    def test_bad_source_sha_fails_before_extraction_or_backend_execution(self):
        with mock.patch("urllib.request.urlopen") as request, \
                mock.patch.object(pure, "unpack_source") as unpack, \
                mock.patch.object(pure.subprocess, "run") as run:
            request.return_value.__enter__.return_value.read.return_value = b"untrusted-not-an-archive"
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                pure.build_one(self.yaml, self.root / "cache", self.root / "out")
        unpack.assert_not_called()
        run.assert_not_called()

    def test_failed_atomic_publication_leaves_no_completion_manifest(self):
        self.batch_patches()
        manifest, cache, output = self.batch_fixture()
        output.mkdir()
        with mock.patch.object(pure, "build_one", side_effect=self.fake_build), \
                mock.patch.object(pure.os, "replace", side_effect=OSError("publish failed")), \
                self.assertRaisesRegex(OSError, "publish failed"):
            pure.build_batch(manifest, cache, output)
        self.assertEqual(list(output.iterdir()), [])
        self.assertFalse(list(self.root.glob(".android-pure-*")))

    def test_build_tool_drift_rejected_before_source_execution(self):
        with mock.patch.object(pure.importlib.metadata, "version", return_value="wrong"), \
                self.assertRaisesRegex(ValueError, "build tools differ"):
            pure.tool_versions(self.manifest["build_tools"])


if __name__ == "__main__":
    unittest.main()
