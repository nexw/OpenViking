"""Offline stdlib tests for Android target pins and Termux sysroot provenance.

Run: python -m unittest discover -s tests/misc -p test_android_target.py
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.build_support import android_target, termux_sysroot


# Deliberately independent of load_target(): changes to these pins need review.
KNOWN_TARGET = {
    "schema": 1,
    "architecture": "aarch64",
    "minimum_android_api": 24,
    "python_minor": "3.14",
    "build_python": "3.14.10",
    "termux_python_package": "3.14.10",
    "termux_python_deb_sha256":
        "1a5a300c8d844f2c18a3bb61cafbffb2e93fba9a7106ebe967f06c9628d1e434",
    "ndk_revision": "30.0.16248370",
    "rust_toolchain": "1.100.0",
}


def sha256(path):
    """Compute expected hashes without using the helpers under test."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FixtureTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        # Any accidental download or native command is a test failure, not I/O.
        for owner, name in (
            (termux_sysroot, "_fetch"),
            (subprocess, "run"),
            (subprocess, "check_output"),
        ):
            patcher = mock.patch.object(
                owner, name, side_effect=AssertionError(f"unexpected call: {name}")
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_prefix(self, prefix, minor="3.14", library=b"fixture libpython\x00"):
        headers = prefix / "include" / f"python{minor}"
        headers.mkdir(parents=True, exist_ok=True)
        (headers / "Python.h").write_text("/* offline fixture */\n")
        (headers / "pyconfig.h").write_text("#define SIZEOF_VOID_P 8\n")
        lib = prefix / "lib"
        lib.mkdir(parents=True, exist_ok=True)
        (lib / f"libpython{minor}.so").write_bytes(library)
        config_dir = lib / f"python{minor}"
        config_dir.mkdir(parents=True, exist_ok=True)
        # Reading/hashing this file is safe; executing target sysconfig is not.
        config = config_dir / "_sysconfigdata__android_aarch64.py"
        config.write_text('raise AssertionError("target sysconfig must not execute")\n')
        return config


class TargetProfileTests(FixtureTestCase):
    def test_checked_in_profile_and_exact_environment(self):
        target = android_target.load_target(android_target.DEFAULT_TARGET)
        self.assertEqual(target, KNOWN_TARGET)
        self.assertEqual(android_target.target_env(target), {
            "TERMUX_PY_VERSION_PIN": "3.14",
            "TERMUX_PYTHON_DEB_SHA256": KNOWN_TARGET["termux_python_deb_sha256"],
            "ANDROID_BUILD_PYTHON": "3.14.10",
            "ANDROID_NDK_REVISION": "30.0.16248370",
            "ANDROID_RUST_TOOLCHAIN": "1.100.0",
        })

    def test_load_rejects_invalid_independent_digest(self):
        path = self.directory / "target.json"
        for checksum in ("", "a" * 63, "a" * 65, "g" * 64, "A" * 64, "a" * 64 + "\n"):
            with self.subTest(checksum=checksum):
                path.write_text(json.dumps(dict(KNOWN_TARGET, termux_python_deb_sha256=checksum)))
                with self.assertRaisesRegex(ValueError, "independent Python deb checksum"):
                    android_target.load_target(path)

    def test_load_rejects_build_python_minor_mismatch(self):
        path = self.directory / "target.json"
        for build_python in ("3.13.10", "3.140.10", "3.14"):
            with self.subTest(build_python=build_python):
                path.write_text(json.dumps(dict(KNOWN_TARGET, build_python=build_python)))
                with self.assertRaisesRegex(ValueError, "build interpreter and target minor"):
                    android_target.load_target(path)

    def test_load_rejects_unsupported_schema_or_architecture(self):
        path = self.directory / "target.json"
        for change in ({"schema": 2}, {"architecture": "x86_64"}):
            with self.subTest(change=change):
                path.write_text(json.dumps(dict(KNOWN_TARGET, **change)))
                with self.assertRaisesRegex(ValueError, "unsupported Android target profile"):
                    android_target.load_target(path)

    def test_env_command_writes_known_profile_without_inspecting_target(self):
        output = self.directory / "nested" / "target.env"
        with mock.patch.object(android_target, "describe_python") as describe:
            self.assertEqual(android_target.main(["env", "--out", str(output)]), 0)
        describe.assert_not_called()
        self.assertEqual(output.read_text(), (
            "TERMUX_PY_VERSION_PIN=3.14\n"
            f"TERMUX_PYTHON_DEB_SHA256={KNOWN_TARGET['termux_python_deb_sha256']}\n"
            "ANDROID_BUILD_PYTHON=3.14.10\n"
            "ANDROID_NDK_REVISION=30.0.16248370\n"
            "ANDROID_RUST_TOOLCHAIN=1.100.0\n"
        ))


class MaterializeTests(FixtureTestCase):
    def setUp(self):
        super().setUp()
        self.deb = self.directory / "python.deb"
        self.deb.write_bytes(b"offline deb revision one")
        self.dest = self.directory / "sysroot"
        self.repo = "https://example.invalid/termux"
        # The repository checksum agrees with the downloaded file. It must not
        # supersede a different, independently trusted target checksum.
        self.stanza = {
            "Package": "python", "Version": "3.14.10", "Architecture": "aarch64",
            "Filename": "pool/python.deb", "SHA256": sha256(self.deb),
        }
        self.resolve = self.enterContext(mock.patch.object(
            termux_sysroot, "resolve_package", return_value=self.stanza
        ))
        self.download = self.enterContext(mock.patch.object(
            termux_sysroot, "download_package", return_value=self.deb
        ))
        self.extract = self.enterContext(mock.patch.object(
            termux_sysroot, "extract_deb", side_effect=self.fake_extract
        ))

    def fake_extract(self, deb, root):
        self.make_prefix(root / termux_sysroot.TERMUX_PREFIX_SUFFIX,
                         library=b"extracted from: " + deb.read_bytes())

    def materialize(self, **overrides):
        args = dict(repo=self.repo, arch="aarch64", package="python", dest=self.dest,
                    version_prefix="3.14", expected_sha256=sha256(self.deb))
        args.update(overrides)
        return termux_sysroot.materialize(**args)

    def test_wrong_independent_pin_rejected_before_extraction_or_description(self):
        with mock.patch.object(termux_sysroot, "find_prefix") as find, mock.patch.object(
            termux_sysroot, "describe_python"
        ) as describe:
            with self.assertRaisesRegex(RuntimeError, "independently pinned SHA256"):
                self.materialize(expected_sha256="0" * 64)
        self.extract.assert_not_called()
        find.assert_not_called()
        describe.assert_not_called()
        self.assertFalse((self.dest / "sysroot-source.json").exists())
        self.resolve.assert_called_once_with(self.repo, "aarch64", "python", "3.14")
        self.download.assert_called_once_with(self.stanza, self.repo, self.dest / "_downloads")

    def test_matching_pin_records_actual_deb_digest_and_source_identity(self):
        env = self.materialize()
        self.assertEqual(env["TERMUX_DEB_SHA256"], sha256(self.deb))
        self.assertEqual(env["TERMUX_DEB"], str(self.deb))
        self.assertEqual(env["TERMUX_PACKAGE"], "python=3.14.10")
        self.assertEqual(env["TERMUX_REPO"], self.repo)
        self.assertEqual(env["TERMUX_PY_VERSION"], "3.14")
        self.assertEqual(json.loads((self.dest / "sysroot-source.json").read_text()), {
            "deb_sha256": sha256(self.deb), "arch": "aarch64",
            "package": "python", "version": "3.14.10",
        })
        self.assertTrue(Path(env["TERMUX_PY_LIBRARY"]).is_file())
        self.extract.assert_called_once()

    def test_identical_identity_reuses_extraction_but_rechecks_pin(self):
        first = self.materialize()
        self.extract.reset_mock()
        self.assertEqual(self.materialize(), first)
        self.extract.assert_not_called()
        with mock.patch.object(termux_sysroot, "describe_python") as describe:
            with self.assertRaisesRegex(RuntimeError, "independently pinned SHA256"):
                self.materialize(expected_sha256="0" * 64)
        describe.assert_not_called()
        self.extract.assert_not_called()

    def test_changed_deb_at_same_path_replaces_stale_extraction(self):
        first = self.materialize()
        library = Path(first["TERMUX_PY_LIBRARY"])
        old_library = library.read_bytes()
        stale = library.parent / "stale-only-in-old-extraction"
        stale.write_bytes(b"stale")
        self.extract.reset_mock()
        self.deb.write_bytes(b"offline deb revision two, same package version")
        self.stanza["SHA256"] = sha256(self.deb)
        second = self.materialize()
        self.extract.assert_called_once()
        self.assertNotEqual(second["TERMUX_DEB_SHA256"], first["TERMUX_DEB_SHA256"])
        self.assertNotEqual(library.read_bytes(), old_library)
        self.assertEqual(library.read_bytes(), b"extracted from: " + self.deb.read_bytes())
        self.assertFalse(stale.exists())
        marker = json.loads((self.dest / "sysroot-source.json").read_text())
        self.assertEqual(marker["deb_sha256"], sha256(self.deb))

    def test_package_architecture_and_version_each_invalidate_reuse(self):
        self.materialize()
        for overrides, key, expected in (
            ({"package": "python-alt"}, "package", "python-alt"),
            ({"arch": "arm"}, "arch", "arm"),
            ({}, "version", "3.14.11"),
        ):
            with self.subTest(identity=key):
                # Reset the baseline so precisely one identity field changes.
                self.materialize()
                self.extract.reset_mock()
                if key == "version":
                    self.stanza["Version"] = expected
                self.materialize(**overrides)
                self.extract.assert_called_once()
                marker = json.loads((self.dest / "sysroot-source.json").read_text())
                self.assertEqual(marker[key], expected)

    def test_missing_or_invalid_marker_cannot_reuse_old_extraction(self):
        self.materialize()
        marker = self.dest / "sysroot-source.json"
        for contents in (None, "not json", "{}"):
            with self.subTest(contents=contents):
                if contents is None:
                    marker.unlink()
                else:
                    marker.write_text(contents)
                self.extract.reset_mock()
                self.materialize()
                self.extract.assert_called_once()

    def test_no_reuse_forces_extraction_even_with_matching_marker(self):
        self.materialize()
        self.extract.reset_mock()
        self.materialize(reuse=False)
        self.extract.assert_called_once()

    def test_requested_minor_mismatch_fails_after_describing_real_fixture(self):
        # resolve_package's version filter alone cannot establish extracted ABI.
        with self.assertRaisesRegex(RuntimeError, r"resolved CPython 3\.14.*requested 3\.13"):
            self.materialize(version_prefix="3.13")
        self.resolve.assert_called_once_with(self.repo, "aarch64", "python", "3.13")


class ProvenanceTests(FixtureTestCase):
    def setUp(self):
        super().setUp()
        self.deb = self.directory / "python.deb"
        self.deb.write_bytes(b"actual provenance input deb\x00")
        self.prefix = self.directory / "prefix"
        self.config = self.make_prefix(self.prefix)
        self.library = self.prefix / "lib" / "libpython3.14.so"
        self.target = dict(KNOWN_TARGET, termux_python_deb_sha256=sha256(self.deb))
        self.compiler = "/fixture/ndk/bin/aarch64-linux-android24-clang"
        self.commit = "0123456789abcdef0123456789abcdef01234567"
        self.compiler_version = "Android clang version fixture-exact\nTarget: aarch64\n"
        self.rust = {
            "rustc": (
                "rustc 1.85.0 (4d91de4e4 2025-02-17)\n"
                "binary: rustc\n"
                "commit-hash: 4d91de4e48198da2e33413efdcd9cd2cc0c46688\n"
                "commit-date: 2025-02-17\n"
                "host: x86_64-unknown-linux-gnu\n"
                "release: 1.85.0\n"
                "LLVM version: 19.1.7\n"
            ),
            "cargo": "cargo 1.85.0 (d73d2caf9 2024-12-31)\n",
        }
        self.tools = {"setuptools": "75.8.2", "wheel": "0.45.1",
                      "build": "1.2.2.post1", "maturin": "1.8.3"}
        self.enterContext(mock.patch.dict(os.environ, {
            "TERMUX_DEB": str(self.deb),
            "TERMUX_PACKAGE": "python=3.14.10",
            "TERMUX_REPO": "https://example.invalid/termux",
            "NDK_VERSION": self.target["ndk_revision"],
        }, clear=True))
        self.host_version = self.enterContext(mock.patch.object(
            android_target.platform, "python_version", return_value="3.14.10"
        ))
        self.enterContext(mock.patch.object(sys, "version", "3.14.10 (fixture host build)"))
        self.metadata = self.enterContext(mock.patch.object(
            android_target.importlib.metadata, "version", side_effect=self.tools.__getitem__
        ))
        self.commands = self.enterContext(mock.patch.object(
            android_target.subprocess, "check_output", side_effect=self.command_output
        ))

    def command_output(self, argv, *, text):
        self.assertTrue(text)
        if argv == ["git", "rev-parse", "HEAD"]:
            return self.commit + "\n"
        if argv == [self.compiler, "--version"]:
            return self.compiler_version
        if argv == ["rustc", "-vV"]:
            return self.rust["rustc"]
        if argv == ["cargo", "--version"]:
            return self.rust["cargo"]
        self.fail(f"unexpected native command: {argv}")

    def record(self):
        return android_target.record(self.target, self.prefix, self.compiler)

    def test_records_actual_artifact_hashes_and_exact_toolchain_provenance(self):
        data = self.record()
        self.assertEqual(data, {
            "schema": 1, "commit": self.commit, "target": self.target,
            "termux_package": "python=3.14.10",
            "termux_repo": "https://example.invalid/termux",
            "termux_deb_sha256": sha256(self.deb),
            "target_sysconfig_sha256": sha256(self.config),
            "target_libpython_sha256": sha256(self.library),
            "ndk_revision": "30.0.16248370",
            "python": "3.14.10 (fixture host build)",
            "build_tools": self.tools, "rust": self.rust,
            "compiler": self.compiler_version,
        })
        self.assertEqual(self.metadata.call_args_list,
                         [mock.call(name) for name in self.tools])
        self.assertEqual(self.commands.call_args_list, [
            mock.call(["git", "rev-parse", "HEAD"], text=True),
            mock.call(["rustc", "-vV"], text=True),
            mock.call(["cargo", "--version"], text=True),
            mock.call([self.compiler, "--version"], text=True),
        ])

    def test_hashes_follow_actual_sysconfig_and_libpython_bytes(self):
        before = self.record()
        self.config.write_bytes(b"# changed target sysconfig, not executed\n")
        self.library.write_bytes(b"changed libpython fixture")
        after = self.record()
        for field, artifact in (("target_sysconfig_sha256", self.config),
                                ("target_libpython_sha256", self.library)):
            self.assertNotEqual(before[field], after[field])
            self.assertEqual(after[field], sha256(artifact))

    def test_wrong_deb_rejected_before_target_description_or_sysconfig_use(self):
        self.deb.write_bytes(b"wrong downloaded deb")
        with mock.patch.object(android_target, "describe_python") as describe:
            with self.assertRaisesRegex(ValueError, "deb differs from independently pinned input"):
                self.record()
        describe.assert_not_called()
        self.host_version.assert_not_called()
        self.metadata.assert_not_called()
        self.commands.assert_not_called()

    def test_termux_deb_environment_is_required(self):
        del os.environ["TERMUX_DEB"]
        with mock.patch.object(android_target, "describe_python") as describe:
            with self.assertRaises(KeyError):
                self.record()
        describe.assert_not_called()
        self.commands.assert_not_called()

    def test_termux_deb_must_point_to_an_actual_file(self):
        os.environ["TERMUX_DEB"] = str(self.directory / "nonexistent.deb")
        with mock.patch.object(android_target, "describe_python") as describe:
            with self.assertRaises(FileNotFoundError):
                self.record()
        describe.assert_not_called()
        self.commands.assert_not_called()

    def test_wrong_resolved_target_minor_is_rejected(self):
        wrong_prefix = self.directory / "wrong-minor"
        self.make_prefix(wrong_prefix, minor="3.13")
        with self.assertRaisesRegex(ValueError, "resolved target Python minor"):
            android_target.record(self.target, wrong_prefix, self.compiler)
        self.host_version.assert_not_called()
        self.metadata.assert_not_called()
        self.commands.assert_not_called()

    def test_wrong_host_patch_is_rejected_even_for_matching_minor(self):
        self.host_version.return_value = "3.14.9"
        with self.assertRaisesRegex(ValueError, "build interpreter patch version"):
            self.record()
        self.metadata.assert_not_called()
        self.commands.assert_not_called()

    def test_wrong_or_missing_resolved_ndk_is_rejected(self):
        for revision in ("29.0.14206865", "30.0.16248371", None):
            with self.subTest(revision=revision):
                if revision is None:
                    os.environ.pop("NDK_VERSION", None)
                else:
                    os.environ["NDK_VERSION"] = revision
                with self.assertRaisesRegex(ValueError, "resolved NDK revision"):
                    self.record()
        self.metadata.assert_not_called()
        self.commands.assert_not_called()

    def test_sysconfig_must_be_unambiguous(self):
        second = self.config.with_name("_sysconfigdata_duplicate.py")
        second.write_bytes(b"# duplicate\n")
        with self.assertRaisesRegex(ValueError, "expected one target sysconfig file"):
            self.record()
        second.unlink()
        self.config.unlink()
        with self.assertRaisesRegex(ValueError, "expected one target sysconfig file"):
            self.record()
        self.commands.assert_not_called()

    def test_git_failure_uses_github_commit_without_executing_other_commands(self):
        os.environ["GITHUB_SHA"] = "abcdef0123456789abcdef0123456789abcdef01"

        def output(argv, *, text):
            if argv == ["git", "rev-parse", "HEAD"]:
                raise subprocess.CalledProcessError(128, argv)
            return self.command_output(argv, text=text)

        self.commands.side_effect = output
        self.assertEqual(self.record()["commit"], os.environ["GITHUB_SHA"])

    def test_missing_maturin_omits_tool_and_skips_rust_commands(self):
        def version(name):
            if name == "maturin":
                raise android_target.importlib.metadata.PackageNotFoundError(name)
            return self.tools[name]

        self.metadata.side_effect = version
        data = self.record()
        self.assertEqual(data["build_tools"],
                         {name: value for name, value in self.tools.items() if name != "maturin"})
        self.assertEqual(data["rust"], {})
        self.assertEqual(self.commands.call_args_list, [
            mock.call(["git", "rev-parse", "HEAD"], text=True),
            mock.call([self.compiler, "--version"], text=True),
        ])

    def test_record_command_writes_provenance_json_from_actual_fixtures(self):
        target_file = self.directory / "target.json"
        target_file.write_text(json.dumps(self.target))
        output = self.directory / "provenance.json"
        self.assertEqual(android_target.main([
            "--target", str(target_file), "record", "--prefix", str(self.prefix),
            "--compiler", self.compiler, "--out", str(output),
        ]), 0)
        self.assertTrue(output.read_text().endswith("\n"))
        self.assertEqual(json.loads(output.read_text()), self.record())


if __name__ == "__main__":
    unittest.main()
