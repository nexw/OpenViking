"""Offline, stdlib-only tests for the Android static language-pack builder.

Run: python -m unittest discover -s tests/misc -p test_android_language_pack.py
No Rust toolchain, source downloads, application imports, or builds are needed.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "android_language_pack", ROOT / "scripts/build_support/android_language_pack.py"
)
pack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack)

DOWNLOAD_APIS = (
    "download", "download_all", "download_group", "manifest_languages",
    "downloaded_languages", "clean_cache", "cache_dir", "configure", "init",
)
CARGO_DEPENDENCY = (
    'tree-sitter-language-pack = { version = "1.13.3", '
    'features = ["serde", "config", "download"] }'
)
# These are deliberately real delegation bodies, not offline success shims.
PROCESS_WRAPPER = '''#[pyfunction]
pub fn process(source: &str, language: &str) -> PyResult<String> {
    let result = tree_sitter_language_pack::process(source, language)?;
    Ok(result)
}
'''
PREFETCH_WRAPPER = '''#[pyfunction]
pub fn prefetch(languages: Vec<String>) -> PyResult<()> {
    tree_sitter_language_pack::prefetch(languages)?;
    Ok(())
}
'''
# Keep the real boundary headers: the patch must replace the entire native
# class, not accidentally treat its indented clean_cache as the global API.
DOWNLOAD_MANAGER = '''#[derive(Clone)]
#[pyclass(unsendable, from_py_object)]
pub struct DownloadManager {
    inner: Arc<tree_sitter_language_pack::DownloadManager>,
}

#[pymethods]
impl DownloadManager {
    #[pyo3(signature = ())]
    pub fn installed_languages(&self) -> Vec<String> {
        self.inner.installed_languages()
    }

    #[allow(clippy::missing_errors_doc)]
    #[pyo3(signature = ())]
    pub fn clean_cache(&self) -> PyResult<()> {
        self.inner.clean_cache()
    }

    #[allow(clippy::missing_errors_doc)]
    #[staticmethod]
    #[pyo3(signature = (version))]
    pub fn new(version: String) -> PyResult<DownloadManager> {
        tree_sitter_language_pack::DownloadManager::new(&version)
            .map(|val| Self { inner: Arc::new(val) })
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
    }
}

'''
FOLLOWING_ENUM = '''#[derive(Clone, PartialEq, Default, serde::Serialize, serde::Deserialize)]
#[pyclass(eq, eq_int, from_py_object)]
pub enum DataNodeKind {
    #[pyo3(name = "KEY_VALUE")]
    #[default]
    KeyValue = 0,
}
'''


def download_functions():
    signatures = {
        "download": ('language: &str', 'PyResult<()>'),
        "download_all": ('', 'PyResult<()>'),
        "download_group": ('group: &str', 'PyResult<()>'),
        "manifest_languages": ('', 'PyResult<Vec<String>>'),
        "downloaded_languages": ('', 'Vec<String>'),
        "clean_cache": ('', 'PyResult<()>'),
        "cache_dir": ('', 'PyResult<String>'),
        "configure": ('config: PackConfig', 'PyResult<()>'),
        "init": ('config: PackConfig', 'PyResult<()>'),
    }
    return "\n".join(
        f'#[pyfunction]\npub fn {name}({args}) -> {result} {{\n'
        f'    tree_sitter_language_pack::{name}({args.split(":")[0]})\n}}\n'
        for name, (args, result) in signatures.items()
    )


class TemporaryTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        # Any accidental network or build operation must fail, never go online.
        for target in (
            "scripts.build_support.android_parser_wheels.urllib.request.urlopen",
            "socket.create_connection",
        ):
            patcher = mock.patch(target, side_effect=AssertionError("offline test attempted network"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("fetch_source", "unpack_source"):
            patcher = mock.patch.object(pack, name, side_effect=AssertionError("unexpected source fetch/unpack"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("run", "check_output"):
            patcher = mock.patch.object(pack.subprocess, name, side_effect=AssertionError("unexpected build"))
            patcher.start()
            self.addCleanup(patcher.stop)


class OfflinePatchTests(TemporaryTestCase):
    def make_project(self):
        project = self.directory / "source"
        member = project / "crates/ts-pack-core-py"
        (member / "src").mkdir(parents=True, exist_ok=True)
        (project / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/ts-pack-core-py"]\n')
        cargo = member / "Cargo.toml"
        cargo.write_text('[package]\nname = "ts-pack-core-py"\nversion = "1.13.3"\n\n'
                         '[dependencies]\n' + CARGO_DEPENDENCY + '\n')
        binding = member / "src/lib.rs"
        binding.write_text('// upstream Python bindings\n' + PROCESS_WRAPPER + '\n' +
                           DOWNLOAD_MANAGER + FOLLOWING_ENUM + '\n' +
                           download_functions() + '\n' + PREFETCH_WRAPPER)
        lock = member / "Cargo.lock"
        lock.write_bytes(b'# upstream lockfile; preserve bytes\r\nversion = 4\r\n'
                         b'[[package]]\r\nname = "tree-sitter-language-pack"\r\nversion = "1.13.3"\r\n')
        return project, cargo, binding, lock

    def test_core_is_exactly_pinned_with_no_default_or_download_features(self):
        project, cargo, _, _ = self.make_project()
        workspace = (project / "Cargo.toml").read_bytes()
        pack.patch_offline(project)
        core = tomllib.loads(cargo.read_text())["dependencies"]["tree-sitter-language-pack"]
        self.assertEqual(core, {
            "version": "=1.13.3", "default-features": False, "features": ["serde", "config"],
        })
        self.assertEqual((project / "Cargo.toml").read_bytes(), workspace)

    def test_all_download_apis_raise_explicit_error_not_fake_success(self):
        project, _, binding, _ = self.make_project()
        self.assertEqual(tuple(pack.DOWNLOAD_APIS), DOWNLOAD_APIS)
        result = pack.patch_offline(project)
        text = binding.read_text()
        self.assertEqual(result["disabled_apis"], list(DOWNLOAD_APIS))
        self.assertIn("offline", pack.OFFLINE_ERROR.lower())
        self.assertIn("disabled", pack.OFFLINE_ERROR.lower())
        for name in DOWNLOAD_APIS:
            with self.subTest(api=name):
                matches = re.findall(
                    r"^pub fn " + name + r"\((.*?)\) -> ([^\n]+) \{\n(.*?)\n\}", text, re.S | re.M
                )
                self.assertEqual(len(matches), 1)
                _, return_type, body = matches[0]
                self.assertTrue(return_type.startswith("PyResult<"), return_type)
                self.assertEqual(body.strip(),
                                 "Err(pyo3::exceptions::PyRuntimeError::new_err("
                                 + json.dumps(pack.OFFLINE_ERROR) + "))")
                self.assertNotIn(f"tree_sitter_language_pack::{name}(", text)
        self.assertIn("pub fn downloaded_languages() -> PyResult<Vec<String>>", text)

    def test_real_process_and_prefetch_wrappers_are_preserved_verbatim(self):
        project, _, binding, _ = self.make_project()
        pack.patch_offline(project)
        text = binding.read_text()
        self.assertEqual(text.count(PROCESS_WRAPPER), 1)
        self.assertEqual(text.count(PREFETCH_WRAPPER), 1)
        self.assertIn("tree_sitter_language_pack::process(source, language)?", text)
        self.assertIn("tree_sitter_language_pack::prefetch(languages)?", text)

    def test_native_download_manager_new_fails_explicitly_offline(self):
        project, _, binding, _ = self.make_project()
        pack.patch_offline(project)
        text = binding.read_text()
        self.assertRegex(text, r"(?m)^#\[pyclass\([^\n]*\)\]\npub struct DownloadManager\b")
        self.assertNotIn("tree_sitter_language_pack::DownloadManager", text)
        manager = re.search(r"^impl DownloadManager \{\n(.*?)\n\}", text, re.S | re.M)
        self.assertIsNotNone(manager, "retain the native DownloadManager Python class")
        constructor = re.search(
            r"#\[staticmethod\]\s*(?:#\[[^\n]+\]\s*)*"
            r"pub fn new\(version: String\) -> PyResult<(?:DownloadManager|Self)> \{\n"
            r"(.*?)\n    \}", manager[1], re.S,
        )
        self.assertIsNotNone(constructor, "retain DownloadManager.new(version) as a static method")
        self.assertEqual(constructor[1].strip(),
                         "Err(pyo3::exceptions::PyRuntimeError::new_err("
                         + json.dumps(pack.OFFLINE_ERROR) + "))")
        self.assertIn(FOLLOWING_ENUM, text, "class replacement must not consume the following type")

    def test_member_lock_is_copied_unchanged_to_workspace_and_hashed(self):
        project, cargo, binding, lock = self.make_project()
        original = lock.read_bytes()
        (project / "Cargo.lock").write_bytes(b"stale workspace lock")
        result = pack.patch_offline(project)
        self.assertEqual(lock.read_bytes(), original)
        self.assertEqual((project / "Cargo.lock").read_bytes(), original)
        for key, path in (("patched_cargo_sha256", cargo), ("patched_binding_sha256", binding),
                          ("cargo_lock_sha256", project / "Cargo.lock")):
            with self.subTest(key=key):
                self.assertEqual(result[key], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_cargo_source_drift_or_duplicate_dependency_is_rejected(self):
        for dependency in (
            CARGO_DEPENDENCY.replace('"1.13.3"', '"1.13.4"'),
            CARGO_DEPENDENCY.replace(', "download"', ''),
            CARGO_DEPENDENCY + '\n' + CARGO_DEPENDENCY,
        ):
            with self.subTest(dependency=dependency):
                project, cargo, binding, _ = self.make_project()
                cargo.write_text('[dependencies]\n' + dependency + '\n')
                before_cargo, before_binding = cargo.read_bytes(), binding.read_bytes()
                with self.assertRaisesRegex(ValueError, "dependency changed"):
                    pack.patch_offline(project)
                self.assertEqual(cargo.read_bytes(), before_cargo)
                self.assertEqual(binding.read_bytes(), before_binding)

    def test_missing_or_duplicate_download_binding_is_rejected(self):
        for name in DOWNLOAD_APIS:
            for drift in ("missing", "duplicate"):
                with self.subTest(api=name, drift=drift):
                    project, _, binding, _ = self.make_project()
                    text = binding.read_text()
                    pattern = r"^pub fn " + name + r"\(.*?\n\}"
                    match = re.search(pattern, text, re.S | re.M)
                    self.assertIsNotNone(match)
                    replacement = ("// binding removed" if drift == "missing"
                                   else match[0] + "\n" + match[0])
                    binding.write_text(text[:match.start()] + replacement + text[match.end():])
                    with self.assertRaisesRegex(ValueError, name + " binding changed"):
                        pack.patch_offline(project)

    def test_downloaded_languages_signature_drift_is_rejected(self):
        project, _, binding, _ = self.make_project()
        binding.write_text(binding.read_text().replace(
            'pub fn downloaded_languages() -> Vec<String>',
            'pub fn downloaded_languages() -> Option<Vec<String>>',
        ))
        with self.assertRaisesRegex(ValueError, "downloaded_languages signature changed"):
            pack.patch_offline(project)

    def test_missing_member_lock_is_rejected(self):
        project, _, _, lock = self.make_project()
        lock.unlink()
        with self.assertRaisesRegex(ValueError, "Cargo.lock"):
            pack.patch_offline(project)
        self.assertFalse((project / "Cargo.lock").exists())


class BuildEnvironmentTests(TemporaryTestCase):
    def setUp(self):
        super().setUp()
        self.prefix = self.directory / "target"
        (self.prefix / "lib").mkdir(parents=True)
        (self.prefix / "lib/libpython3.14.so").touch()
        self.bundle = self.directory / "local grammar bundle.tar.zst"
        self.bundle.write_bytes(b"local source fixture (never unpacked by this test)")
        self.cc = "/ndk/bin/aarch64-linux-android24-clang"
        self.languages = ["python", "tsx", "csharp"]

    def environment(self, **overrides):
        arguments = dict(prefix=self.prefix, version="3.14", api=24, cc=self.cc,
                         bundle=self.bundle, languages=self.languages)
        arguments.update(overrides)
        return pack.build_environment(**arguments)

    def test_foreign_pyo3_and_rust_flags_are_removed_or_replaced(self):
        removed = ("PYO3_NO_PYTHON", "PYO3_CONFIG_FILE", "PYO3_PYTHON", "PYTHONHOME",
                   "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "PROJECT_ROOT")
        dirty = {key: "/foreign/host-config" for key in removed}
        dirty.update(PYO3_CROSS="0", PYO3_CROSS_PYTHON_VERSION="3.11",
                     PYO3_CROSS_LIB_DIR="/foreign/lib",
                     CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS="-L/foreign/lib")
        with mock.patch.dict(os.environ, dirty, clear=True):
            before = dict(os.environ)
            env = self.environment()
            self.assertEqual(dict(os.environ), before, "must not mutate the parent environment")
        for key in removed:
            with self.subTest(variable=key):
                self.assertNotIn(key, env)
        self.assertEqual(env["PYO3_CROSS"], "1")
        self.assertEqual(env["PYO3_CROSS_PYTHON_VERSION"], "3.14")
        self.assertEqual(env["PYO3_CROSS_LIB_DIR"], str(self.prefix / "lib"))
        self.assertNotIn("/foreign/", env["CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS"])

    def test_links_target_libpython_without_undefined_symbols(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = self.environment()
        flags = env["CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS"].split()
        self.assertIn(f"-Lnative={self.prefix / 'lib'}", flags)
        self.assertIn("-Clink-arg=-lpython3.14", flags)
        self.assertIn("-Clink-arg=-Wl,--no-as-needed", flags)
        self.assertIn("-Clink-arg=-Wl,--no-undefined", flags)
        self.assertEqual(env["CC_aarch64_linux_android"], self.cc)
        self.assertEqual(env["CXX_aarch64_linux_android"], self.cc + "++")
        self.assertEqual(env["AR_aarch64_linux_android"], "/ndk/bin/llvm-ar")
        self.assertEqual(env["CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER"], self.cc)
        self.assertEqual(env["CARGO_BUILD_JOBS"], "2")

    def test_static_explicit_language_list_uses_local_bundle_and_offline_zero(self):
        with mock.patch.dict(os.environ, {
            "TSLP_LINK_MODE": "dynamic", "TSLP_LANGUAGES": "all",
            "TSLP_SOURCE_BUNDLE_URL": "https://example.invalid/remote", "TSLP_OFFLINE": "1",
            "TSLP_ALLOW_FAILED_GRAMMARS": "1",
        }, clear=True):
            env = self.environment()
        self.assertEqual(env["TSLP_LINK_MODE"], "static")
        self.assertEqual(env["TSLP_LANGUAGES"], "python,tsx,csharp")
        self.assertEqual(env["TSLP_SOURCE_BUNDLE_URL"], self.bundle.resolve().as_uri())
        self.assertTrue(env["TSLP_SOURCE_BUNDLE_URL"].startswith("file://"))
        self.assertIn("%20", env["TSLP_SOURCE_BUNDLE_URL"])
        # Upstream OFFLINE=1 skips even local bundle unpacking; this must stay 0.
        self.assertEqual(env["TSLP_OFFLINE"], "0")
        self.assertEqual(env["TSLP_ALLOW_FAILED_GRAMMARS"], "0")

    def test_invalid_or_empty_language_lists_are_rejected(self):
        for languages in ([], [""], ["Python"], ["python,rust"], ["c++"], ["../rust"],
                          ["python "], [" python"], ["py\nthon"], ["rust", "*"]):
            with self.subTest(languages=languages):
                with self.assertRaisesRegex(ValueError, "invalid embedded language list"):
                    self.environment(languages=languages)
        self.assertEqual(self.environment(languages=["c_sharp", "html5"])["TSLP_LANGUAGES"],
                         "c_sharp,html5")

    def test_whitespace_target_prefix_is_rejected(self):
        for name in ("target prefix", "target\tprefix", "target\nprefix"):
            with self.subTest(prefix=name):
                prefix = self.directory / name
                (prefix / "lib").mkdir(parents=True)
                (prefix / "lib/libpython3.14.so").touch()
                with self.assertRaisesRegex(ValueError, "prefix.*whitespace"):
                    self.environment(prefix=prefix)

    def test_missing_target_libpython_is_rejected(self):
        (self.prefix / "lib/libpython3.14.so").unlink()
        (self.prefix / "lib/libpython3.13.so").touch()
        with self.assertRaisesRegex(ValueError, "missing target libpython"):
            self.environment()


class CommandLineTests(TemporaryTestCase):
    def test_requires_exactly_one_explicit_workspace_lock_policy(self):
        arguments = [
            "--target-prefix", str(self.directory / "target"),
            "--python-version", f"{pack.sys.version_info.major}.{pack.sys.version_info.minor}",
            "--cc", "target-clang", "--cache", str(self.directory / "cache"),
            "--out", str(self.directory / "out"),
        ]
        for policy in ([], ["--resolve-lock", "--cargo-lock", "reviewed-Cargo.lock"]):
            with self.subTest(policy=policy), mock.patch("sys.stderr", new_callable=io.StringIO) as error:
                with self.assertRaises(SystemExit) as raised:
                    pack.main(arguments + policy)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--cargo-lock", error.getvalue())
                self.assertIn("--resolve-lock", error.getvalue())
        self.assertFalse((self.directory / "cache").exists())
        self.assertFalse((self.directory / "out").exists())


class ManifestTests(unittest.TestCase):
    def test_versioned_sources_have_the_reviewed_exact_hashes(self):
        manifest = json.loads(pack.DEFAULT_MANIFEST.read_text())
        self.assertEqual(manifest["schema"], 1)
        expected = {
            "python_source": (
                "tree-sitter-language-pack",
                "https://files.pythonhosted.org/packages/cb/9e/01be7a94fc69204350dc99707730aa4f32906cdb8b0b5856ff43fd0c60d5/tree_sitter_language_pack-1.13.3.tar.gz",
                "e0e0287bf5a2f3d8a382136b1565206988417ece502b796f4b6f7fefb832963c",
            ),
            "grammar_source": (
                "parser-sources",
                "https://github.com/xberg-io/tree-sitter-language-pack/releases/download/v1.13.3/parser-sources-1.13.3.tar.zst",
                "f02694b1bb6b9a9a51c4960b0169d201c2fbe0c7f6b91cbde19a81ccbe1e6723",
            ),
        }
        for key, (name, url, checksum) in expected.items():
            with self.subTest(source=key):
                source = manifest[key]
                self.assertEqual(source["name"], name)
                self.assertEqual(source["version"], "1.13.3")
                self.assertEqual(source["url"], url)
                self.assertEqual(source["sha256"], checksum)
                self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")

    def test_embedded_languages_are_explicit_and_reviewed_not_all(self):
        manifest = json.loads(pack.DEFAULT_MANIFEST.read_text())
        self.assertEqual(manifest["languages"], [
            "python", "javascript", "typescript", "tsx", "java", "cpp", "rust", "go",
            "csharp", "php", "lua",
        ])
        self.assertEqual(len(manifest["languages"]), len(set(manifest["languages"])))
        for language in manifest["languages"]:
            self.assertRegex(language, r"^[a-z][a-z0-9_]*$")


if __name__ == "__main__":
    unittest.main()
