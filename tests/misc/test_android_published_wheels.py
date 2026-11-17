"""Offline published-wheel mirror contracts; never build/install/execute a wheel.

Only tag integration tests need packaging and skip if it is unavailable. Atomic
batch and native-payload tests use stdlib fixtures/mocks, including on bare CI.
"""
import hashlib
import importlib.util
import json
import stat
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts.build_support import android_published_wheels as published

TARGET = {"schema": 1, "python_minor": "3.14", "build_python": "3.14.10",
          "architecture": "aarch64", "minimum_android_api": 24}
HAS_PACKAGING = importlib.util.find_spec("packaging") is not None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def selection(filename="demo-1.0-py3-none-any.whl", **extra):
    return {"name": "demo", "version": "1.0", "filename": filename,
            "url": f"https://files.example.invalid/{filename}", "sha256": "a" * 64,
            "requires_python": ">=3.10", **extra}


def lock_for(*sources):
    return {"schema": 1, "python_minor": "3.14", "android_api": 24,
            "architecture": "aarch64", "wheels": list(sources),
            "missing": [{"name": "not-published", "version": "2.0"}]}


def wheel_fixture(path, *, tag="py3-none-any", pure=True, metadata=None, extra=None):
    dist = "demo-1.0.dist-info"
    files = {
        "demo/__init__.py": b"raise RuntimeError('must never execute this wheel')\n",
        f"{dist}/METADATA": (metadata or "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
                              "Requires-Python: >=3.10\n\n").encode(),
        f"{dist}/WHEEL": ("Wheel-Version: 1.0\nRoot-Is-Purelib: "
                             + str(pure).lower() + "\nTag: " + tag + "\n\n").encode(),
        f"{dist}/licenses/LICENSE": b"authentic fixture license preserved unchanged\n",
        f"{dist}/RECORD": b"",
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


def elf_fixture(machine=b"\xb7\x00", endian=1):
    # Header fixture for static inspection ONLY; not a runnable library.
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02" + bytes([endian, 1])
    header[18:20] = machine
    return bytes(header)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        guard = mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network"))
        guard.start()
        self.addCleanup(guard.stop)
        guard = mock.patch.object(published.subprocess, "check_output",
                                  side_effect=AssertionError("unexpected external command"))
        self.command = guard.start()
        self.addCleanup(guard.stop)


class AtomicBatchTests(OfflineCase):
    """These exercise real batch/copy/hash logic without importing packaging."""
    def setUp(self):
        super().setUp()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.output = self.root / "out"
        self.source = selection()
        self.wheel = wheel_fixture(self.cache / self.source["filename"])
        self.source["sha256"] = digest(self.wheel.read_bytes())
        self.lock = self.root / "lock.json"
        self.target = self.root / "target.json"
        self.target.write_text(json.dumps(TARGET))
        self.write_lock(lock_for(self.source))
        fake_packaging = types.ModuleType("packaging")
        fake_packaging.__version__ = "unittest-fixture"
        patcher = mock.patch.dict(sys.modules, {"packaging": fake_packaging})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("supported_tags", frozenset({"fixture-tag"})),
                            ("validate_lock", None), ("source_commit", "f" * 40)):
            patcher = mock.patch.object(published, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(published, "inspect_published_wheel",
                                    side_effect=self.fake_inspection)
        self.inspect = patcher.start()
        self.addCleanup(patcher.stop)

    def fake_inspection(self, wheel, source, target, tags, readelf):
        self.assertFalse(self.output.exists() and any(self.output.iterdir()))
        return {"wheel": wheel.name, "sha256": source["sha256"], "source": source}

    def write_lock(self, value):
        self.lock.write_text(json.dumps(value))

    def run_mirror(self):
        return published.mirror(self.lock, self.target, self.cache, self.output)

    def test_success_preserves_bytes_license_and_records_subset_provenance(self):
        self.output.mkdir()  # Empty output is supported, not just a missing directory.
        before = self.wheel.read_bytes()
        result = self.run_mirror()
        self.assertEqual((self.output / self.wheel.name).read_bytes(), before)
        self.assertEqual(result["lock_sha256"], digest(self.lock.read_bytes()))
        self.assertEqual(result["source_commit"], "f" * 40)
        self.assertEqual(result["target_tag_profile"]["build_python"], "3.14.10")
        self.assertEqual(result["packaging_version"], "unittest-fixture")
        self.assertTrue(result["host_python"])
        self.assertEqual(result["missing"], [{"name": "not-published", "version": "2.0"}])
        self.assertEqual(result["stage"], "published-android-wheel-subset")
        self.assertEqual(json.loads((self.output / "build-manifest.json").read_text()), result)
        self.assertEqual({p.name for p in self.output.iterdir()},
                         {self.wheel.name, "build-manifest.json"})
        self.assertFalse(list(self.root.glob(".android-published-*")))

    def test_second_wheel_failure_never_publishes_first_or_manifest(self):
        other = selection("second-2.0-py3-none-any.whl", name="second", version="2.0")
        payload = self.cache / other["filename"]
        payload.write_bytes(b"second fixture, not a wheel")
        other["sha256"] = digest(payload.read_bytes())
        self.write_lock(lock_for(self.source, other))
        self.output.mkdir()
        self.inspect.side_effect = [self.fake_inspection(self.wheel, self.source, TARGET, (), ""),
                                    ValueError("second wheel invalid")]
        with self.assertRaisesRegex(ValueError, "second wheel invalid"):
            self.run_mirror()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue(self.wheel.exists())
        self.assertTrue(payload.exists())  # Verified downloads may remain cached.
        self.assertFalse(list(self.root.glob(".android-published-*")))

    def test_stale_output_rejected_before_fetch(self):
        self.output.mkdir()
        stale = self.output / "stale.whl"
        stale.write_bytes(b"keep untouched")
        with mock.patch.object(published, "fetch_source") as fetch:
            with self.assertRaisesRegex(ValueError, "output directory"):
                self.run_mirror()
        fetch.assert_not_called()
        self.assertEqual(stale.read_bytes(), b"keep untouched")

    def test_whole_lock_preflight_happens_before_any_fetch(self):
        self.validate_lock.side_effect = ValueError("bad final source")
        with mock.patch.object(published, "fetch_source") as fetch:
            with self.assertRaisesRegex(ValueError, "bad final source"):
                self.run_mirror()
        fetch.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_wrong_download_hash_fails_before_any_zip_inspection(self):
        self.source["sha256"] = "0" * 64
        self.write_lock(lock_for(self.source))
        with mock.patch("urllib.request.urlopen") as download:
            download.return_value.__enter__.return_value.read.return_value = b"wrong bytes"
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                self.run_mirror()
        self.inspect.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual(download.call_count, 3)

    def test_output_cannot_contain_cache_or_be_symlink(self):
        with self.assertRaisesRegex(ValueError, "cache must be outside"):
            published.mirror(self.lock, self.target, self.output / "cache", self.output)
        empty = self.root / "empty"
        empty.mkdir()
        self.output.symlink_to(empty, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "output directory"):
            self.run_mirror()

    def test_copy_change_aborts_manifest_and_missing_queue_never_becomes_wheels(self):
        with mock.patch.object(published.shutil, "copyfile", side_effect=lambda src, dst:
                               Path(dst).write_bytes(b"changed during copy")):
            with self.assertRaisesRegex(ValueError, "changed during"):
                self.run_mirror()
        self.assertFalse(self.output.exists())


class NativePayloadTests(OfflineCase):
    def inspect(self, files, *, pure=False, dynamic=""):
        path = self.root / "payload.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in files.items():
                archive.writestr(name, payload)
        self.command.side_effect = None
        self.command.return_value = dynamic
        with zipfile.ZipFile(path) as archive:
            result = published.inspect_payload(archive, pure=pure, target=TARGET,
                                               readelf="fixture-readelf")
        return result

    def test_pure_wheel_cannot_hide_elf_or_native_named_payload(self):
        for name, payload in (("demo/data", elf_fixture()), ("demo/lib.so", b"not ELF"),
                              ("demo/file", b"MZnative"), ("demo/x.dll", b"x")):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "pure any wheel contains native"):
                    self.inspect({name: payload}, pure=True)
        self.command.assert_not_called()

    def test_android_auxiliary_so_and_versioned_so_do_not_require_pyinit_or_libpython(self):
        result = self.inspect({"demo.libs/libaux.so.1": elf_fixture(),
                               "demo/data/unnamed": elf_fixture()},
                              dynamic=" 0x01 (NEEDED) Shared library: [libc.so]\n")
        self.assertEqual(result, {"demo.libs/libaux.so.1": ["libc.so"],
                                  "demo/data/unnamed": ["libc.so"]})
        self.assertEqual(self.command.call_count, 2)
        for call in self.command.call_args_list:
            self.assertEqual(call.args[0][:2], ["fixture-readelf", "-d"])
            self.assertFalse(Path(call.args[0][2]).exists())

    def test_wrong_arch_endian_or_nonelf_native_rejected(self):
        for payload in (elf_fixture(machine=b"\x3e\x00"), elf_fixture(endian=2),
                        b"not an ELF", b"\x7fELF"):
            with self.subTest(payload=payload[:7]):
                with self.assertRaisesRegex(ValueError, "ELF64/little-endian/AArch64"):
                    self.inspect({"demo/native.so": payload})
        self.command.assert_not_called()

    def test_glibc_needed_and_loader_rejected(self):
        for library in ("libc.so.6", "libpthread.so.0", "libstdc++.so.6",
                        "ld-linux-aarch64.so.1"):
            with self.subTest(library=library):
                with self.assertRaisesRegex(ValueError, "glibc DT_NEEDED"):
                    self.inspect({"libaux.so": elf_fixture()},
                                 dynamic=f" 0x01 (NEEDED) Shared library: [{library}]\n")

    def test_versioned_python_dependency_must_match_target_minor_not_host(self):
        for library in ("libpython3.13.so", "libpython3.14t.so", "libpython3.14d.so.1.0"):
            with self.subTest(library=library):
                with self.assertRaisesRegex(ValueError, "mismatched versioned libpython"):
                    self.inspect({"libaux.so": elf_fixture()},
                                 dynamic=f" 0x01 (NEEDED) Shared library: [{library}]\n")
        self.assertEqual(self.inspect({"libaux.so": elf_fixture()},
                                     dynamic="(NEEDED) [libpython3.14.so]\n")
                         ["libaux.so"], ["libpython3.14.so"])

    def test_linux_cpython_extension_suffix_rejected_even_with_android_elf(self):
        with self.assertRaisesRegex(ValueError, "foreign CPython extension suffix"):
            self.inspect({"demo.cpython-314-aarch64-linux-gnu.so": elf_fixture()})
        self.command.assert_not_called()


