"""Mirror a bounded, hash-locked subset of published Android/pure wheels.

No compilation, installation, retagging, dependency resolution or wheel execution.
An incomplete native dependency queue is recorded, not filled with placeholders.
Only packaging is needed on the host (imported lazily); no target sysroot/NDK.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from scripts.build_support.android_parser_wheels import fetch_source, sha256
from scripts.build_support.android_rust_wheels import canonical_name, inspect_metadata

DEFAULT_LOCK = Path(__file__).with_name("android_published_wheels.json")
DEFAULT_TARGET = Path(__file__).with_name("android_target.json")
GLIBC_NEEDED = {
    "libc.so.6", "libm.so.6", "libstdc++.so.6", "libgcc_s.so.1",
    "libpthread.so.0", "libdl.so.2", "librt.so.1", "libutil.so.1",
    "libresolv.so.2", "libcrypt.so.1", "libnsl.so.1", "libanl.so.1",
}


def supported_tags(target: dict) -> frozenset:
    """Use packaging's Android API ladder and normal CPython/abi3 compatibility."""
    from packaging.tags import android_platforms, compatible_tags, cpython_tags

    version = tuple(map(int, target["python_minor"].split(".")))
    interpreter = "cp" + "".join(map(str, version))
    platforms = list(android_platforms(api_level=target["minimum_android_api"],
                                       abi="arm64_v8a"))
    return frozenset([
        *cpython_tags(version, abis=[interpreter], platforms=platforms),
        *compatible_tags(version, interpreter=interpreter, platforms=platforms),
    ])


def _requires_python(value, target: dict) -> None:
    from packaging.specifiers import SpecifierSet

    if value is not None and not isinstance(value, str):
        raise ValueError("Requires-Python must be a string or null")
    if not SpecifierSet(value or "").contains(target["build_python"]):
        raise ValueError(f"Requires-Python {value!r} excludes target {target['build_python']}")


def _filename_tags(source: dict, allowed: frozenset) -> frozenset:
    from packaging.utils import parse_wheel_filename
    from packaging.version import Version

    name, version, _, tags = parse_wheel_filename(source["filename"])
    if (canonical_name(name) != canonical_name(source["name"])
            or version != Version(source["version"])):
        raise ValueError("wheel filename disagrees with locked name/version")
    if not tags.intersection(allowed):
        raise ValueError(f"incompatible published wheel tag: {source['filename']}")
    return tags


def validate_lock(lock: dict, target: dict, allowed: frozenset) -> None:
    """Validate every fixed selection and target before the first download."""
    if (target.get("schema") != 1 or target.get("architecture") != "aarch64"
            or not re.fullmatch(r"3\.\d+", str(target.get("python_minor", "")))
            or type(target.get("minimum_android_api")) is not int
            or target["minimum_android_api"] < 24
            or not re.fullmatch(re.escape(target["python_minor"]) + r"\.\d+",
                                str(target.get("build_python", "")))):
        raise ValueError("unsupported Android target profile")
    if (lock.get("schema") != 1 or lock.get("python_minor") != target["python_minor"]
            or lock.get("architecture") != target["architecture"]
            or type(lock.get("android_api")) is not int
            or lock["android_api"] != target["minimum_android_api"]
            or not isinstance(lock.get("wheels"), list)
            or not isinstance(lock.get("missing"), list)):
        raise ValueError("published wheel lock and target profile disagree")
    names, filenames = set(), set()
    for source in [*lock["wheels"], *lock["missing"]]:
        if (not isinstance(source, dict)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(source.get("name", "")))
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]*", str(source.get("version", "")))):
            raise ValueError("lock entries require a distribution name and version")
        name = canonical_name(source["name"])
        if name in names:
            raise ValueError(f"duplicate locked distribution: {name}")
        names.add(name)
    for source in lock["wheels"]:
        filename = source.get("filename")
        if (not isinstance(filename, str) or not filename.endswith(".whl")
                or Path(filename).name != filename or "\\" in filename
                or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", "")))
                or "requires_python" not in source):
            raise ValueError("published wheels require filename, SHA256 and Requires-Python")
        if filename in filenames:
            raise ValueError(f"duplicate locked filename: {filename}")
        filenames.add(filename)
        url = urlsplit(source.get("url", ""))
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.fragment or unquote(url.path.rsplit("/", 1)[-1]) != filename):
            raise ValueError("published wheel URL must be HTTPS with the exact locked filename")
        _filename_tags(source, allowed)
        _requires_python(source["requires_python"], target)


