"""Verify, bundle and install the Android/Termux native runtime artifacts.

The three artifacts below are the ones ``setup.py`` normally produces locally
(see also ``.github/scripts/native_bundle.py`` for the glibc variants):

* ``openviking/bin/ov``                                 - Rust CLI
* ``openviking/lib/ragfs_python.abi3.so``               - PyO3 RAGFS binding
* ``openviking/storage/vectordb/engine/_native.abi3.so`` - C++ vectordb engine

The extensions use the stable API (``Py_LIMITED_API`` >= 3.10), but these Android
builds link Termux's versioned libpython and require that CPython minor version
on the device. The CLI is static musl; the extensions must use bionic rather than
glibc. ``verify`` checks ELF structure and dependencies, not runtime imports.

Typical CI usage::

    python scripts/build_support/android_native.py verify --root .
    python scripts/build_support/android_native.py bundle --root . --out dist-android

And on the device::

    python scripts/build_support/android_native.py install \\
        openviking-native-android-aarch64.tar.gz --root .
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

CLI_ARTIFACT = "openviking/bin/ov"
RAGFS_ARTIFACT = "openviking/lib/ragfs_python.abi3.so"
ENGINE_ARTIFACT = "openviking/storage/vectordb/engine/_native.abi3.so"
ARTIFACTS = (CLI_ARTIFACT, RAGFS_ARTIFACT, ENGINE_ARTIFACT)

# Exported entry points prove the module is really a CPython extension and not a
# plain shared library that would fail at import time.
MODULE_ENTRY_POINTS = {
    RAGFS_ARTIFACT: "PyInit_ragfs_python",
    ENGINE_ARTIFACT: "PyInit__native",
}

# Anything in this list means the file was linked against glibc, i.e. it cannot
# load inside Termux. libpython is handled separately: PyO3 always links it for
# Android targets, and the NDK's -Wl,--no-undefined effectively requires it.
GLIBC_MARKERS = (
    "ld-linux",
    "libc.so.6",
    "libm.so.6",
    "libdl.so.2",
    "libpthread.so.",
    "librt.so.",
    "libutil.so.",
    "libgcc_s.so",
    "libstdc++.so.6",
)

MACHINE_NAMES = {"aarch64", "arm64", "arm64-v8a"}
MANIFEST_SCHEMA = 1

# ``cp310-abi3`` is what setup.py/maturin emit for these artifacts.
ANDROID_PLATFORM_TAG = "android_24_arm64_v8a"
WHEEL_MODULE_PREFIX = "ragfs_python-"
EM_AARCH64 = b"\xb7\x00"


class VerificationError(RuntimeError):
    """Raised when an artifact is missing or was not built for Termux."""


def _readelf(args: list[str], readelf: str = "readelf") -> str:
    try:
        return subprocess.run(
            [readelf, *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout
    except FileNotFoundError as exc:
        raise VerificationError(f"{readelf} not found; install binutils/llvm") from exc
    except subprocess.CalledProcessError as exc:
        raise VerificationError(f"{readelf} {' '.join(args)} failed: {exc.stderr}") from exc


def needed_libraries(binary: Path, readelf: str = "readelf") -> list[str]:
    """Return ``DT_NEEDED`` entries in link order."""
    return [
        line.split("[", 1)[1].split("]", 1)[0]
        for line in _readelf(["-d", str(binary)], readelf).splitlines()
        if "(NEEDED)" in line and "[" in line
    ]


def dynamic_symbols(binary: Path, readelf: str = "readelf") -> str:
    return _readelf(["--dyn-syms", "--wide", str(binary)], readelf)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(
    relative: str, root: Path, readelf: str = "readelf", python_version: str = ""
) -> dict[str, object]:
    """Check a single artifact and return a small summary dict."""
    path = root / relative
    if not path.is_file():
        raise VerificationError(f"missing artifact: {relative}")
    header = _readelf(["-h", str(path)], readelf)
    if "AArch64" not in header:
        raise VerificationError(f"{relative}: expected an AArch64 ELF, got:\n{header.strip()}")
    if "ELF64" not in header:
        raise VerificationError(f"{relative}: expected ELF64")
    if "EXEC" not in header and "DYN" not in header:
        raise VerificationError(f"{relative}: unexpected ELF type (need EXEC or DYN)")

    needed = needed_libraries(path, readelf)
    offenders = [
        library
        for library in needed
        if any(marker in library for marker in GLIBC_MARKERS)
    ]
    if offenders:
        raise VerificationError(
            f"{relative}: linked against glibc/CPython ({', '.join(offenders)}); "
            "build it with the Android NDK instead of the host toolchain"
        )

    # Extensions must link the Termux libpython: Android's linker resolves a
    # stable-ABI module's Py* symbols only through its own DT_NEEDED entry.
    libpython = [library for library in needed if library.startswith("libpython")]
    if python_version:
        expected = f"libpython{python_version}.so"
        unexpected = [library for library in libpython if library != expected]
        if unexpected:
            raise VerificationError(
                f"{relative}: linked against {', '.join(unexpected)} but the bundle targets "
                f"Termux CPython {python_version} ({expected})"
            )
        if relative in MODULE_ENTRY_POINTS and expected not in libpython:
            raise VerificationError(
                f"{relative}: no DT_NEEDED {expected}; Android would fail to resolve the "
                "stable-ABI symbols (build it against the Termux sysroot libpython)"
            )
    elif libpython:
        raise VerificationError(
            f"{relative}: linked against {', '.join(libpython)}; pass --python-version to "
            "accept a Termux-specific build"
        )

    entry_point = MODULE_ENTRY_POINTS.get(relative)
    if entry_point and entry_point not in dynamic_symbols(path, readelf):
        raise VerificationError(f"{relative}: exported symbol {entry_point} not found")

    return {
        "path": relative,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "needed": needed,
    }


def verify(
    root: Path, readelf: str = "readelf", python_version: str = ""
) -> list[dict[str, object]]:
    return [
        verify_artifact(relative, root, readelf, python_version) for relative in ARTIFACTS
    ]


def git_commit(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("GITHUB_SHA", "")


def bundle(
    root: Path,
    out_dir: Path,
    *,
    version: str = "",
    android_api: str = "",
    ndk: str = "",
    python_version: str = "",
    readelf: str = "readelf",
) -> Path:
    """Verify the artifacts and pack them with a manifest next to the archive."""
    summary = verify(root, readelf, python_version)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{version}" if version else ""
    archive = out_dir / f"openviking-native-android-aarch64{suffix}.tar.gz"

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": git_commit(root),
        "version": version,
        "platform": "android",
        "arch": "aarch64",
        "abi": "cp310-abi3",
        "android_api": android_api,
        "ndk": ndk,
        "termux_python": python_version,
        "files": {item["path"]: item["sha256"] for item in summary},
        "sizes": {item["path"]: item["size"] for item in summary},
        "needed": {item["path"]: item["needed"] for item in summary},
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json", recursive=False)
        for relative in ARTIFACTS:
            tar.add(root / relative, arcname=relative, recursive=False)

    digest = sha256_file(archive)
    (out_dir / f"{archive.name}.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    return archive


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.name == "manifest.json":
                continue
            if member.name not in ARTIFACTS:
                raise VerificationError(f"unexpected bundle member: {member.name}")
            if not (member.isfile() or member.issym()):
                raise VerificationError(f"bundle member is not a regular file: {member.name}")
            if member.issym():
                raise VerificationError(f"bundle member is a symlink: {member.name}")
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise VerificationError(f"cannot read bundle member: {member.name}")
            target.write_bytes(source.read())


def install(
    archive: Path, root: Path, *, force: bool = False, ignore_arch: bool = False
) -> list[str]:
    """Restore a bundle into a checkout (run this on Termux)."""
    machine = platform.machine().lower()
    if machine not in MACHINE_NAMES and not ignore_arch:
        raise VerificationError(
            f"this bundle targets Android aarch64/Termux, current machine is '{machine}'"
        )
    if not archive.is_file():
        raise FileNotFoundError(archive)

    with tarfile.open(archive) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
    if manifest.get("arch") != "aarch64":
        raise VerificationError(f"unexpected bundle arch: {manifest.get('arch')}")

    existing = [relative for relative in ARTIFACTS if (root / relative).exists()]
    if existing and not force:
        raise VerificationError(
            "refusing to overwrite existing artifacts without --force: " + ", ".join(existing)
        )

    with tarfile.open(archive) as tar:
        members = {
            member.name: member
            for member in tar.getmembers()
            if member.name != "manifest.json"
        }
        missing = sorted(set(ARTIFACTS) - set(members))
        if missing:
            raise VerificationError(f"bundle is missing: {', '.join(missing)}")
        for relative, expected in manifest["files"].items():
            payload = tar.extractfile(members[relative]).read()
            if hashlib.sha256(payload).hexdigest() != expected:
                raise VerificationError(f"checksum mismatch for {relative}")

    _safe_extract(archive, root)
    for relative in ARTIFACTS:
        (root / relative).chmod(0o755)
    return list(ARTIFACTS)


def _default_out_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "dist-android"


def _is_aarch64_elf(payload: bytes) -> bool:
    return payload[:4] == b"\x7fELF" and payload[18:20] == EM_AARCH64


def wheel_check(dist: Path) -> list[str]:
    """Assert the built wheels carry the Android tag and the native payloads."""
    wheels = sorted(dist.glob("*.whl"))
    if not wheels:
        raise VerificationError(f"no wheels under {dist}")
    checked: list[str] = []
    for wheel in wheels:
        name = wheel.name
        if f"-{ANDROID_PLATFORM_TAG}.whl" not in name:
            raise VerificationError(
                f"{name}: expected the {ANDROID_PLATFORM_TAG} platform tag "
                "(Android/bionic); Termux would reject this wheel"
            )
        with zipfile.ZipFile(wheel) as archive:
            members = archive.namelist()
            if name.startswith("openviking-"):
                # The package wheel has to carry all three native artifacts.
                required = list(ARTIFACTS)
                missing = [member for member in required if member not in members]
                if missing:
                    raise VerificationError(
                        f"{name}: missing wheel members {missing}; wheel has {sorted(members)}"
                    )
            elif name.startswith(WHEEL_MODULE_PREFIX):
                # maturin supports both a top-level extension and a package
                # containing __init__.py plus ragfs_python/ragfs_python.abi3.so.
                required = [
                    member
                    for member in members
                    if Path(member).parent.as_posix() in (".", "ragfs_python")
                    and Path(member).name.startswith("ragfs_python.")
                    and member.endswith(".so")
                ]
                if not required:
                    raise VerificationError(
                        f"{name}: no ragfs_python extension at the wheel root or in "
                        f"ragfs_python/; wheel has {sorted(members)}"
                    )
            else:
                required = []
            for member in required:
                if not _is_aarch64_elf(archive.read(member)):
                    raise VerificationError(f"{name}: {member} is not an AArch64 ELF")
        checked.append(name)
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser("verify", help="check the three native artifacts")
    verify_parser.add_argument("--root", type=Path, default=Path.cwd())
    verify_parser.add_argument("--readelf", default="readelf")
    verify_parser.add_argument(
        "--python-version",
        default=os.environ.get("TERMUX_PY_VERSION", ""),
        help="Termux CPython minor version the artifacts must link (e.g. 3.14)",
    )

    bundle_parser = subparsers.add_parser("bundle", help="verify then pack a runtime tarball")
    bundle_parser.add_argument("--root", type=Path, default=Path.cwd())
    bundle_parser.add_argument("--out", type=Path, default=_default_out_dir())
    bundle_parser.add_argument("--version", default=os.environ.get("OPENVIKING_VERSION", ""))
    bundle_parser.add_argument(
        "--android-api", default=os.environ.get("ANDROID_API_LEVEL", "")
    )
    bundle_parser.add_argument("--ndk", default=os.environ.get("ANDROID_NDK_VERSION", ""))
    bundle_parser.add_argument("--termux-python", default=os.environ.get("TERMUX_PY_VERSION", ""))
    bundle_parser.add_argument("--readelf", default="readelf")

    install_parser = subparsers.add_parser("install", help="restore a bundle into a checkout")
    install_parser.add_argument("archive", type=Path)
    install_parser.add_argument("--root", type=Path, default=Path.cwd())
    install_parser.add_argument("--force", action="store_true")
    install_parser.add_argument(
        "--ignore-arch",
        action="store_true",
        help="skip the local machine check (for CI round-trip validation)",
    )

    wheel_parser = subparsers.add_parser("wheel-check", help="validate android wheels")
    wheel_parser.add_argument("--dist", type=Path, default=_default_out_dir())

    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            for item in verify(args.root, args.readelf, args.python_version):
                needed = ", ".join(item["needed"]) or "(none)"
                print(f"[OK] {item['path']} ({item['size']} bytes) NEEDED: {needed}")
            return 0
        if args.command == "bundle":
            archive = bundle(
                args.root,
                args.out,
                version=args.version,
                android_api=args.android_api,
                ndk=args.ndk,
                python_version=args.termux_python,
                readelf=args.readelf,
            )
            print(f"[OK] wrote {archive} ({archive.stat().st_size} bytes)")
            print(f"[OK] sha256 {sha256_file(archive)}")
            return 0
        if args.command == "wheel-check":
            for name in wheel_check(args.dist):
                print(f"[OK] wheel {name}")
            return 0
        installed = install(
            args.archive, args.root, force=args.force, ignore_arch=args.ignore_arch
        )
    except (VerificationError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print("[OK] restored:")
    for relative in installed:
        print(f"  {relative}")
    print(
        "\nTermux note: these artifacts link Termux's libpython, so the device needs the "
        "same CPython minor version that produced them (check the manifest's termux_python). "
        "Run `pkg upgrade python` if the import fails with 'library libpython3.X.so not found'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
