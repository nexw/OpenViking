"""Standalone Android packaging tests (no application dependencies required).

Run with: python -m unittest discover -s tests/misc -p test_android_native.py
"""

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_support import android_native


def elf_stub(machine=183):
    """Minimal ELF header for the wheel payload's architecture check."""
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


class AndroidWheelCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)

    def write_wheel(self, members, *, project="ragfs_python", platform=None):
        platform = platform or android_native.ANDROID_PLATFORM_TAG
        name = f"{project}-0.1.0-cp310-abi3-{platform}.whl"
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
            "ragfs_python-0.1.0.dist-info/WHEEL": b"",
            "ragfs_python-0.1.0.dist-info/sboms/ragfs-python.cyclonedx.json": b"{}",
        })
        self.assertEqual(android_native.wheel_check(self.dist), [name])

    def test_top_level_layout(self):
        name = self.write_wheel({"ragfs_python.abi3.so": elf_stub()})
        self.assertEqual(android_native.wheel_check(self.dist), [name])

    def test_full_openviking_wheel(self):
        name = self.write_wheel(
            dict.fromkeys(android_native.ARTIFACTS, elf_stub()), project="openviking"
        )
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

    def test_glibc_wheel_tag_is_rejected(self):
        self.write_wheel(
            {"ragfs_python/ragfs_python.abi3.so": elf_stub()},
            platform="manylinux_2_17_aarch64",
        )
        with self.assertRaisesRegex(android_native.VerificationError, "Android/bionic"):
            android_native.wheel_check(self.dist)

    def test_empty_dist_is_rejected(self):
        with self.assertRaisesRegex(android_native.VerificationError, "no wheels"):
            android_native.wheel_check(self.dist)


if __name__ == "__main__":
    unittest.main()
