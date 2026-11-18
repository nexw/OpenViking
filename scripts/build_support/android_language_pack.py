"""Build the real language-pack process() implementation for offline Termux use.

This opt-in prototype embeds an explicit grammar set and compiles out runtime
network/dynamic grammar loading. Download/cache-management APIs fail explicitly;
parsing and prefetch use the actual upstream implementations. Upstream's
configuration/cache APIs depend on its downloader, so they are unsupported too.
Invoke as python -m scripts.build_support.android_language_pack.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from scripts.build_support.android_parser_wheels import (
    fetch_source,
    inspect_wheel,
    sha256,
    unpack_source,
)
from scripts.build_support.android_rust_common import build_environment as rust_environment
from scripts.build_support.android_rust_common import maturin_cargo_options

DEFAULT_MANIFEST = Path(__file__).with_name("android_language_pack_sources.json")
OFFLINE_ERROR = (
    "This Termux language-pack embeds an offline grammar set; "
    "runtime downloads and download-cache management are disabled."
)
DOWNLOAD_APIS = (
    "download", "download_all", "download_group", "manifest_languages",
    "downloaded_languages", "clean_cache", "cache_dir", "configure", "init",
)


def patch_offline(project: Path) -> dict:
    """Make a documented, fail-closed native variant; never fake download success."""
    cargo = project / "crates/ts-pack-core-py/Cargo.toml"
    original = 'tree-sitter-language-pack = { version = "1.13.3", features = ["serde", "config", "download"] }'
    replacement = ('tree-sitter-language-pack = { version = "=1.13.3", '
                   'default-features = false, features = ["serde", "config"] }')
    text = cargo.read_text()
    if text.count(original) != 1:
        raise ValueError("language-pack core dependency changed; review the offline patch")
    cargo.write_text(text.replace(original, replacement))
    binding = project / "crates/ts-pack-core-py/src/lib.rs"
    text = binding.read_text()
    manager_pattern = re.compile(
        r"#\[derive\(Clone\)\]\n#\[pyclass\(unsendable, from_py_object\)\]\n"
        r"pub struct DownloadManager \{.*?\n\}\n\n#\[pymethods\]\n"
        r"impl DownloadManager \{.*?\n\}", re.S,
    )
    manager = ('#[derive(Clone)]\n#[pyclass(unsendable, from_py_object)]\n'
               'pub struct DownloadManager;\n\n#[pymethods]\nimpl DownloadManager {\n'
               '    #[staticmethod]\n    #[pyo3(signature = (version))]\n'
               '    pub fn new(version: String) -> PyResult<DownloadManager> {\n'
               f'        Err(pyo3::exceptions::PyRuntimeError::new_err({json.dumps(OFFLINE_ERROR)}))\n'
               '    }\n}')
    text, count = manager_pattern.subn(lambda _: manager, text)
    if count != 1:
        raise ValueError("upstream DownloadManager binding changed; review the offline patch")
    for name in DOWNLOAD_APIS:
        pattern = re.compile(r"^pub fn " + name + r"\((.*?)\) -> ([^\n]+) \{\n.*?\n\}", re.S | re.M)
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError(f"upstream {name} binding changed; review the offline patch")
        match = matches[0]
        result_type = match.group(2)
        if name == "downloaded_languages":
            if result_type != "Vec<String>":
                raise ValueError("downloaded_languages signature changed")
            result_type = "PyResult<Vec<String>>"
        replacement = (f"pub fn {name}({match.group(1)}) -> {result_type} {{\n"
                       f"    Err(pyo3::exceptions::PyRuntimeError::new_err({json.dumps(OFFLINE_ERROR)}))\n"
                       "}")
        text = text[:match.start()] + replacement + text[match.end():]
    binding.write_text(text)
    # The sdist ships the wrapper's real Cargo.lock in its member directory.
    # Cargo ignores member lockfiles in a workspace; put it at the workspace root.
    lock = cargo.with_name("Cargo.lock")
    if not lock.is_file():
        raise ValueError("the pinned sdist no longer supplies a Cargo.lock")
    shutil.copyfile(lock, project / "Cargo.lock")
    return {
        "offline_policy": "static-only; downloader and dynamic-loading features disabled",
        "disabled_apis": list(DOWNLOAD_APIS),
        "patched_binding_sha256": sha256(binding),
        "patched_cargo_sha256": sha256(cargo),
        "cargo_lock_sha256": sha256(project / "Cargo.lock"),
    }


def patch_cpp_names(project: Path, archive: Path) -> dict:
    """Fix upstream's missed C/C++ nested declarators, caught by device tests."""
    vendor = project / "vendor"
    with tarfile.open(archive) as tar:
        tar.extractall(vendor, filter="data")
    core = vendor / "tree-sitter-language-pack-1.13.3"
    source = core / "src/intel/intelligence.rs"
    text = source.read_text()
    marker = '    for target_kind in &["type_identifier", "identifier", "scoped_identifier"] {'
    if text.count(marker) != 1:
        raise ValueError("upstream structure-name resolution changed; review C++ patch")
    patch = '''    // Termux device acceptance caught unnamed C/C++ function definitions:
    // their identifier is nested under one or more declarator fields.
    if let Some(declarator) = node.child_by_field_name("declarator") {
        let name = if matches!(declarator.kind(), "identifier" | "field_identifier"
            | "operator_name" | "destructor_name") {
            Some(node_text(&declarator, source).to_string())
        } else {
            resolve_structure_name(&declarator, source)
        };
        if let Some(name) = name.filter(|name| !name.is_empty()) {
            return Some(name);
        }
    }
'''
    source.write_text(text.replace(marker, patch + marker))
    cargo = project / "Cargo.toml"
    text = cargo.read_text()
    if "[patch.crates-io]" in text:
        raise ValueError("upstream workspace patches changed; review core override")
    cargo.write_text(text + '\n[patch.crates-io]\n'
                     'tree-sitter-language-pack = { path = "vendor/tree-sitter-language-pack-1.13.3" }\n')
    return {"core_patch": "resolve-nested-C-C++-declarators",
            "patched_intelligence_sha256": sha256(source)}