def inspect_payload(archive: zipfile.ZipFile, *, pure: bool, target: dict,
                    readelf: str) -> dict:
    """Inspect every ELF, including auxiliary libraries with no PyInit export."""
    needed = {}
    with tempfile.TemporaryDirectory(prefix="android-published-elf-") as temporary:
        for number, member in enumerate(archive.infolist()):
            if member.is_dir():
                continue
            name = member.filename
            with archive.open(member) as handle:
                header = handle.read(64)
            native_name = bool(re.search(r"\.(?:so(?:\..*)?|pyd|dll|dylib|a|o|lib|exe)$", name, re.I))
            native_magic = header.startswith((b"\x7fELF", b"MZ", b"!<arch>\n",
                                              b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                                              b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
                                              b"\xca\xfe\xba\xbe"))
            if not native_name and not native_magic:
                continue
            if pure:
                raise ValueError(f"pure any wheel contains native payload: {name}")
            if (len(header) < 64 or header[:7] != b"\x7fELF\x02\x01\x01"
                    or header[18:20] != b"\xb7\x00"):
                raise ValueError(f"not ELF64/little-endian/AArch64: {name}")
            if ".cpython-" in name and (
                    f".cpython-{target['python_minor'].replace('.', '')}-" not in name
                    or "aarch64-linux-android" not in name):
                raise ValueError(f"foreign CPython extension suffix: {name}")
            binary = Path(temporary) / f"payload-{number}.so"
            binary.write_bytes(archive.read(member))
            dynamic = subprocess.check_output([readelf, "-d", str(binary)], text=True)
            libraries = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", dynamic)
            for library in libraries:
                if library in GLIBC_NEEDED or library.startswith(("ld-linux", "ld64.so")):
                    raise ValueError(f"glibc DT_NEEDED {library}: {name}")
                match = re.match(r"libpython(\d+\.\d+)([^/]*)", library)
                if match and (match[1] != target["python_minor"]
                              or not re.fullmatch(r"\.so(?:\.\d+)*", match[2])):
                    raise ValueError(f"mismatched versioned libpython {library}: {name}")
            needed[name] = libraries
    return needed


def inspect_published_wheel(wheel: Path, source: dict, target: dict,
                            allowed: frozenset, readelf: str = "readelf") -> dict:
    from packaging.tags import parse_tag

    if wheel.name != source["filename"]:
        raise ValueError("downloaded wheel filename differs from locked filename")
    # fetch_source checks cached/downloaded bytes first; independently defend this API too.
    if sha256(wheel) != source["sha256"]:
        raise ValueError("published wheel checksum mismatch before ZIP inspection")
    tags = _filename_tags(source, allowed)
    _requires_python(source["requires_python"], target)
    identity = inspect_metadata(wheel, source, pure=False)
    with zipfile.ZipFile(wheel) as archive:
        wheels = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(wheels) != 1 or wheels[0].rsplit("/", 1)[0] != metadata[0].rsplit("/", 1)[0]:
            raise ValueError("missing or ambiguous/co-located WHEEL metadata")
        info = BytesParser().parsebytes(archive.read(wheels[0]))
        declared = info.get_all("Tag", [])
        internal = frozenset(tag for value in declared for tag in parse_tag(value))
        if not declared or len(declared) != len(set(declared)) or internal != tags:
            raise ValueError("internal WHEEL tags disagree with exact published filename")
        meta = BytesParser().parsebytes(archive.read(metadata[0]))
        requires = meta.get_all("Requires-Python", [])
        if len(requires) > 1:
            raise ValueError("ambiguous Requires-Python metadata")
        _requires_python(requires[0] if requires else None, target)
        pure = any(tag.platform == "any" for tag in tags)
        if pure and info.get("Root-Is-Purelib", "").lower() != "true":
            raise ValueError("any wheel must declare Root-Is-Purelib: true")
        libraries = inspect_payload(archive, pure=pure, target=target, readelf=readelf)
    return {**identity, "wheel": wheel.name, "sha256": source["sha256"],
            "source": source, "tags": sorted(map(str, tags)), "needed": libraries}


def source_commit() -> str:
    """Record repository identity without consulting target build tools."""
    return subprocess.check_output(
        ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"],
        text=True,
    ).strip()


def mirror(lock_path: Path, target_path: Path, cache: Path, output: Path,
           readelf: str = "readelf") -> dict:
    """Publish unchanged wheels plus a manifest in a single directory rename."""
    import packaging

    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    target = json.loads(target_path.read_bytes())
    allowed = supported_tags(target)
    validate_lock(lock, target, allowed)
    if output.is_symlink() or (output.exists() and (
            not output.is_dir() or any(output.iterdir()))):
        raise ValueError("output directory must be empty (and not a symlink)")
    cache = cache.resolve()
    output = output.resolve()
    if cache == output or cache.is_relative_to(output):
        raise ValueError("download cache must be outside the output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Capture provenance before downloads; lock digest describes the exact parsed bytes.
    import hashlib

    manifest = {
        "schema": 1, "stage": "published-android-wheel-subset",
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "source_commit": source_commit(), "target": target,
        "target_tag_profile": {"python_minor": target["python_minor"],
                               "build_python": target["build_python"],
                               "architecture": target["architecture"],
                               "android_api": target["minimum_android_api"],
                               "supported_tags": sorted(map(str, allowed))},
        "host_python": platform.python_version(), "packaging_version": packaging.__version__,
        "missing": lock["missing"], "wheels": [],
    }
    with tempfile.TemporaryDirectory(prefix=".android-published-", dir=output.parent) as temporary:
        staged = Path(temporary) / "wheels"
        staged.mkdir()
        for source in lock["wheels"]:
            wheel = fetch_source(source, cache)
            result = inspect_published_wheel(wheel, source, target, allowed, readelf)
            destination = staged / source["filename"]
            shutil.copyfile(wheel, destination)
            if sha256(destination) != source["sha256"]:
                raise ValueError("wheel changed during unchanged-byte staging")
            manifest["wheels"].append(result)
        (staged / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Never merge with stale artifacts; recheck immediately before atomic publication.
        if output.is_symlink() or (output.exists() and (
                not output.is_dir() or any(output.iterdir()))):
            raise ValueError("output directory must remain empty")
        os.replace(staged, output)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--readelf", default="readelf")
    args = parser.parse_args(argv)
    try:
        mirror(args.lock, args.target, args.cache, args.out, args.readelf)
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
