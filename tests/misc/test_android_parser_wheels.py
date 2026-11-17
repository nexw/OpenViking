"""Offline, stdlib-only tests; run with unittest discover -s tests/misc -p test_android_parser_wheels.py."""

import hashlib
import importlib.util
import io
import json
import os
import runpy
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "android_parser_wheels", ROOT / "scripts/build_support/android_parser_wheels.py"
)
wheels = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wheels)

# Complete official version-tag snapshots replace incomplete PyPI sdists.
GIT_PINS = {
    "tree-sitter-python": ("293fdc02038ee2bf0e2e206711b69c90ac0d413f", "b74d8ba6730535354175436208fe1452fd464f235263959ca40b8bf32cb23dc6"),
    "tree-sitter-typescript": ("f975a621f4e7f532fe322e13c4f79495e0a7b2e7", "4de2e82e557810eecb93cb3b31fbb3bd28ba4c91ba9a6c6164bcaa074125b7b5"),
    "tree-sitter-java": ("94703d5a6bed02b98e438d7cad1136c01a60ba2c", "39dea56cc3bb5fb1658f79b195a92b6f08885886bca636adf2dc6243af641c47"),
    "tree-sitter-cpp": ("f41e1a044c8a84ea9fa8577fdd2eab92ec96de02", "ad4b9b56602e9b14082637988f0042521006819960ed8c308bc24ded9ceb9331"),
    "tree-sitter-rust": ("18b0515fca567f5a10aee9978c6d2640e878671a", "af3dc21a2985201f764eb94709f09277500fb732f1863f9bdd6371147ca7afe7"),
    "tree-sitter-c-sharp": ("362a8a41b265056592a0c3771664a21d23a71392", "f351b27b9b250c7c1938e9d245325b151844bb5f6533db82dbe9d5e8bf4f49e9"),
    "tree-sitter-php": ("1269143868747e3b8e50f221ca0a2e5a1a2f2b44", "49605cd2a0af1c7ee041abf2411df488ad1fea97c57ff1781bc7434d44a29e0a"),
    "tree-sitter-lua": ("10fe0054734eec83049514ea2e718b2a56acd0c9", "82c3ca5808de02addd9c7fb5275d89260c6557019aa6e40ca52c0595bf1d33cd"),
}


