"""Offline Rust-wheel driver contracts; no builds, installs, or network access.

Run with unittest discovery (including test_android*.py). Native build results in
batch tests are explicitly .fixture files, not purported working native wheels.
The ZIP fixtures exercise only genuine wheel metadata/layout, not ELF execution.
"""

import hashlib
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts.build_support import android_rust_common as common
from scripts.build_support import android_rust_wheels as rust

HOST_MINOR = f"{sys.version_info.major}.{sys.version_info.minor}"
FASTUUID_CONFIG_BYTES = b'[target.\'cfg(all())\']\nrustflags = ["--cfg", "uuid_unstable"]\n'
ORJSON_AVX = """    #[cfg(all(target_arch = "x86_64", not(target_os = "macos")))]
    if is_64_bit_python {
        println!("cargo:rustc-cfg=feature=\\"avx512\\"");
    }"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source(name="demo", version="1.0", **extra):
    return {
        "name": name,
        "version": version,
        "url": "https://example.invalid/source.tar.gz",
        "sha256": "a" * 64,
        **extra,
    }


def metadata_wheel(
    path, *, name="demo", version="1.0", metadata=None, wheel_metadata=None, extra=None
):
    """Small structurally valid pure-wheel ZIP, with independently mutable metadata."""
    dist = "demo-1.0.dist-info"
    files = {
        "demo/__init__.py": b'__version__ = "1.0"\n',
        f"{dist}/METADATA": (
            metadata
            if metadata is not None
            else f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
        ).encode(),
        f"{dist}/WHEEL": (
            wheel_metadata
            if wheel_metadata is not None
            else "Wheel-Version: 1.0\nGenerator: unittest\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n\n"
        ).encode(),
    }
    files.update(extra or {})
    # An unsigned RECORD is valid; zipfile and email parsing need no wheel package.
    files[f"{dist}/RECORD"] = (
        "\n".join(f"{name},," for name in files) + f"\n{dist}/RECORD,,\n"
    ).encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for filename, data in files.items():
            archive.writestr(filename, data)
    return path


class OfflineCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Every accidental source download or tool invocation fails deterministically.
        for target in (
            "scripts.build_support.android_parser_wheels.urllib.request.urlopen",
            "socket.create_connection",
        ):
            patcher = mock.patch(target, side_effect=AssertionError("unexpected network"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for owner, name in (
            (rust, "fetch_source"),
            (rust.subprocess, "run"),
            (rust.subprocess, "check_output"),
        ):
            patcher = mock.patch.object(
                owner, name, side_effect=AssertionError("unexpected build/fetch")
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def wheel(self, **kwargs):
        return metadata_wheel(self.root / "demo-1.0-py3-none-any.whl", **kwargs)

    def manifest(self, packages=None, support=None):
        path = self.root / "sources.json"
        path.write_text(
            json.dumps(
                {"schema": 1, "packages": packages or [source()], "support_wheels": support or []}
            )
        )
        return path

    def args(self, manifest=None, **changes):
        values = {
            "manifest": manifest or self.manifest(),
            "target-prefix": self.root / "prefix",
            "python-version": HOST_MINOR,
            "android-api": 24,
            "cc": "/ndk/bin/aarch64-linux-android24-clang",
            "cache": self.root / "cache",
            "out": self.root / "out",
        }
        values.update(changes)
        return [item for key, value in values.items() for item in ("--" + key, str(value))]


class MetadataTests(OfflineCase):
    def test_exact_metadata_identity_allows_distribution_name_normalization_only(self):
        wheel = self.wheel(name="Demo_Pkg.Name", version="1.0+local")
        self.assertEqual(
            rust.inspect_metadata(wheel, source("demo-pkg-name", "1.0+local")),
            {"name": "Demo_Pkg.Name", "version": "1.0+local"},
        )

    def test_wrong_missing_or_duplicate_identity_is_rejected(self):
        for headers in (
            "Name: other\nVersion: 1.0",
            "Name: demo\nVersion: 1.0.0",
            "Name: demo",
            "Version: 1.0",
            "Name: demo\nName: demo\nVersion: 1.0",
            "Name: demo\nVersion: 1.0\nVersion: 1.0",
        ):
            with self.subTest(headers=headers):
                with self.assertRaisesRegex(ValueError, "metadata disagrees"):
                    rust.inspect_metadata(self.wheel(metadata=headers + "\n\n"), source())

    def test_missing_or_multiple_dist_info_metadata_is_rejected(self):
        wheel = self.wheel(extra={"other-1.0.dist-info/METADATA": b"Name: demo\nVersion: 1.0\n"})
        with self.assertRaisesRegex(ValueError, "ambiguous METADATA"):
            rust.inspect_metadata(wheel, source())
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("demo/__init__.py", "")
        with self.assertRaisesRegex(ValueError, "METADATA"):
            rust.inspect_metadata(wheel, source())

    def test_support_requires_pure_metadata_filename_tags_and_no_native_payload(self):
        self.assertEqual(rust.inspect_metadata(self.wheel(), source(), pure=True)["name"], "demo")
        bad_metadata = (
            "Root-Is-Purelib: false\nTag: py3-none-any\n",
            "Root-Is-Purelib: true\nTag: cp314-cp314-android_24_arm64_v8a\n",
            "Root-Is-Purelib: true\nTag: py3-none-any\nTag: cp314-cp314-linux_aarch64\n",
            "Root-Is-Purelib: true\n",
        )
        for text in bad_metadata:
            with self.subTest(metadata=text), self.assertRaisesRegex(ValueError, "pure Python"):
                rust.inspect_metadata(self.wheel(wheel_metadata=text), source(), pure=True)
        for suffix in (".so", ".pyd", ".dll", ".dylib"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "pure Python"):
                rust.inspect_metadata(
                    self.wheel(extra={"demo/native" + suffix: b"native fixture"}),
                    source(),
                    pure=True,
                )
        renamed = self.wheel().rename(self.root / "demo-1.0-cp314-cp314-linux_aarch64.whl")
        with self.assertRaisesRegex(ValueError, "pure Python"):
            rust.inspect_metadata(renamed, source(), pure=True)

    def test_corrupt_zip_crc_is_rejected(self):
        wheel = self.wheel()
        with zipfile.ZipFile(wheel) as archive:
            member = archive.getinfo("demo/__init__.py")
        data = bytearray(wheel.read_bytes())
        # ZIP local header lengths locate the stored payload without touching CRCs.
        name_length, extra_length = struct.unpack_from("<HH", data, member.header_offset + 26)
        payload = member.header_offset + 30 + name_length + extra_length
        data[payload] ^= 1
        wheel.write_bytes(data)
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            rust.inspect_metadata(wheel, source(), pure=True)

    def test_zip_traversal_absolute_symlink_and_duplicate_members_are_rejected(self):
        for pure in (False, True):
            for name in (
                "../escape.py",
                "/absolute.py",
                "C:/absolute.py",
                "demo/../escape.py",
                "demo\\escape.py",
                "demo//escape.py",
                "demo/./escape.py",
            ):
                with self.subTest(pure=pure, member=name):
                    wheel = self.wheel(extra={name: b"unsafe member"})
                    with self.assertRaisesRegex(ValueError, "unsafe wheel ZIP member"):
                        rust.inspect_metadata(wheel, source(), pure=pure)
            wheel = self.wheel()
            link = zipfile.ZipInfo("demo/link.py")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr(link, "../../outside.py")
            with self.assertRaisesRegex(ValueError, "unsafe wheel ZIP member"):
                rust.inspect_metadata(wheel, source(), pure=pure)
            wheel = self.wheel()
            with zipfile.ZipFile(wheel, "a") as archive, self.assertWarns(UserWarning):
                archive.writestr("demo/__init__.py", "duplicate module")
            with self.assertRaisesRegex(ValueError, "duplicate wheel ZIP members"):
                rust.inspect_metadata(wheel, source(), pure=pure)

    def test_support_copy_validates_identity_and_preserves_bytes_and_digest(self):
        wheel = self.wheel()
        out = self.root / "support"
        out.mkdir()
        with mock.patch.object(rust, "fetch_source", return_value=wheel):
            with self.assertRaisesRegex(ValueError, "metadata disagrees"):
                rust.copy_support_wheel(source(version="2.0"), self.root, out)
            self.assertEqual(list(out.iterdir()), [])
            result = rust.copy_support_wheel(source(), self.root, out)
            self.assertEqual((out / wheel.name).read_bytes(), wheel.read_bytes())
            self.assertEqual(result["sha256"], digest(wheel.read_bytes()))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                rust.copy_support_wheel(source(), self.root, out)


class SourceAndLockTests(OfflineCase):
    def test_current_six_manifest_pins_and_original_locks(self):
        manifest = json.loads(rust.DEFAULT_MANIFEST.read_text())
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(
            {p["name"]: p["version"] for p in manifest["packages"]},
            {
                "orjson": "3.13.0",
                "pydantic-core": "2.50.4",
                "jiter": "0.19.0",
                "rpds-py": "2026.9.1",
                "fastuuid": "0.14.0",
                "frostwork": "0.2.2",
            },
        )
        for package in manifest["packages"]:
            with self.subTest(package=package["name"]):
                self.assertRegex(package["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(package["url"].startswith("https://files.pythonhosted.org/"))
                self.assertEqual(package["sdist_cargo_lock"]["path"], "Cargo.lock")
                self.assertRegex(package["sdist_cargo_lock"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            manifest["packages"][2]["cargo_manifest"], "crates/jiter-python/Cargo.toml"
        )
        self.assertEqual(manifest["packages"][0]["patch"], "orjson-target-avx512")
        fastuuid = manifest["packages"][4]
        self.assertEqual(fastuuid["preserve_cargo_config"], rust.FASTUUID_CONFIG)
        self.assertEqual(rust.FASTUUID_CONFIG["sha256"], digest(FASTUUID_CONFIG_BYTES))
        self.assertEqual(
            fastuuid["sdist_cargo_lock"]["sha256"],
            "8c30c9686cf96793eb3995f213e8ae4d4d694964bc2a02b50d9ce16b7ed7763b",
        )
        self.assertEqual(
            manifest["packages"][5]["sdist_cargo_lock"]["sha256"],
            "008cc09152b0de5cfa0a56928a659dc7ec8aae9ffb19ef6d45b5f72334f0c167",
        )

    def test_orjson_avx_patch_uses_cargo_target_not_build_host_cfg(self):
        build_rs = self.root / "build.rs"
        build_rs.write_text("fn main() {\n" + ORJSON_AVX + "\n}\n")
        result = rust.patch_source(self.root, source("orjson", patch="orjson-target-avx512"))
        text = build_rs.read_text()
        self.assertIn('std::env::var("CARGO_CFG_TARGET_ARCH").as_deref() == Ok("x86_64")', text)
        self.assertIn('std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos")', text)
        self.assertNotIn("#[cfg(all(target_arch", text)
        self.assertIn('println!("cargo:rustc-cfg=feature=\\"avx512\\"")', text)
        self.assertEqual(result["patched_build_rs_sha256"], digest(build_rs.read_bytes()))

    def test_patch_drift_duplicate_unknown_and_wrong_distribution_fail_closed(self):
        for text in (ORJSON_AVX.replace("is_64_bit_python", "changed"), ORJSON_AVX * 2):
            (self.root / "build.rs").write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "changed"):
                rust.patch_source(self.root, source("orjson", patch="orjson-target-avx512"))
        for package in (
            source("orjson", patch="unknown"),
            source("jiter", patch="orjson-target-avx512"),
        ):
            with self.subTest(package=package), self.assertRaisesRegex(ValueError, "unsupported"):
                rust.patch_source(self.root, package)
        self.assertEqual(rust.patch_source(self.root, source()), {})

    def test_override_requires_independent_sha_and_matching_contents(self):
        lock = self.root / "reviewed.Cargo.lock"
        lock.write_bytes(b"version = 4\n")
        override = {"path": lock.name, "sha256": digest(lock.read_bytes())}
        self.assertEqual(rust.verified_lock(source(cargo_lock=override), self.root), lock)
        self.assertIsNone(rust.verified_lock(source(), self.root))
        for bad in (
            {"path": lock.name},
            {"path": lock.name, "sha256": "A" * 64},
            {"path": lock.name, "sha256": "0" * 64},
            {"path": "absent", "sha256": override["sha256"]},
            "not a lock descriptor",
        ):
            with self.subTest(override=bad), self.assertRaises(ValueError):
                rust.verified_lock(source(cargo_lock=bad), self.root)

    def test_bad_override_is_rejected_before_fetching_or_building_one(self):
        with mock.patch.object(rust, "fetch_source") as fetch:
            with self.assertRaisesRegex(ValueError, "checksum"):
                rust.build_one(
                    source(cargo_lock={"path": "missing", "sha256": "0" * 64}),
                    self.root,
                    HOST_MINOR,
                    24,
                    "clang",
                    self.root,
                    self.root,
                    "readelf",
                    self.root,
                )
            fetch.assert_not_called()

    def test_cargo_manifest_rejects_absolute_traversal_and_escaping_symlink(self):
        project = self.root / "project"
        project.mkdir()
        outside = self.root / "Cargo.toml"
        outside.write_text("[workspace]\n")
        (project / "escape").symlink_to(self.root, target_is_directory=True)
        for value in (str(outside), "../Cargo.toml", "escape/Cargo.toml"):
            with self.subTest(path=value), self.assertRaisesRegex(ValueError, "inside"):
                rust._relative_file(project, value)
        nested = project / "crates/module/Cargo.toml"
        nested.parent.mkdir(parents=True)
        nested.write_text("[package]\n")
        self.assertEqual(rust._relative_file(project, "crates/module/Cargo.toml"), nested)


class EnvironmentTests(OfflineCase):
    def test_generic_environment_scrubs_foreign_flags_links_target_and_isolates_build(self):
        prefix = self.root / "prefix"
        (prefix / "lib").mkdir(parents=True)
        (prefix / "lib" / f"libpython{HOST_MINOR}.so").write_bytes(b"target library fixture")
        hostile = {
            "PATH": "/tools",
            "PYO3_CONFIG_FILE": "/host/config",
            "PYO3_PYTHON": "/host/python",
            "PYO3_CROSS_LIB_DIR": "/host/lib",
            "RUSTFLAGS": "host rust flags",
            "CARGO_ENCODED_RUSTFLAGS": "host encoded flags",
            "PYTHONHOME": "/host/home",
            "PROJECT_ROOT": "/host/project",
            "TSLP_OFFLINE": "1",
            "TSLP_SOURCE_BUNDLE_URL": "https://host.invalid",
            "CARGO_TARGET_DIR": "/host/target",
            "CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS": "host target flags",
        }
        with mock.patch.dict(os.environ, hostile, clear=True):
            env = common.build_environment(
                prefix, HOST_MINOR, 24, "/ndk/bin/clang", self.root / "target-a"
            )
            second = common.build_environment(
                prefix, HOST_MINOR, 24, "/ndk/bin/clang", self.root / "target-b"
            )
        self.assertFalse(any(key.startswith("TSLP_") for key in env))
        for key in (
            "PYO3_CONFIG_FILE",
            "PYO3_PYTHON",
            "PYTHONHOME",
            "PROJECT_ROOT",
            "RUSTFLAGS",
            "CARGO_ENCODED_RUSTFLAGS",
        ):
            self.assertNotIn(key, env)
        self.assertEqual(
            {k: v for k, v in env.items() if k.startswith("PYO3_")},
            {
                "PYO3_CROSS": "1",
                "PYO3_CROSS_PYTHON_VERSION": HOST_MINOR,
                "PYO3_CROSS_LIB_DIR": str(prefix / "lib"),
            },
        )
        flags = env["CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS"]
        for value in (
            f"-Lnative={prefix / 'lib'}",
            f"-lpython{HOST_MINOR}",
            "--no-as-needed",
            "--no-undefined",
        ):
            self.assertIn(value, flags)
        self.assertNotIn("host", flags)
        self.assertEqual(env["CC_aarch64_linux_android"], "/ndk/bin/clang")
        self.assertEqual(env["CXX_aarch64_linux_android"], "/ndk/bin/clang++")
        self.assertEqual(env["AR_aarch64_linux_android"], "/ndk/bin/llvm-ar")
        self.assertEqual(env["CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER"], "/ndk/bin/clang")
        self.assertEqual(env["CARGO_TARGET_DIR"], str((self.root / "target-a").resolve()))
        self.assertNotEqual(env["CARGO_TARGET_DIR"], second["CARGO_TARGET_DIR"])
        self.assertEqual(env["CARGO_PROFILE_RELEASE_LTO"], "false")

    def test_environment_rejects_api_version_missing_library_and_whitespace_prefix(self):
        prefix = self.root / "with space"
        (prefix / "lib").mkdir(parents=True)
        (prefix / "lib" / f"libpython{HOST_MINOR}.so").touch()
        for path, version, api in (
            (prefix, HOST_MINOR, 24),
            (prefix, HOST_MINOR, 23),
            (prefix, "3.14.10", 24),
            (self.root, HOST_MINOR, 24),
        ):
            with self.subTest(path=path, version=version, api=api), self.assertRaises(ValueError):
                common.build_environment(path, version, api, "clang", self.root / "target")


class BatchTests(OfflineCase):
    def test_main_rejects_host_minor_low_api_and_unknown_packages_before_build(self):
        arguments = (
            self.args(**{"python-version": "3.999"}),
            self.args(**{"android-api": 23}),
            self.args() + ["--packages", "unknown"],
        )
        with mock.patch.object(rust, "build_one") as build:
            for argv in arguments:
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as error:
                    rust.main(argv)
                self.assertEqual(error.exception.code, 2)
            build.assert_not_called()

    def test_main_rejects_nonempty_output_without_touching_stale_file(self):
        out = self.root / "out"
        out.mkdir()
        (out / "old.whl").write_bytes(b"stale sentinel, not a wheel")
        with mock.patch.object(rust, "build_one") as build:
            with self.assertRaises(SystemExit):
                rust.main(self.args())
            build.assert_not_called()
        self.assertEqual((out / "old.whl").read_bytes(), b"stale sentinel, not a wheel")

    def test_main_validates_all_selected_override_locks_before_first_build(self):
        packages = [
            source("first"),
            source("second", cargo_lock={"path": "missing", "sha256": "0" * 64}),
        ]
        with mock.patch.object(rust, "build_one") as build:
            with self.assertRaisesRegex(ValueError, "checksum"):
                rust.main(self.args(self.manifest(packages)))
            build.assert_not_called()
        self.assertFalse((self.root / "out").exists())

    def test_batch_publishes_whole_manifest_only_after_all_native_and_support_success(self):
        manifest = self.manifest([source("first"), source("second")], [source("support")])
        out = self.root / "out"
        events = []
        stages = []

        def native(package, prefix, version, api, cc, cache, output, readelf, manifest_dir):
            self.assertEqual(list(out.iterdir()), [])
            self.assertNotEqual(output, out)
            self.assertEqual(output.parent.parent, out.parent)
            self.assertFalse((output / "rust-wheels.json").exists())
            self.assertEqual(manifest_dir, manifest.parent.resolve())
            stages.append(output)
            events.append(package["name"])
            filename = package["name"] + ".fixture"
            (output / filename).write_text("mock build result, not a native wheel")
            return {"name": package["name"], "version": package["version"], "wheel": filename}

        def support(package, cache, output):
            self.assertEqual(events, ["first", "second"])
            self.assertEqual(list(out.iterdir()), [])
            self.assertFalse((output / "rust-wheels.json").exists())
            events.append("support")
            (output / "support.fixture").write_text("mock support result")
            return {"name": "support", "wheel": "support.fixture"}

        real_replace = os.replace

        def publish(staged, destination):
            self.assertEqual(events, ["first", "second", "support"])
            self.assertEqual(destination, out)
            self.assertEqual(list(out.iterdir()), [])
            self.assertEqual(
                {p.name for p in staged.iterdir()},
                {"first.fixture", "second.fixture", "support.fixture", "rust-wheels.json"},
            )
            return real_replace(staged, destination)

        with (
            mock.patch.object(rust, "build_one", side_effect=native),
            mock.patch.object(rust, "copy_support_wheel", side_effect=support),
            mock.patch.object(rust.os, "replace", side_effect=publish) as replace,
        ):
            self.assertEqual(rust.main(self.args(manifest)), 0)
            replace.assert_called_once()
        record = json.loads((out / "rust-wheels.json").read_text())
        self.assertEqual(record["manifest_sha256"], digest(manifest.read_bytes()))
        self.assertEqual(record["python"], HOST_MINOR)
        self.assertEqual(record["target"], "aarch64-linux-android")
        self.assertEqual([p["name"] for p in record["wheels"]], ["first", "second"])
        self.assertEqual(record["support_wheels"][0]["name"], "support")
        self.assertTrue(all(not path.exists() for path in stages))

    def test_native_or_support_failure_leaves_output_empty_and_cleans_stage(self):
        for failure in ("second", "support"):
            with self.subTest(failure=failure):
                manifest = self.manifest([source("first"), source("second")], [source("support")])
                out = self.root / "out"
                stages = []

                def native(
                    package,
                    prefix,
                    version,
                    api,
                    cc,
                    cache,
                    output,
                    readelf,
                    manifest_dir,
                    stages=stages,
                    failure=failure,
                ):
                    stages.append(output)
                    (output / (package["name"] + ".fixture")).write_text("mock native result")
                    if package["name"] == failure:
                        raise RuntimeError("simulated native failure")
                    return {"name": package["name"]}

                def support(package, cache, output):
                    (output / "partial-support.fixture").write_text("mock partial support result")
                    raise RuntimeError("simulated support failure")

                with (
                    mock.patch.object(rust, "build_one", side_effect=native),
                    mock.patch.object(rust, "copy_support_wheel", side_effect=support),
                    mock.patch.object(rust.os, "replace") as publish,
                ):
                    with self.assertRaisesRegex(RuntimeError, "simulated"):
                        rust.main(self.args(manifest))
                    publish.assert_not_called()
                self.assertEqual(list(out.iterdir()), [])
                self.assertTrue(all(not path.exists() for path in stages))

    def test_packages_subset_normalizes_names_and_keeps_support(self):
        manifest = self.manifest([source("other"), source("demo-pkg")], [source("support")])
        with (
            mock.patch.object(rust, "build_one", return_value={"name": "demo-pkg"}) as build,
            mock.patch.object(
                rust, "copy_support_wheel", return_value={"name": "support"}
            ) as support,
        ):
            rust.main(self.args(manifest) + ["--packages", "DEMO_pkg"])
        self.assertEqual(build.call_count, 1)
        self.assertEqual(build.call_args.args[0]["name"], "demo-pkg")
        support.assert_called_once()


class BuildContractTests(OfflineCase):
    def project(self):
        project = self.root / "source"
        project.mkdir()
        (project / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "1.0"\n')
        (project / "Cargo.lock").write_bytes(b"# original upstream lock\r\nversion = 4\r\n")
        archive = self.root / "source.tar.gz"
        archive.write_bytes(b"mock archive; unpack_source is mocked")
        out = self.root / "staged"
        out.mkdir()
        return project, archive, out

    def config_project(self):
        project, archive, out = self.project()
        config = project / ".cargo/config.toml"
        config.parent.mkdir()
        config.write_bytes(FASTUUID_CONFIG_BYTES)
        package = source(
            "fastuuid", "0.14.0", preserve_cargo_config=dict(rust.FASTUUID_CONFIG)
        )
        return project, archive, out, package

    def fixture_build(self, project, archive, out, package, on_maturin=None):
        """Capture a build contract without running package code or native tools."""
        commands = []
        environment = {rust.TARGET_RUSTFLAGS: "-Lnative=/target/lib -Clink-arg=-lpython3.14"}

        def run(command, **kwargs):
            commands.append((command, kwargs))
            if "maturin" in command:
                if on_maturin:
                    on_maturin(command, kwargs)
                wheels = Path(command[command.index("--out") + 1])
                wheels.mkdir()
                metadata_wheel(
                    wheels / "demo-1.0-py3-none-any.whl",
                    name=package["name"], version=package["version"],
                )

        with (
            mock.patch.object(rust, "fetch_source", return_value=archive),
            mock.patch.object(rust, "unpack_source", return_value=project),
            mock.patch.object(rust, "build_environment", return_value=environment),
            mock.patch.object(rust.subprocess, "run", side_effect=run),
            mock.patch.object(rust.subprocess, "check_output", return_value="rustc fixture\n"),
            mock.patch.object(rust, "include_notices", return_value={}),
            mock.patch.object(rust, "inspect_wheel", return_value={}),
        ):
            result = rust.build_one(
                package, self.root, HOST_MINOR, 24, "clang", self.root, out, "readelf"
            )
        return result, commands

    def test_config_policy_rejects_missing_unreviewed_flags_versions_and_paths_before_fetch(self):
        config = rust.FASTUUID_CONFIG
        packages = [
            source("fastuuid", "0.14.0"),
            source("fastuuid", "0.14.0", preserve_cargo_config=None),
            source("fastuuid", "0.14.1", preserve_cargo_config=config),
            source("other", preserve_cargo_config=config),
        ]
        for key, value in (
            ("path", "../config.toml"),
            ("sha256", "0" * 64),
            ("required_rustflags", ["--cfg", "invented_feature"]),
            ("required_rustflags", ["uuid_unstable", "--cfg"]),
            ("extra", "not reviewed"),
        ):
            packages.append(source(
                "fastuuid", "0.14.0", preserve_cargo_config={**config, key: value}
            ))
        with mock.patch.object(rust, "fetch_source") as fetch:
            for package in packages:
                with self.subTest(package=package), self.assertRaisesRegex(ValueError, "unreviewed"):
                    rust.build_one(
                        package, self.root, HOST_MINOR, 24, "clang", self.root, self.root, "readelf"
                    )
            fetch.assert_not_called()
        # Whole-batch validation also happens before building or creating output.
        with mock.patch.object(rust, "build_one") as build:
            with self.assertRaisesRegex(ValueError, "unreviewed"):
                rust.main(self.args(self.manifest([source(), packages[0]])))
            build.assert_not_called()
        self.assertFalse((self.root / "out").exists())

    def test_config_missing_wrong_bytes_and_escaping_symlink_fail_before_maturin(self):
        project, archive, out, package = self.config_project()
        config = project / ".cargo/config.toml"
        for contents in (None, FASTUUID_CONFIG_BYTES + b"# changed\n"):
            if contents is None:
                config.unlink()
            else:
                config.write_bytes(contents)
            with (
                self.subTest(contents=contents),
                mock.patch.object(rust, "fetch_source", return_value=archive),
                mock.patch.object(rust, "unpack_source", return_value=project),
                mock.patch.object(rust, "build_environment") as environment,
                mock.patch.object(rust.subprocess, "run") as run,
            ):
                with self.assertRaises(ValueError):
                    rust.build_one(
                        package, self.root, HOST_MINOR, 24, "clang", self.root, out, "readelf"
                    )
                environment.assert_not_called()
                run.assert_not_called()
                self.assertEqual(list(out.iterdir()), [])
        config.unlink()
        outside = self.root / "outside-config.toml"
        outside.write_bytes(FASTUUID_CONFIG_BYTES)
        config.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "inside"):
            rust.verified_cargo_config(project, package)

    def test_config_toml_semantics_are_checked_independently_of_sha(self):
        project, _, _, package = self.config_project()
        config = project / ".cargo/config.toml"
        # Mock ONLY the digest to prove TOML parsing independently rejects drift.
        for contents in (
            '[build]\nrustflags = ["--cfg", "uuid_unstable"]\n',
            '[target.\'cfg(all())\']\nrustflags = ["--cfg", "other"]\n',
            '[target.\'cfg(all())\']\nrustflags = "--cfg uuid_unstable"\n',
            FASTUUID_CONFIG_BYTES.decode() + '[env]\nUNREVIEWED = "1"\n',
            '[invalid TOML',
        ):
            config.write_text(contents)
            with (
                self.subTest(contents=contents),
                mock.patch.object(rust, "sha256", return_value=rust.FASTUUID_CONFIG["sha256"]),
                self.assertRaises(ValueError),
            ):
                rust.verified_cargo_config(project, package)

    def test_config_tokens_reach_only_target_subprocess_flags_and_are_recorded(self):
        project, archive, out, package = self.config_project()
        original = (project / ".cargo/config.toml").read_bytes()
        result, commands = self.fixture_build(project, archive, out, package)
        command, kwargs = commands[0]
        env = kwargs["env"]
        self.assertEqual(
            env[rust.TARGET_RUSTFLAGS].split(),
            ["-Lnative=/target/lib", "-Clink-arg=-lpython3.14", "--cfg", "uuid_unstable"],
        )
        self.assertEqual(list(env), [rust.TARGET_RUSTFLAGS])
        self.assertNotIn("RUSTFLAGS", env)
        self.assertNotIn("CARGO_ENCODED_RUSTFLAGS", env)
        self.assertNotIn("--features", command)
        self.assertNotIn("--all-features", command)
        self.assertNotIn("--no-default-features", command)
        self.assertIn("--locked", command)
        self.assertEqual(result["preserved_cargo_config"], rust.FASTUUID_CONFIG)
        self.assertEqual((project / ".cargo/config.toml").read_bytes(), original)

    def test_config_drift_during_locked_build_cannot_publish_a_wheel(self):
        project, archive, out, package = self.config_project()

        def change_config(command, kwargs):
            (project / ".cargo/config.toml").write_bytes(FASTUUID_CONFIG_BYTES + b"# drift\n")

        with self.assertRaisesRegex(ValueError, "config checksum"):
            self.fixture_build(project, archive, out, package, change_config)
        self.assertEqual(list(out.iterdir()), [])

    def test_frostwork_leaves_pyproject_python_feature_and_empty_defaults_unchanged(self):
        project, archive, out = self.project()
        pyproject = project / "pyproject.toml"
        policy = ('[tool.maturin]\nfeatures = ["python"]\n'
                  'module-name = "frostwork._frostwork"\npython-source = "python"\n')
        pyproject.write_text(policy)
        cargo = project / "Cargo.toml"
        cargo.write_text('[features]\npython = ["dep:pyo3"]\nmutate = []\n')
        result, commands = self.fixture_build(
            project, archive, out, source("frostwork", "0.2.2")
        )
        command, kwargs = commands[0]
        for flag in ("--features", "--all-features", "--no-default-features"):
            self.assertNotIn(flag, command)
        self.assertNotIn("mutate", " ".join(command))
        self.assertNotIn("uuid_unstable", kwargs["env"][rust.TARGET_RUSTFLAGS])
        self.assertEqual(pyproject.read_text(), policy)
        self.assertEqual(cargo.read_text(), '[features]\npython = ["dep:pyo3"]\nmutate = []\n')
        self.assertNotIn("preserved_cargo_config", result)

    def test_locked_build_archives_unchanged_sdist_lock_and_preserves_upstream_features(self):
        project, archive, out = self.project()
        original = (project / "Cargo.lock").read_bytes()
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if "maturin" in command:
                self.assertEqual(kwargs["cwd"], project)
                wheels = Path(command[command.index("--out") + 1])
                wheels.mkdir()
                metadata_wheel(wheels / "demo-1.0-py3-none-any.whl")
            # wheel tags is a no-op fixture: actual tagging/ELF are separate validators.

        with (
            mock.patch.object(rust, "fetch_source", return_value=archive),
            mock.patch.object(rust, "unpack_source", return_value=project),
            mock.patch.object(
                rust, "build_environment", return_value={"fixture": "environment"}
            ) as env,
            mock.patch.object(rust.subprocess, "run", side_effect=run),
            mock.patch.object(rust.subprocess, "check_output", return_value="rustc fixture\n"),
            mock.patch.object(rust, "include_notices", return_value={"fixture": True}),
            mock.patch.object(
                rust, "inspect_wheel", return_value={"wheel": "demo-1.0-py3-none-any.whl"}
            ),
        ):
            package = source(sdist_cargo_lock={"path": "Cargo.lock", "sha256": digest(original)})
            result = rust.build_one(
                package, self.root, HOST_MINOR, 24, "clang", self.root, out, "readelf", self.root
            )
        command = commands[0]
        self.assertIn("--locked", command)
        self.assertEqual(command[command.index("--target") + 1], common.TARGET)
        self.assertEqual(command[command.index("--manifest-path") + 1], str(project / "Cargo.toml"))
        self.assertNotIn("--features", command)
        self.assertNotIn("--no-default-features", command)
        self.assertEqual(Path(env.call_args.args[4]).name, "target")
        self.assertNotEqual(Path(env.call_args.args[4]), out)
        tag_command = commands[1]
        self.assertEqual(
            tag_command[tag_command.index("--python-tag") + 1], "cp" + HOST_MINOR.replace(".", "")
        )
        self.assertEqual(
            tag_command[tag_command.index("--abi-tag") + 1], "cp" + HOST_MINOR.replace(".", "")
        )
        self.assertEqual(result["sdist_cargo_lock_sha256"], digest(original))
        self.assertEqual(result["cargo_lock"]["origin"], "sdist")
        self.assertEqual(result["cargo_lock"]["sha256"], digest(original))
        self.assertEqual((out / result["cargo_lock"]["file"]).read_bytes(), original)
        self.assertEqual(result["source_sha256"], digest(archive.read_bytes()))

    def test_source_build_uses_process_temp_root_not_output_parent_workspace(self):
        project, archive, out = self.project()
        real_temporary_directory = tempfile.TemporaryDirectory
        extracted = []

        def unpack(archive_path, destination):
            self.assertEqual(archive_path, archive)
            # A standalone crate must not inherit the checkout's Cargo workspace.
            self.assertFalse(destination.resolve().is_relative_to(out.parent.resolve()))
            self.assertEqual(
                destination.parent.parent.resolve(), Path(tempfile.gettempdir()).resolve()
            )
            destination.mkdir()
            for name in ("Cargo.toml", "Cargo.lock"):
                (destination / name).write_bytes((project / name).read_bytes())
            extracted.append(destination)
            return destination

        def run(command, **kwargs):
            if "maturin" in command:
                self.assertEqual(kwargs["cwd"], extracted[0])
                wheels = Path(command[command.index("--out") + 1])
                self.assertEqual(wheels.parent, extracted[0].parent)
                wheels.mkdir()
                metadata_wheel(wheels / "demo-1.0-py3-none-any.whl")

        with (
            mock.patch.object(
                rust.tempfile, "TemporaryDirectory", wraps=real_temporary_directory
            ) as temporary,
            mock.patch.object(rust, "fetch_source", return_value=archive),
            mock.patch.object(rust, "unpack_source", side_effect=unpack),
            mock.patch.object(rust, "build_environment", return_value={}),
            mock.patch.object(rust.subprocess, "run", side_effect=run),
            mock.patch.object(rust.subprocess, "check_output", return_value="rustc fixture\n"),
            mock.patch.object(rust, "include_notices", return_value={"fixture": True}),
            mock.patch.object(
                rust, "inspect_wheel", return_value={"wheel": "demo-1.0-py3-none-any.whl"}
            ),
        ):
            rust.build_one(
                source(), self.root, HOST_MINOR, 24, "clang", self.root, out, "readelf", self.root
            )
        temporary.assert_called_once()
        self.assertEqual(temporary.call_args.args, ())
        self.assertNotIn("dir", temporary.call_args.kwargs)
        self.assertEqual(temporary.call_args.kwargs["prefix"], "android-rust-build-")
        self.assertEqual(len(extracted), 1)
        self.assertFalse(extracted[0].exists(), "source build directory must be cleaned up")
        self.assertTrue((out / "demo-1.0-py3-none-any.whl").is_file())

    def test_original_sdist_lock_is_validated_before_override_copy_or_build(self):
        project, archive, out = self.project()
        original = (project / "Cargo.lock").read_bytes()
        override = self.root / "override.Cargo.lock"
        override.write_bytes(b"version = 4\n# independently reviewed override\n")
        bad_pins = (
            {"path": "Cargo.lock", "sha256": "0" * 64},
            {"path": "../Cargo.lock", "sha256": digest(original)},
            {"path": "Cargo.lock"},
            "invalid lock descriptor",
        )
        for pin in bad_pins:
            package = source(
                sdist_cargo_lock=pin,
                cargo_lock={
                    "path": override.name,
                    "sha256": digest(override.read_bytes()),
                },
            )
            with (
                self.subTest(pin=pin),
                mock.patch.object(rust, "fetch_source", return_value=archive),
                mock.patch.object(rust, "unpack_source", return_value=project),
                mock.patch.object(rust, "build_environment") as environment,
            ):
                with self.assertRaisesRegex(ValueError, "sdist Cargo.lock checksum"):
                    rust.build_one(
                        package,
                        self.root,
                        HOST_MINOR,
                        24,
                        "clang",
                        self.root,
                        out,
                        "readelf",
                        self.root,
                    )
                environment.assert_not_called()
                self.assertEqual((project / "Cargo.lock").read_bytes(), original)
                self.assertEqual(list(out.iterdir()), [])

    def test_missing_or_modified_workspace_lock_cannot_produce_output(self):
        for mode in ("missing", "modified"):
            with self.subTest(mode=mode):
                case = self.root / mode
                case.mkdir()
                project = case / "source"
                project.mkdir()
                (project / "Cargo.toml").write_text("[workspace]\n")
                lock = project / "Cargo.lock"
                if mode == "modified":
                    lock.write_text("version = 4\n")
                out = case / "out"
                out.mkdir()

                def run(command, *, lock=lock, **kwargs):
                    lock.write_text("changed during locked build\n")

                with (
                    mock.patch.object(rust, "fetch_source", return_value=case / "archive"),
                    mock.patch.object(rust, "unpack_source", return_value=project),
                    mock.patch.object(rust, "build_environment", return_value={}),
                    mock.patch.object(rust.subprocess, "run", side_effect=run) as build,
                ):
                    with self.assertRaisesRegex(ValueError, "Cargo.lock"):
                        rust.build_one(
                            source(),
                            self.root,
                            HOST_MINOR,
                            24,
                            "clang",
                            case,
                            out,
                            "readelf",
                            self.root,
                        )
                    if mode == "missing":
                        build.assert_not_called()
                self.assertEqual(list(out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
