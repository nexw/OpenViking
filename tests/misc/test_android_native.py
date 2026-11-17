"""Standalone Android packaging tests: stdlib, ZIP/ELF fixtures, no builds.

Run with: python -m unittest discover -s tests/misc -p test_android_native.py
"""

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts.build_support import android_native


def elf_stub(machine=183, *, needed="libpython3.14.so", symbol="PyInit_ragfs_python",
             elf_type=3, exported=True):
    """ELF header plus deterministic readelf-mock annotations, never executed."""
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[16:18] = elf_type.to_bytes(2, "little")
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header) + f"\nNEEDED={needed}\nSYMBOL={symbol}\nEXPORTED={int(exported)}\n".encode()


def readelf_stub(args, readelf="readelf"):
    payload = Path(args[-1]).read_bytes()
    fields = dict(line.split("=", 1) for line in payload[64:].decode().splitlines() if "=" in line)
    if args[0] == "-h":
        machine = "AArch64" if payload[18:20] == b"\xb7\x00" else "X86-64"
        kind = "DYN" if payload[16:18] == b"\x03\x00" else "EXEC"
        return f"Class: ELF64\nType: {kind}\nMachine: {machine}\n"
    if args[0] == "-d":
        return "\n".join(f"0 (NEEDED) Shared library: [{lib}]"
                         for lib in fields["NEEDED"].split(",") if lib)
    index = "1" if fields["EXPORTED"] == "1" else "UND"
    return f"1: 00000100 24 FUNC GLOBAL DEFAULT {index} {fields['SYMBOL']}\n"


class AndroidWheelCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)
        self.readelf = mock.patch.object(android_native, "_readelf", side_effect=readelf_stub).start()
        self.addCleanup(mock.patch.stopall)
        self.env = mock.patch.dict(os.environ, {"TERMUX_PY_VERSION": "3.14", "ANDROID_API_LEVEL": "24"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_wheel(self, members, *, project="ragfs_python", platform=None,
                    python_tag="cp314", abi_tag="cp314", internal_tags=None):
        platform = platform or android_native.ANDROID_PLATFORM_TAG
        tag = f"{python_tag}-{abi_tag}-{platform}"
        name = f"{project}-0.1.0-{tag}.whl"
        members = dict(members)
        metadata = f"{project}-0.1.0.dist-info/WHEEL"
        tags = [tag] if internal_tags is None else internal_tags
        members.setdefault(metadata, "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n" +
                           "".join(f"Tag: {value}\n" for value in tags))
        with zipfile.ZipFile(self.dist / name, "w") as archive:
            for member, payload in members.items():
                archive.writestr(member, payload)
        return name

    def test_maturin_package_layout(self):
        name = self.write_wheel({
            "ragfs_python/__init__.py": b"from .ragfs_python import *\n",
            "ragfs_python/ragfs_python.abi3.so": elf_stub(),
            "ragfs_python-0.1.0.dist-info/METADATA": b"Name: ragfs-python\n",
            "ragfs_python-0.1.0.dist-info/RECORD": b"",
            "ragfs_python-0.1.0.dist-info/sboms/ragfs-python.cyclonedx.json": b"{}",
        })
        self.assertEqual(android_native.wheel_check(self.dist), [name])

    def test_top_level_layout(self):
        name = self.write_wheel({"ragfs_python.abi3.so": elf_stub()})
        self.assertEqual(android_native.wheel_check(self.dist), [name])

    def full_payload(self):
        return {
            android_native.CLI_ARTIFACT: elf_stub(needed="", symbol="", elf_type=2),
            android_native.RAGFS_ARTIFACT: elf_stub(),
            android_native.ENGINE_ARTIFACT: elf_stub(symbol="PyInit__native"),
        }

    def test_full_openviking_wheel_static_cli_is_not_a_python_extension(self):
        name = self.write_wheel(self.full_payload(), project="openviking")
        self.assertEqual(android_native.wheel_check(self.dist), [name])

    def test_missing_openviking_payload(self):
        self.write_wheel({android_native.CLI_ARTIFACT: elf_stub()}, project="openviking")
        with self.assertRaisesRegex(android_native.VerificationError, "missing wheel members"):
            android_native.wheel_check(self.dist)

    def test_unrelated_extensions_are_not_accepted(self):
        for member in (
            "unrelated/ragfs_python.abi3.so",
            "ragfs_python/nested/ragfs_python.abi3.so",
            "ragfs_python/unrelated.so",
            "ragfs_python_fake.abi3.so",
        ):
            with self.subTest(member=member):
                self.write_wheel({member: elf_stub()})
                with self.assertRaisesRegex(android_native.VerificationError, "no ragfs_python"):
                    android_native.wheel_check(self.dist)

    def test_wrong_architecture_is_rejected_in_both_layouts(self):
        for member in ("ragfs_python.abi3.so", "ragfs_python/ragfs_python.abi3.so"):
            with self.subTest(member=member):
                self.write_wheel({member: elf_stub(machine=62)})
                with self.assertRaisesRegex(android_native.VerificationError, "not an AArch64 ELF"):
                    android_native.wheel_check(self.dist)

    def test_non_elf_and_wrong_class_or_endianness_are_rejected(self):
        original = elf_stub()
        for payload in (b"not ELF", original[:4] + b"\x01\x01" + original[6:],
                        original[:5] + b"\x02" + original[6:]):
            with self.subTest(payload=payload[:6]):
                self.write_wheel({"ragfs_python.abi3.so": payload})
                with self.assertRaisesRegex(android_native.VerificationError, "not an AArch64 ELF"):
                    android_native.wheel_check(self.dist)

    def test_glibc_wheel_tag_is_rejected(self):
        self.write_wheel({"ragfs_python/ragfs_python.abi3.so": elf_stub()},
                         platform="manylinux_2_17_aarch64")
        with self.assertRaisesRegex(android_native.VerificationError, "Android/bionic"):
            android_native.wheel_check(self.dist)

    def test_broad_abi3_filename_is_rejected(self):
        self.write_wheel({"ragfs_python.abi3.so": elf_stub()}, python_tag="cp310", abi_tag="abi3")
        with self.assertRaisesRegex(android_native.VerificationError, "exact-minor cp314-cp314"):
            android_native.wheel_check(self.dist)

    def test_internal_metadata_must_match_filename_exactly(self):
        for tags in (["cp310-abi3-android_24_arm64_v8a"], [],
                     ["cp314-cp314-android_24_arm64_v8a", "cp310-abi3-android_24_arm64_v8a"],
                     ["cp314-cp314-android_28_arm64_v8a"]):
            with self.subTest(tags=tags):
                self.write_wheel({"ragfs_python.abi3.so": elf_stub()}, internal_tags=tags)
                with self.assertRaisesRegex(android_native.VerificationError, "internal WHEEL tags"):
                    android_native.wheel_check(self.dist)

    def test_missing_internal_metadata_is_rejected(self):
        name = "ragfs_python-0.1.0-cp314-cp314-android_24_arm64_v8a.whl"
        with zipfile.ZipFile(self.dist / name, "w") as archive:
            archive.writestr("ragfs_python.abi3.so", elf_stub())
        with self.assertRaisesRegex(android_native.VerificationError, "internal WHEEL metadata"):
            android_native.wheel_check(self.dist)

    def test_missing_or_different_libpython_minor_is_rejected(self):
        for needed in ("", "libpython3.13.so", "libpython3.14.so,libpython3.13.so"):
            with self.subTest(needed=needed):
                self.write_wheel({"ragfs_python.abi3.so": elf_stub(needed=needed)})
                with self.assertRaisesRegex(android_native.VerificationError, "libpython"):
                    android_native.wheel_check(self.dist)

    def test_glibc_payload_cannot_be_relabelled_android(self):
        self.write_wheel({"ragfs_python.abi3.so": elf_stub(needed="libpython3.14.so,libc.so.6")})
        with self.assertRaisesRegex(android_native.VerificationError, "linked against glibc"):
            android_native.wheel_check(self.dist)

    def test_missing_or_undefined_pyinit_is_rejected(self):
        for kwargs in ({"symbol": "PyInit_wrong"}, {"exported": False},
                       {"symbol": "PyInit_ragfs_python_fake"}):
            with self.subTest(kwargs=kwargs):
                self.write_wheel({"ragfs_python.abi3.so": elf_stub(**kwargs)})
                with self.assertRaisesRegex(android_native.VerificationError, "exported symbol"):
                    android_native.wheel_check(self.dist)

    def test_every_extension_is_checked_not_only_required_one(self):
        self.write_wheel({"ragfs_python.abi3.so": elf_stub(),
                          "ragfs_python/extra.so": elf_stub(needed="libpython3.13.so", symbol="PyInit_extra")})
        with self.assertRaisesRegex(android_native.VerificationError, "extra.so.*libpython3.13"):
            android_native.wheel_check(self.dist)

    def test_api28_propagates_from_explicit_arguments_and_environment(self):
        name = self.write_wheel({"ragfs_python.abi3.so": elf_stub()}, platform="android_28_arm64_v8a")
        self.assertEqual(android_native.wheel_check(self.dist, "3.14", "28"), [name])
        with mock.patch.dict(os.environ, {"ANDROID_API_LEVEL": "28"}):
            self.assertEqual(android_native.wheel_check(self.dist), [name])
        with self.assertRaisesRegex(android_native.VerificationError, "android_24"):
            android_native.wheel_check(self.dist)

    def test_checker_defaults_are_cp314_and_api24_without_environment(self):
        name = self.write_wheel({"ragfs_python.abi3.so": elf_stub()})
        with mock.patch.dict(os.environ):
            os.environ.pop("TERMUX_PY_VERSION", None)
            os.environ.pop("ANDROID_API_LEVEL", None)
            self.assertEqual(android_native.wheel_check(self.dist), [name])

    def test_python_minor_argument_controls_tags_and_needed_library(self):
        name = self.write_wheel({"ragfs_python.abi3.so": elf_stub(needed="libpython3.13.so")},
                                python_tag="cp313", abi_tag="cp313")
        self.assertEqual(android_native.wheel_check(self.dist, "3.13", "24"), [name])

    def test_cli_options_reach_checker(self):
        with mock.patch.object(android_native, "wheel_check", return_value=[]) as check:
            self.assertEqual(android_native.main(["wheel-check", "--dist", str(self.dist),
                             "--python-version", "3.14", "--android-api", "28", "--readelf", "llvm-readelf"]), 0)
            check.assert_called_once_with(self.dist, "3.14", "28", "llvm-readelf")

    def test_invalid_target_options_are_rejected(self):
        for version, api in (("3.14.10", "24"), ("3.14", "23"), ("3.14", "bad")):
            with self.subTest(version=version, api=api):
                with self.assertRaises(android_native.VerificationError):
                    android_native.wheel_check(self.dist, version, api)

    def test_empty_dist_is_rejected(self):
        with self.assertRaisesRegex(android_native.VerificationError, "no wheels"):
            android_native.wheel_check(self.dist)

    def test_bundle_verify_and_install_keep_target_version_and_api(self):
        root = self.dist / "source"
        for relative, payload in self.full_payload().items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        with mock.patch.object(android_native, "git_commit", return_value="test-commit"):
            archive = android_native.bundle(root, self.dist / "bundle", version="0.1.0",
                                            android_api="28", python_version="3.14")
        manifest = json.loads((archive.parent / "manifest.json").read_text())
        self.assertEqual(manifest["abi"], "cp314-cp314")
        self.assertEqual(manifest["android_api"], "28")
        self.assertEqual(manifest["termux_python"], "3.14")
        restored = self.dist / "restored"
        self.assertEqual(android_native.install(archive, restored, ignore_arch=True), list(android_native.ARTIFACTS))
        for relative in android_native.ARTIFACTS:
            self.assertEqual((root / relative).read_bytes(), (restored / relative).read_bytes())

    def test_unlinked_prototype_verifier_remains_usable_without_python_argument(self):
        root = self.dist / "prototype"
        path = root / android_native.RAGFS_ARTIFACT
        path.parent.mkdir(parents=True)
        path.write_bytes(elf_stub(needed=""))
        android_native.verify_artifact(android_native.RAGFS_ARTIFACT, root)


if __name__ == "__main__":
    unittest.main()