class ManifestTests(unittest.TestCase):
    def test_manifest_matches_project_parser_pins(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        dependencies = [dep for dep in project["project"]["dependencies"]
                        if dep.startswith("tree-sitter")
                        and not dep.startswith("tree-sitter-language-pack")]
        expected = dict(dep.split("==") for dep in dependencies)
        manifest = json.loads(wheels.DEFAULT_MANIFEST.read_text())
        self.assertEqual(manifest["schema"], 1)
        packages = manifest["packages"]
        self.assertEqual(len(packages), len(expected))
        self.assertEqual({p["name"]: p["version"] for p in packages}, expected)
        for package in packages:
            with self.subTest(package=package["name"]):
                self.assertRegex(package["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(package["url"].startswith("https://"))

    def test_known_git_sources_keep_exact_commit_and_checksum(self):
        packages = json.loads(wheels.DEFAULT_MANIFEST.read_text())["packages"]
        git_sources = {p["name"]: p for p in packages if "commit" in p}
        self.assertEqual(set(git_sources), set(GIT_PINS))
        for name, (commit, checksum) in GIT_PINS.items():
            with self.subTest(package=name):
                source = git_sources[name]
                self.assertEqual(source["commit"], commit)
                self.assertEqual(source["sha256"], checksum)
                owner = "tree-sitter-grammars" if name == "tree-sitter-lua" else "tree-sitter"
                self.assertEqual(source["url"], f"https://codeload.github.com/{owner}/{name}/tar.gz/{commit}")


class TemporaryTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)


class FetchTests(TemporaryTestCase):
    def setUp(self):
        super().setUp()
        self.payload = b"pinned source archive"
        self.source = {"name": "parser", "version": "1.0", "url": "https://example.invalid/source",
                       "sha256": hashlib.sha256(self.payload).hexdigest()}
        self.cache = self.directory / "cache"

    def test_download_verified_and_cached_without_network_on_reuse(self):
        with mock.patch.object(wheels.urllib.request, "urlopen", return_value=io.BytesIO(self.payload)) as request:
            archive = wheels.fetch_source(self.source, self.cache)
        request.assert_called_once_with(self.source["url"], timeout=120)
        self.assertEqual(archive.read_bytes(), self.payload)
        with mock.patch.object(wheels.urllib.request, "urlopen") as request:
            self.assertEqual(wheels.fetch_source(self.source, self.cache), archive)
        request.assert_not_called()

    def test_corrupt_cache_is_refetched_and_verified(self):
        self.cache.mkdir()
        archive = self.cache / "parser-1.0.tar.gz"
        archive.write_bytes(b"corrupt cache")
        with mock.patch.object(wheels.urllib.request, "urlopen", return_value=io.BytesIO(self.payload)) as request:
            self.assertEqual(wheels.fetch_source(self.source, self.cache), archive)
        request.assert_called_once()
        self.assertEqual(archive.read_bytes(), self.payload)

    def test_wrong_payload_is_never_cached(self):
        with mock.patch.object(wheels.urllib.request, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(b"wrong payload")) as request:
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                wheels.fetch_source(self.source, self.cache)
        self.assertEqual(request.call_count, 3)
        self.assertFalse((self.cache / "parser-1.0.tar.gz").exists())


class ExtractionTests(TemporaryTestCase):
    def make_tar(self, members):
        archive = self.directory / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for member, payload in members:
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
        return archive

    def test_valid_single_project_extracts(self):
        archive = self.make_tar([(tarfile.TarInfo("parser/pyproject.toml"), b"[build-system]\n")])
        destination = self.directory / "unpacked"
        self.assertEqual(wheels.unpack_source(archive, destination), destination / "parser")
        self.assertEqual((destination / "parser/pyproject.toml").read_bytes(), b"[build-system]\n")

    def test_traversal_and_escaping_links_rejected(self):
        outside = self.directory / "escaped"
        for kind in ("traversal", "symlink", "hardlink"):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo("../escaped" if kind == "traversal" else "parser/link")
                if kind != "traversal":
                    member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                    member.linkname = str(outside)
                archive = self.make_tar([(member, b"")])
                with self.assertRaises(tarfile.FilterError):
                    wheels.unpack_source(archive, self.directory / kind)
                self.assertFalse(outside.exists())

    def test_ambiguous_project_roots_rejected(self):
        archive = self.make_tar([(tarfile.TarInfo(f"{name}/pyproject.toml"), b"")
                                 for name in ("one", "two")])
        with self.assertRaisesRegex(ValueError, "expected one Python project"):
            wheels.unpack_source(archive, self.directory / "unpacked")


class RuntimePatchTests(TemporaryTestCase):
    def test_workaround_only_runs_for_aarch64_gcc(self):
        setup = self.directory / "setup.py"
        setup.write_text('def workaround(self):\n'
                         '    if machine().startswith("aarch64"):\n'
                         '        self.flags.append("-fno-early-inlining")\n')
        result = wheels.patch_runtime(self.directory)
        self.assertEqual(result["patch"], "gcc-only-early-inlining-workaround")
        self.assertEqual(result["setup_sha256"], hashlib.sha256(setup.read_bytes()).hexdigest())
        for machine, compiler, expected in (
            ("aarch64", ["gcc"], ["-fno-early-inlining"]),
            ("aarch64", ["/ndk/bin/aarch64-linux-android24-clang"], []),
            ("aarch64", ["ccache", "clang"], []),
            ("x86_64", ["gcc"], []),
        ):
            with self.subTest(machine=machine, compiler=compiler):
                namespace = {"machine": lambda: machine}
                exec(setup.read_text(), namespace)
                command = SimpleNamespace(compiler=SimpleNamespace(compiler=compiler), flags=[])
                namespace["workaround"](command)
                self.assertEqual(command.flags, expected)

    def test_unknown_or_duplicate_pattern_fails_without_changing_source(self):
        setup = self.directory / "setup.py"
        for text in ("# upstream changed\n", 'if machine().startswith("aarch64"):\n    pass\n' * 2):
            with self.subTest(text=text):
                setup.write_text(text)
                with self.assertRaisesRegex(ValueError, "review source patch"):
                    wheels.patch_runtime(self.directory)
                self.assertEqual(setup.read_text(), text)


class TargetEnvironmentTests(TemporaryTestCase):
    def test_target_config_replaces_host_flags_and_sets_android_platform(self):
        prefix = self.directory / "target prefix"
        include = prefix / "include/python3.14"
        library = prefix / "lib"
        config_dir = library / "python3.14"
        include.mkdir(parents=True)
        config_dir.mkdir(parents=True)
        for name in ("Python.h", "pyconfig.h"):
            (include / name).touch()
        (library / "libpython3.14.so").touch()
        dirty = {key: "/host/build -march=x86-64" for key in (
            "CC", "CXX", "LDSHARED", "BLDSHARED", "CFLAGS", "PY_CFLAGS", "PY_CFLAGS_NODIST",
            "LDFLAGS", "PY_LDFLAGS", "PY_LDFLAGS_NODIST", "INCLUDEPY", "CONFINCLUDEPY", "LIBDIR")}
        dirty.update(SOABI="cpython-314-aarch64-linux-android", ANDROID_API_LEVEL=35)
        (config_dir / "_sysconfigdata_target.py").write_text("build_time_vars = " + repr(dirty))
        output = self.directory / "config"
        with mock.patch.dict(os.environ, {"PYTHONHOME": "/host/python", "AR": "target-ar"}):
            env = wheels.target_environment(prefix, "3.14", 24, "target-clang", output)
        config = runpy.run_path(str(output / "ov_android_sysconfig.py"))["build_time_vars"]
        for key in dirty.keys() - {"SOABI", "ANDROID_API_LEVEL"}:
            with self.subTest(key=key):
                self.assertNotIn("/host/", config[key])
                self.assertEqual(env[key], str(config[key]))
        self.assertEqual(config["CC"], "target-clang")
        self.assertEqual(config["LDSHARED"], "target-clang -shared")
        self.assertEqual(config["INCLUDEPY"], str(include))
        self.assertEqual(config["LIBDIR"], str(library))
        self.assertIn("-lpython3.14", config["LDFLAGS"])
        self.assertIn("--no-undefined", config["LDFLAGS"])
        self.assertEqual(config["PY_CFLAGS_NODIST"], "")
        self.assertEqual(config["PY_LDFLAGS_NODIST"], "")
        self.assertEqual(config["ANDROID_API_LEVEL"], 24)
        self.assertEqual(env["ANDROID_API_LEVEL"], "24")
        self.assertEqual(env["_PYTHON_HOST_PLATFORM"], "android-aarch64")
        self.assertEqual(env["_PYTHON_SYSCONFIGDATA_NAME"], "ov_android_sysconfig")
        self.assertEqual(env["PYTHONPATH"], str(output))
        self.assertEqual(env["AR"], "target-ar")
        self.assertNotIn("PYTHONHOME", env)


class WheelValidationTests(TemporaryTestCase):
    tag = "cp314-cp314-android_24_arm64_v8a"
    dynamic = " 0x1 (NEEDED) Shared library: [libpython3.14.so]\n 0x1 (NEEDED) Shared library: [libc.so]\n"
    symbols = " 1: 00001000 12 FUNC GLOBAL DEFAULT 10 PyInit__binding\n"

    def make_wheel(self, *, tags=None, machine=183, extension="parser/_binding.cpython-314-aarch64-linux-android.so"):
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = machine.to_bytes(2, "little")
        wheel = self.directory / f"parser-1.0-{self.tag}.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("parser-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n" + "".join(
                f"Tag: {tag}\n" for tag in ([self.tag] if tags is None else tags)))
            archive.writestr(extension, header)
        return wheel

    def test_valid_wheel_records_dependencies_and_checksum(self):
        wheel = self.make_wheel()
        with mock.patch.object(wheels.subprocess, "check_output", side_effect=[self.dynamic, self.symbols]) as readelf:
            result = wheels.inspect_wheel(wheel, "3.14", 24, "target-readelf")
        self.assertEqual(result["sha256"], hashlib.sha256(wheel.read_bytes()).hexdigest())
        self.assertEqual(result["wheel"], wheel.name)
        self.assertEqual(list(result["needed"].values()), [["libpython3.14.so", "libc.so"]])
        self.assertEqual([call.args[0][1:-1] for call in readelf.call_args_list],
                         [["-d"], ["--dyn-syms", "--wide"]])
        self.assertTrue(all(call.args[0][0] == "target-readelf" for call in readelf.call_args_list))

    def test_wrong_internal_tags_rejected_before_readelf(self):
        for tags in ([], ["cp314-abi3-android_24_arm64_v8a"], [self.tag, self.tag]):
            with self.subTest(tags=tags), mock.patch.object(wheels.subprocess, "check_output") as readelf:
                with self.assertRaisesRegex(ValueError, "internal wheel tags disagree"):
                    wheels.inspect_wheel(self.make_wheel(tags=tags), "3.14", 24, "readelf")
                readelf.assert_not_called()

    def test_wrong_architecture_rejected_before_readelf(self):
        with mock.patch.object(wheels.subprocess, "check_output") as readelf:
            with self.assertRaisesRegex(ValueError, "not ELF64/little-endian/AArch64"):
                wheels.inspect_wheel(self.make_wheel(machine=62), "3.14", 24, "readelf")
            readelf.assert_not_called()

    def test_missing_or_wrong_minor_libpython_rejected(self):
        for dynamic in ("", self.dynamic.replace("libpython3.14.so", "libpython3.13.so")):
            with self.subTest(dynamic=dynamic), mock.patch.object(wheels.subprocess, "check_output", return_value=dynamic):
                with self.assertRaisesRegex(ValueError, "missing target libpython dependency"):
                    wheels.inspect_wheel(self.make_wheel(), "3.14", 24, "readelf")

    def test_glibc_dependencies_rejected(self):
        for library in wheels.GLIBC_LIBRARIES:
            with self.subTest(library=library), mock.patch.object(
                wheels.subprocess, "check_output", return_value=self.dynamic + f" (NEEDED) [{library}]\n"
            ):
                with self.assertRaisesRegex(ValueError, "glibc dependency"):
                    wheels.inspect_wheel(self.make_wheel(), "3.14", 24, "readelf")

    def test_host_extension_suffix_rejected(self):
        with mock.patch.object(wheels.subprocess, "check_output") as readelf:
            with self.assertRaisesRegex(ValueError, "host extension suffix"):
                wheels.inspect_wheel(self.make_wheel(extension="parser/_binding.cpython-314-x86_64-linux-gnu.so"), "3.14", 24, "readelf")
            readelf.assert_not_called()

    def test_missing_or_undefined_initializer_rejected(self):
        for symbols in ("", self.symbols.replace("DEFAULT 10", "DEFAULT UND")):
            with self.subTest(symbols=symbols), mock.patch.object(
                wheels.subprocess, "check_output", side_effect=[self.dynamic, symbols]
            ):
                with self.assertRaisesRegex(ValueError, "missing exported PyInit__binding"):
                    wheels.inspect_wheel(self.make_wheel(), "3.14", 24, "readelf")


if __name__ == "__main__":
    unittest.main()
