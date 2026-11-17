"""Build authentic, checksum-pinned Rust Python wheels for Android/Termux.

Requires host Python matching the target minor, maturin, wheel and the Android
Rust target/NDK. Upstream pyproject features and modules are preserved. Every
Cargo resolution must already be locked; this builder never generates locks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from scripts.build_support.android_parser_wheels import (
    fetch_source,
    inspect_wheel,
    sha256,
    unpack_source,
)
from scripts.build_support.android_rust_common import TARGET, build_environment, include_notices

DEFAULT_MANIFEST = Path(__file__).with_name("android_rust_sources.json")


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def inspect_metadata(wheel: Path, source: dict, *, pure: bool = False) -> dict:
    """ELF validation alone cannot detect a wrong distribution or source pin."""
    with zipfile.ZipFile(wheel) as archive:
        if archive.testzip():
            raise ValueError(f"wheel CRC mismatch: {wheel.name}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate wheel ZIP members: {wheel.name}")
        for member in archive.infolist():
            name = member.filename
            if (name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name)
                    or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError(f"unsafe wheel ZIP member: {name}")
        metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise ValueError(f"missing or ambiguous METADATA: {wheel.name}")
        metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
        if (len(metadata.get_all("Name", [])) != 1 or len(metadata.get_all("Version", [])) != 1
                or canonical_name(metadata["Name"]) != canonical_name(source["name"])
                or metadata["Version"] != source["version"]):
            raise ValueError(f"wheel metadata disagrees with source manifest: {wheel.name}")
        if pure:
            wheel_files = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(wheel_files) != 1:
                raise ValueError(f"missing or ambiguous WHEEL metadata: {wheel.name}")
            info = BytesParser().parsebytes(archive.read(wheel_files[0]))
            tags = info.get_all("Tag", [])
            if (info.get("Root-Is-Purelib", "").lower() != "true" or not tags
                    or any(not tag.endswith("-none-any") for tag in tags)
                    or not wheel.name.endswith("-none-any.whl")
                    or any(name.endswith((".so", ".pyd", ".dll", ".dylib")) for name in names)):
                raise ValueError(f"support wheel must be pure Python: {wheel.name}")
        return {"name": metadata["Name"], "version": metadata["Version"]}


def _relative_file(project: Path, value: str) -> Path:
    path = project / value
    if Path(value).is_absolute() or not path.resolve().is_relative_to(project.resolve()):
        raise ValueError(f"Cargo manifest must stay inside the source root: {value}")
    if not path.is_file():
        raise ValueError(f"missing Cargo manifest: {value}")
    return path


def verified_lock(source: dict, manifest_dir: Path) -> Path | None:
    """Verify external overrides before ANY source fetch or build operation."""
    override = source.get("cargo_lock")
    if override is None:
        return None
    if (not isinstance(override, dict) or not isinstance(override.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(override.get("sha256", "")))):
        raise ValueError("cargo_lock requires a path and independently pinned SHA256")
    path = (manifest_dir / override["path"]).resolve()
    if not path.is_file() or sha256(path) != override["sha256"]:
        raise ValueError(f"Cargo lock checksum mismatch: {source['name']}")
    return path


def patch_source(project: Path, source: dict) -> dict:
    """Only reviewed, exact-match source adaptations are permitted."""
    patch = source.get("patch")
    if patch is None:
        return {}
    if patch != "orjson-target-avx512" or canonical_name(source["name"]) != "orjson":
        raise ValueError(f"unsupported Rust source patch: {patch}")
    build_rs = project / "build.rs"
    original = ('    #[cfg(all(target_arch = "x86_64", not(target_os = "macos")))]\n'
                '    if is_64_bit_python {\n'
                '        println!("cargo:rustc-cfg=feature=\\"avx512\\"");\n'
                '    }')
    replacement = ('    if is_64_bit_python\n'
                   '        && std::env::var("CARGO_CFG_TARGET_ARCH").as_deref() == Ok("x86_64")\n'
                   '        && std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos")\n'
                   '    {\n'
                   '        println!("cargo:rustc-cfg=feature=\\"avx512\\"");\n'
                   '    }')
    text = build_rs.read_text()
    if text.count(original) != 1:
        raise ValueError("orjson AVX512 build.rs changed; review the target-cfg patch")
    build_rs.write_text(text.replace(original, replacement))
    return {"patch": patch, "patched_build_rs_sha256": sha256(build_rs)}


def build_one(source: dict, prefix: Path, version: str, api: int, cc: str,
              cache: Path, output: Path, readelf: str,
              manifest_dir: Path = DEFAULT_MANIFEST.parent) -> dict:
    """Build one real upstream module; all outputs go to the private batch stage."""
    external_lock = verified_lock(source, manifest_dir)
    archive = fetch_source(source, cache)
    with tempfile.TemporaryDirectory(prefix=".rust-build-", dir=output.parent) as temporary:
        directory = Path(temporary)
        project = unpack_source(archive, directory / "source")
        patches = patch_source(project, source)
        cargo_manifest = _relative_file(project, source.get("cargo_manifest", "Cargo.toml"))
        lock = project / "Cargo.lock"
        sdist_lock_hash = sha256(lock) if lock.is_file() else None
        pinned_lock = source.get("sdist_cargo_lock")
        if pinned_lock is not None:
            if (not isinstance(pinned_lock, dict) or pinned_lock.get("path") != "Cargo.lock"
                    or not re.fullmatch(r"[0-9a-f]{64}", str(pinned_lock.get("sha256", "")))
                    or sdist_lock_hash != pinned_lock["sha256"]):
                raise ValueError(f"sdist Cargo.lock checksum mismatch: {source['name']}")
        if external_lock is not None:
            shutil.copyfile(external_lock, lock)
            if sha256(lock) != source["cargo_lock"]["sha256"]:
                raise ValueError("external Cargo lock changed during copy")
        if not lock.is_file():
            raise ValueError(f"missing workspace Cargo.lock: {source['name']}; supply a pinned override")
        lock_hash = sha256(lock)
        env = build_environment(prefix, version, api, cc, directory / "target")
        wheels = directory / "wheels"
        # No --features/--no-default-features: retain the upstream pyproject policy.
        subprocess.run([sys.executable, "-m", "maturin", "build", "--release", "--locked",
                        "--target", TARGET, "--manifest-path", str(cargo_manifest),
                        "--out", str(wheels)], cwd=project, env=env, check=True)
        if not lock.is_file() or sha256(lock) != lock_hash:
            raise ValueError("workspace Cargo.lock changed during the locked build")
        built = list(wheels.glob("*.whl"))
        if len(built) != 1:
            raise ValueError(f"expected one wheel for {source['name']}")
        inspect_metadata(built[0], source)
        tag = "cp" + version.replace(".", "")
        # Both WHEEL metadata and filename must be exact-minor, including abi3 builds.
        subprocess.run([sys.executable, "-m", "wheel", "tags", "--python-tag", tag,
                        "--abi-tag", tag, "--platform-tag", f"android_{api}_arm64_v8a",
                        "--remove", str(built[0])], check=True)
        tagged = list(wheels.glob("*.whl"))
        if len(tagged) != 1:
            raise ValueError("expected one retagged Rust wheel")
        wheel = tagged[0]
        notices = include_notices(project, wheel, directory, env, cargo_manifest)
        if not lock.is_file() or sha256(lock) != lock_hash:
            raise ValueError("workspace Cargo.lock changed while collecting notices")
        identity = inspect_metadata(wheel, source)
        result = inspect_wheel(wheel, version, api, readelf)
        lock_name = f"{canonical_name(source['name'])}-{source['version']}.Cargo.lock"
        lock_destination = output / "locks" / lock_name
        lock_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(lock, lock_destination)
        result.update({
            **identity, "source": source, "source_sha256": sha256(archive), "patches": patches,
            "cargo_manifest": cargo_manifest.relative_to(project).as_posix(),
            "cargo_manifest_sha256": sha256(cargo_manifest),
            "sdist_cargo_lock_sha256": sdist_lock_hash,
            "cargo_lock": {"file": f"locks/{lock_name}", "sha256": lock_hash,
                           "origin": "override" if external_lock else "sdist"},
            "notices": notices,
            "rustc": subprocess.check_output(["rustc", "-vV"], cwd=project, env=env, text=True),
        })
        shutil.copyfile(wheel, output / wheel.name)
        return result


def copy_support_wheel(source: dict, cache: Path, output: Path) -> dict:
    wheel = fetch_source(source, cache)
    if wheel.suffix != ".whl":
        raise ValueError("support_wheels entries require a .whl filename")
    identity = inspect_metadata(wheel, source, pure=True)
    destination = output / wheel.name
    if destination.exists():
        raise ValueError(f"duplicate output wheel: {wheel.name}")
    shutil.copyfile(wheel, destination)
    return {**identity, "wheel": wheel.name, "sha256": sha256(destination), "source": source}


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
    parser.add_argument("--packages", nargs="+", help="subset of native distribution names")
    args = parser.parse_args(argv)
    if args.python_version != f"{sys.version_info.major}.{sys.version_info.minor}":
        parser.error("build interpreter must match the target Python minor")
    if args.android_api < 24:
        parser.error("Termux requires Android API >=24")
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("packages"), list):
        parser.error("unsupported source manifest")
    sources = manifest["packages"]
    support = manifest.get("support_wheels", [])
    names = set()
    if not sources or not isinstance(support, list):
        parser.error("manifest requires native packages and a support_wheels list")
    for source in [*sources, *support]:
        if (not isinstance(source, dict)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(source.get("name", "")))
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]*", str(source.get("version", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", "")))
                or not isinstance(source.get("url"), str) or not source["url"]):
            parser.error("source entries require name, version, URL and SHA256")
        name = canonical_name(source["name"])
        if name in names:
            parser.error(f"duplicate distribution: {name}")
        names.add(name)
    if args.packages:
        requested = {canonical_name(name) for name in args.packages}
        unknown = requested - {canonical_name(source["name"]) for source in sources}
        if unknown:
            parser.error(f"unknown native packages: {sorted(unknown)}")
        sources = [source for source in sources if canonical_name(source["name"]) in requested]
    # Validate the whole selected lock set before the first source fetch.
    manifest_dir = args.manifest.resolve().parent
    for source in sources:
        verified_lock(source, manifest_dir)
    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        parser.error("output directory must be empty")
    args.out.mkdir(parents=True, exist_ok=True)
    # All sources, targets, wheels, support wheels and locks remain private until
    # success. A single rename publishes the completion manifest and entire batch.
    with tempfile.TemporaryDirectory(prefix=".android-rust-", dir=args.out.parent) as temporary:
        staged = Path(temporary) / "wheels"
        staged.mkdir()
        results = []
        for source in sources:
            print(f"Building {source['name']}=={source['version']}", flush=True)
            results.append(build_one(source, args.target_prefix.resolve(), args.python_version,
                                     args.android_api, args.cc, args.cache.resolve(), staged,
                                     args.readelf, manifest_dir))
        support_results = [copy_support_wheel(source, args.cache.resolve(), staged) for source in support]
        (staged / "rust-wheels.json").write_text(json.dumps({
            "schema": 1, "python": args.python_version, "android_api": args.android_api,
            "target": TARGET, "manifest_sha256": sha256(args.manifest),
            "wheels": results, "support_wheels": support_results,
        }, indent=2) + "\n")
        os.replace(staged, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