@unittest.skipUnless(HAS_PACKAGING, "packaging is optional for standalone helper tests")
class PackagingIntegrationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        # Old host packaging without PEP 738 also cannot run actual mirror jobs.
        from packaging import tags
        if not hasattr(tags, "android_platforms"):
            self.skipTest("packaging with Android tags is required for tag integration")
        self.allowed = published.supported_tags(TARGET)

    def source_wheel(self, *, tag="py3-none-any", **kwargs):
        filename = f"demo-1.0-{tag}.whl"
        wheel = wheel_fixture(self.root / filename, tag=tag, **kwargs)
        return wheel, selection(filename, sha256=digest(wheel.read_bytes()))

    def inspect(self, wheel, source):
        return published.inspect_published_wheel(wheel, source, TARGET, self.allowed)

    def test_supported_tags_accept_exact_cpython_older_abi3_and_pure_not_linux(self):
        for tag in ("cp314-cp314-android_24_arm64_v8a", "cp39-abi3-android_24_arm64_v8a",
                    "cp314-cp314-android_21_arm64_v8a", "py3-none-android_24_arm64_v8a",
                    "py3-none-any", "py2.py3-none-any"):
            with self.subTest(tag=tag):
                wheel, source = self.source_wheel(tag=tag, pure=tag.endswith("any"))
                published.validate_lock(lock_for(source), TARGET, self.allowed)
                self.assertEqual(self.inspect(wheel, source)["wheel"], wheel.name)
        for tag in ("cp314-cp314-manylinux_2_17_aarch64", "cp314-cp314-linux_aarch64",
                    "cp314-cp314-android_25_arm64_v8a", "cp313-cp313-android_24_arm64_v8a",
                    "cp314-cp314-android_24_x86_64"):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(ValueError, "incompatible published wheel tag"):
                    published.validate_lock(lock_for(selection(f"demo-1.0-{tag}.whl")),
                                            TARGET, self.allowed)

    def test_exact_internal_tags_and_name_version_metadata_required(self):
        wheel, source = self.source_wheel(extra={
            "demo-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n"
                                        b"Tag: py2-none-any\n\n"})
        with self.assertRaisesRegex(ValueError, "internal WHEEL tags disagree"):
            self.inspect(wheel, source)
        for metadata in ("Name: wrong\nVersion: 1.0\n", "Name: demo\nVersion: 2.0\n"):
            wheel, source = self.source_wheel(metadata=metadata)
            with self.assertRaisesRegex(ValueError, "metadata disagrees"):
                self.inspect(wheel, source)
        wheel, source = self.source_wheel()
        source["name"] = "wrong"
        with self.assertRaisesRegex(ValueError, "filename disagrees"):
            self.inspect(wheel, source)

    def test_requires_python_checked_against_target_patch_in_lock_and_metadata(self):
        wheel, source = self.source_wheel()
        for requirement in (">=3.15", "<3.14.10", "!=3.14.10"):
            with self.subTest(requirement=requirement):
                with self.assertRaisesRegex(ValueError, "excludes target 3.14.10"):
                    published.validate_lock(lock_for({**source, "requires_python": requirement}),
                                            TARGET, self.allowed)
        wheel, source = self.source_wheel(metadata="Name: demo\nVersion: 1.0\n"
                                         "Requires-Python: >=3.15\n\n")
        with self.assertRaisesRegex(ValueError, "excludes target"):
            self.inspect(wheel, source)

    def test_target_lock_mismatch_duplicate_names_and_unpinned_sources_fail(self):
        _, source = self.source_wheel()
        for key, value in (("python_minor", "3.13"), ("android_api", 25),
                           ("architecture", "x86_64"), ("schema", 2)):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "lock and target profile disagree"):
                    published.validate_lock({**lock_for(source), key: value}, TARGET, self.allowed)
        with self.assertRaisesRegex(ValueError, "duplicate locked distribution"):
            published.validate_lock(lock_for(source, source), TARGET, self.allowed)
        for change in ({"sha256": "bad"}, {"url": "https://example.invalid/renamed.whl"},
                       {"url": source["url"].replace("https:", "http:")},
                       {"filename": "../" + source["filename"]}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    published.validate_lock(lock_for({**source, **change}), TARGET, self.allowed)

    def test_hash_checked_before_inspection_and_exact_filename_not_renamed(self):
        wheel, source = self.source_wheel()
        with mock.patch.object(published, "inspect_metadata") as metadata:
            with self.assertRaisesRegex(ValueError, "checksum mismatch before ZIP"):
                self.inspect(wheel, {**source, "sha256": "0" * 64})
        metadata.assert_not_called()
        with self.assertRaisesRegex(ValueError, "filename differs"):
            self.inspect(wheel, {**source, "filename": "renamed-1.0-py3-none-any.whl"})

    def test_unsafe_members_symlinks_and_crc_corruption_fail(self):
        for name in ("../escape", "/absolute", "demo\\escape"):
            wheel, source = self.source_wheel(extra={name: b"unsafe"})
            with self.assertRaisesRegex(ValueError, "unsafe wheel ZIP member"):
                self.inspect(wheel, source)
        wheel, source = self.source_wheel()
        with zipfile.ZipFile(wheel, "a") as archive:
            symlink = zipfile.ZipInfo("demo/link")
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(symlink, "target")
        source["sha256"] = digest(wheel.read_bytes())
        with self.assertRaisesRegex(ValueError, "unsafe wheel ZIP member"):
            self.inspect(wheel, source)
        wheel, source = self.source_wheel()
        original = wheel.read_bytes()
        changed = original.replace(b"authentic fixture license", b"corrupted fixture license", 1)
        # Equal length corruption keeps ZIP offsets valid but invalidates the CRC.
        self.assertEqual(len(original), len(changed))
        wheel.write_bytes(changed)
        source["sha256"] = digest(changed)
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            self.inspect(wheel, source)

    def test_pure_any_disguised_elf_rejected_but_android_aux_library_allowed(self):
        wheel, source = self.source_wheel(extra={"demo/data": elf_fixture()})
        with self.assertRaisesRegex(ValueError, "pure any wheel contains native"):
            self.inspect(wheel, source)
        wheel, source = self.source_wheel(tag="py3-none-android_24_arm64_v8a", pure=False,
                                         extra={"demo.libs/libaux.so": elf_fixture()})
        self.command.side_effect = None
        self.command.return_value = "(NEEDED) [libc.so]\n"
        self.assertEqual(self.inspect(wheel, source)["needed"],
                         {"demo.libs/libaux.so": ["libc.so"]})


if __name__ == "__main__":
    unittest.main()
