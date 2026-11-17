"""Verify, bundle and install the Android/Termux native runtime artifacts.

The three artifacts below are the ones ``setup.py`` normally produces locally
(see also ``.github/scripts/native_bundle.py`` for the glibc variants):

* ``openviking/bin/ov``                                 - Rust CLI
* ``openviking/lib/ragfs_python.abi3.so``               - PyO3 RAGFS binding
* ``openviking/storage/vectordb/engine/_native.abi3.so`` - C++ vectordb engine

All of them are stable-ABI (``Py_LIMITED_API`` >= 3.10) so one build serves every
CPython Termux ships, but they must be linked against bionic - a manylinux build
is rejected by Android's linker. ``verify`` enforces exactly that, which is what
makes a cross-built bundle safe to publish.

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

# Anything in this list means the file was linked against glibc/CPython's own
# dynamic library instead of bionic, i.e. it cannot load inside Termux.
GLIBC_MARKERS = (
    "libpython",
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
WHEEL_REQUIRED_MEMBERS = {
    "openviking-": (*ARTIFACTS,),  # full package wheel bundles all three
    "ragfs_python-": ("ragfs_python.abi3.so",),
}
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


def verify_artifact(relative: str, root: Path, readelf: str = "readelf") -> dict[str, object]:
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

    entry_point = MODULE_ENTRY_POINTS.get(relative)
    if entry_point and entry_point not in dynamic_symbols(path, readelf):
        raise VerificationError(f"{relative}: exported symbol {entry_point} not found")

    return {
        "path": relative,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "needed": needed,
    }


def verify(root: Path, readelf: str = "readelf") -> list[dict[str, object]]:
    return [verify_artifact(relative, root, readelf) for relative in ARTIFACTS]


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
    summary = verify(root, readelf)
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
            members = set(archive.namelist())
            required = next(
                (
                    value
                    for prefix, value in WHEEL_REQUIRED_MEMBERS.items()
                    if name.startswith(prefix)
                ),
                (),
            )
            for member in required:
                if member not in members:
                    raise VerificationError(f"{name}: missing wheel member {member}")
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
            for item in verify(args.root, args.readelf):
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
        "\nTermux note: stable-ABI modules carry no DT_NEEDED libpython, and Android's "
        "linker scopes libpython locally, so export the shared library before importing:\n"
        "  export LD_PRELOAD=$PREFIX/lib/libpython$(python -c "
        "'import sys;print(f\"{sys.version_info.major}.{sys.version_info.minor}\")').so"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