def build_environment(prefix: Path, version: str, api: int, cc: str,
                      bundle: Path, languages: list[str]) -> dict:
    env = rust_environment(prefix, version, api, cc)
    if not languages or any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in languages):
        raise ValueError("invalid embedded language list")
    env.update({
        "TSLP_LINK_MODE": "static", "TSLP_LANGUAGES": ",".join(languages),
        "TSLP_SOURCE_BUNDLE_URL": bundle.resolve().as_uri(),
        # Upstream build.rs treats TSLP_OFFLINE=1 as 'do not unpack any bundle',
        # including a local file. Runtime network is disabled through Cargo features.
        "TSLP_OFFLINE": "0", "TSLP_ALLOW_FAILED_GRAMMARS": "0",
    })
    return env


def include_notices(project: Path, wheel: Path, manifest: dict, cache: Path,
                    directory: Path, env: dict) -> None:
    """Carry exact grammar notices and the resolved Rust dependency notices."""
    unpacked = directory / "unpacked"
    subprocess.run([sys.executable, "-m", "wheel", "unpack", str(wheel),
                    "--dest", str(unpacked)], check=True)
    root = next(unpacked.iterdir())
    dist_info = next(root.glob("*.dist-info"))
    notices = dist_info / "licenses"
    for source in manifest["grammar_licenses"]:
        text = fetch_source(source, cache)
        destination = notices / "embedded-grammars" / source["filename"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(text.read_bytes())
    options = maturin_cargo_options(project)
    # Match tool.maturin.manifest-path, not the virtual workspace's defaults.
    cargo_manifest = project / "crates/ts-pack-core-py/Cargo.toml"
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--format-version", "1",
         "--filter-platform", "aarch64-linux-android", "--manifest-path", str(cargo_manifest),
         *options], cwd=project, env=env, text=True,
    ))
    packages = []
    for package in metadata["packages"]:
        source_root = Path(package["manifest_path"]).parent
        files = set()
        for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*"):
            files.update(p for p in source_root.glob(pattern) if p.is_file())
        if package.get("license_file"):
            custom = source_root / package["license_file"]
            if custom.is_file() and custom.resolve().is_relative_to(source_root.resolve()):
                files.add(custom)
        copied = []
        for path in sorted(files):
            destination = notices / "rust" / f"{package['name']}-{package['version']}" / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
            copied.append({"file": destination.relative_to(dist_info).as_posix(),
                           "sha256": sha256(path)})
        packages.append({"name": package["name"], "version": package["version"],
                         "license": package.get("license"), "source": package.get("source"),
                         "notices": copied})
    (dist_info / "ANDROID-THIRD-PARTY.json").write_text(json.dumps({
        "grammars": manifest["grammar_licenses"], "rust_packages": packages,
        "cargo_metadata_options": options,
        "cargo_manifest": cargo_manifest.relative_to(project).as_posix(),
    }, indent=2) + "\n")
    (dist_info / "ANDROID-OFFLINE.json").write_text(json.dumps({
        "embedded_languages": manifest["languages"], "disabled_apis": list(DOWNLOAD_APIS),
        "runtime_downloads": False, "dynamic_grammar_loading": False,
    }, indent=2) + "\n")
    subprocess.run([sys.executable, "-m", "wheel", "pack", str(root),
                    "--dest-dir", str(wheel.parent)], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--target-prefix", type=Path, required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--android-api", type=int, default=24)
    parser.add_argument("--cc", required=True)
    parser.add_argument("--readelf", default="readelf")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    lock = parser.add_mutually_exclusive_group(required=True)
    lock.add_argument("--cargo-lock", type=Path, help="verified complete workspace lockfile")
    lock.add_argument("--resolve-lock", action="store_true", help="prototype only: resolve and archive a lock")
    args = parser.parse_args(argv)
    if args.python_version != f"{sys.version_info.major}.{sys.version_info.minor}":
        parser.error("build interpreter must match the target Python minor")
    if args.android_api < 24:
        parser.error("Termux requires Android API >=24")
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("schema") != 1:
        parser.error("unsupported source manifest")
    if args.cargo_lock and sha256(args.cargo_lock) != manifest["cargo_lock_sha256"]:
        parser.error("Cargo lock does not match the independently pinned SHA256")
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("output directory must be empty")
    wrapper = fetch_source(manifest["python_source"], args.cache)
    bundle = fetch_source(manifest["grammar_source"], args.cache)
    core = fetch_source(manifest["core_source"], args.cache)
    integration = [fetch_source(source, args.cache) for source in manifest["integration_wheels"]]
    for wheel in integration:
        with zipfile.ZipFile(wheel) as archive:
            if archive.testzip():
                raise ValueError(f"integration wheel CRC mismatch: {wheel.name}")
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="android-language-pack-") as temporary:
        directory = Path(temporary)
        project = unpack_source(wrapper, directory / "source")
        patches = patch_offline(project)
        patches.update(patch_cpp_names(project, core))
        env = build_environment(args.target_prefix.resolve(), args.python_version,
                                args.android_api, args.cc, bundle, manifest["languages"])
        env["CARGO_TARGET_DIR"] = str(directory / "target")
        if args.cargo_lock:
            shutil.copyfile(args.cargo_lock, project / "Cargo.lock")
        else:
            # The published member lockfile is an outdated seed, not a complete
            # workspace resolution. Prototype runs opt in and archive the result;
            # repeatable builds must provide the verified --cargo-lock artifact.
            subprocess.run(["cargo", "generate-lockfile"], cwd=project, env=env, check=True)
        patches["cargo_lock_sha256"] = sha256(project / "Cargo.lock")
        wheels = directory / "wheels"
        subprocess.run([sys.executable, "-m", "maturin", "build", "--release", "--locked",
                        "--target", "aarch64-linux-android", "--out", str(wheels)],
                       cwd=project, env=env, check=True)
        built = list(wheels.glob("*.whl"))
        if len(built) != 1:
            raise ValueError("expected one language-pack wheel")
        tag = "cp" + args.python_version.replace(".", "")
        subprocess.run([sys.executable, "-m", "wheel", "tags", "--python-tag", tag,
                        "--abi-tag", tag, "--platform-tag", f"android_{args.android_api}_arm64_v8a",
                        "--remove", str(built[0])], check=True)
        wheel = next(wheels.glob("*.whl"))
        include_notices(project, wheel, manifest, args.cache, directory, env)
        result = inspect_wheel(wheel, args.python_version, args.android_api, args.readelf)
        result.update({"sources": manifest, "patches": patches,
                       "rustc": subprocess.check_output(["rustc", "-vV"], text=True)})
        (args.out / wheel.name).write_bytes(wheel.read_bytes())
        for pure_wheel in integration:
            shutil.copyfile(pure_wheel, args.out / pure_wheel.name)
        (args.out / "language-pack-build.json").write_text(json.dumps(result, indent=2) + "\n")
        shutil.copyfile(project / "Cargo.lock", args.out / "Cargo.lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
