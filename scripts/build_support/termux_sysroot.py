"""Materialize a Termux CPython prefix as an Android cross-compilation sysroot.

Cross-building OpenViking's abi3 extensions for Termux only needs CPython
artifacts that were built *for bionic*: the headers (``Python.h`` +
``pyconfig.h``) and ``libpython3.X.so`` that ``find_package(Python3 COMPONENTS
Interpreter Development)`` insists on resolving. Termux ships both inside its
single ``python`` deb, so downloading that deb is enough and no Termux container
image is required.

The script is deliberately stdlib-only: it runs on the CI runner (x86_64 Linux)
and on Termux itself, where it can be used to inspect the expected layout.

Example::

    python scripts/build_support/termux_sysroot.py \
        --dest /tmp/termux-sysroot \
        --write-env /tmp/termux-sysroot.env
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import lzma
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

DEFAULT_REPO = "https://packages.termux.dev/apt/termux-main"
DEFAULT_ARCH = "aarch64"
DEFAULT_PACKAGE = "python"
# Termux debs install into the Android app data directory; the same tree is
# produced by ``dpkg-deb -x`` on any Debian-flavoured host.
TERMUX_PREFIX_SUFFIX = Path("data/data/com.termux/files/usr")
INDEX_CANDIDATES = ("Packages.xz", "Packages.gz", "Packages")


def _fetch(url: str, *, retries: int = 3, timeout: int = 120) -> bytes:
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "openviking-android-build"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - retried below, surfaced afterwards
            last_error = exc
    raise RuntimeError(f"failed to download {url}: {last_error}")


def _decompress(name: str, payload: bytes) -> bytes:
    if name.endswith(".xz"):
        return lzma.decompress(payload)
    if name.endswith(".gz"):
        return gzip.decompress(payload)
    return payload


def parse_packages(text: str) -> list[dict[str, str]]:
    """Parse a Debian ``Packages`` index into a list of stanzas."""
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    key = ""
    for raw_line in text.splitlines():
        if not raw_line.strip():
            if current:
                stanzas.append(current)
                current = {}
            key = ""
            continue
        if raw_line.startswith((" ", "\t")):
            if key:
                current[key] = f"{current[key]}\n{raw_line.strip()}".strip()
            continue
        if ":" not in raw_line:
            continue
        key, _, value = raw_line.partition(":")
        key = key.strip()
        current[key] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def resolve_package(
    repo: str, arch: str, package: str, version_prefix: str = ""
) -> dict[str, str]:
    """Return the ``Packages`` stanza for ``package`` in the given Termux arch.

    ``version_prefix`` pins a CPython minor version (e.g. ``3.13``) when the
    artifacts have to match a device that has not upgraded yet.
    """
    index_root = f"{repo.rstrip('/')}/dists/stable/main/binary-{arch}"
    errors: list[str] = []
    for candidate in INDEX_CANDIDATES:
        url = f"{index_root}/{candidate}"
        try:
            payload = _fetch(url)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        for stanza in parse_packages(_decompress(candidate, payload).decode("utf-8", "replace")):
            if stanza.get("Package") != package:
                continue
            if version_prefix and not stanza.get("Version", "").startswith(version_prefix + "."):
                continue
            return stanza
        errors.append(f"{url} has no '{package}' stanza")
    raise RuntimeError("; ".join(errors) or f"no Packages index under {index_root}")


def _extract_deb_with_dpkg(deb: Path, dest: Path) -> bool:
    dpkg_deb = shutil.which("dpkg-deb")
    if not dpkg_deb:
        return False
    subprocess.run([dpkg_deb, "-x", str(deb), str(dest)], check=True)
    return True


def _ar_members(payload: bytes) -> dict[str, bytes]:
    """Read the members of a Unix ``ar`` archive (the outer deb container)."""
    if not payload.startswith(b"!<arch>\n"):
        raise ValueError("not an ar archive")
    members: dict[str, bytes] = {}
    offset = 8
    while offset + 60 <= len(payload):
        header = payload[offset : offset + 60]
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii", "replace").strip() or 0)
        start = offset + 60
        members[name] = payload[start : start + size]
        offset = start + size + (size % 2)
    return members


def _extract_deb_fallback(deb: Path, dest: Path) -> None:
    """Extract a deb without dpkg-deb (pure stdlib)."""
    members = _ar_members(deb.read_bytes())
    data_member = next((name for name in members if name.startswith("data.tar")), "")
    if not data_member:
        raise RuntimeError(f"{deb} has no data.tar member")
    data = members[data_member]
    if data_member.endswith(".zst"):
        raise RuntimeError("dpkg-deb is required to extract zstd-compressed debs")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        try:
            archive.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12 has no extraction filters
            archive.extractall(dest)


def extract_deb(deb: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if _extract_deb_with_dpkg(deb, dest):
        return
    _extract_deb_fallback(deb, dest)


def download_package(stanza: dict[str, str], repo: str, dest: Path) -> Path:
    """Download a package listed in a Termux ``Packages`` stanza."""
    filename = stanza.get("Filename", "")
    if not filename:
        raise RuntimeError(f"package '{stanza.get('Package')}' has no Filename field")
    target = dest / Path(filename).name
    if target.is_file() and _sha256(target) == stanza.get("SHA256", "").strip():
        return target
    payload = _fetch(f"{repo.rstrip('/')}/{filename.lstrip('/')}")
    expected = stanza.get("SHA256", "").strip()
    if expected and hashlib.sha256(payload).hexdigest() != expected:
        raise RuntimeError(f"checksum mismatch for {filename}")
    dest.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def find_prefix(root: Path) -> Path:
    """Locate the Termux ``$PREFIX`` inside an extracted deb tree."""
    nested = root / TERMUX_PREFIX_SUFFIX
    if nested.is_dir():
        return nested
    if (root / "include").is_dir() and (root / "lib").is_dir():
        return root
    raise RuntimeError(f"no Termux prefix found under {root}")


def describe_python(prefix: Path) -> dict[str, str]:
    """Return the environment consumed by the Android cross build."""
    include_dirs = sorted(
        (path for path in (prefix / "include").glob("python3.*") if path.is_dir()),
        key=lambda path: path.name,
    )
    if not include_dirs:
        raise RuntimeError(f"no CPython headers under {prefix / 'include'}")
    include_dir = include_dirs[-1]
    version = include_dir.name.removeprefix("python")
    shared_candidates = [
        prefix / "lib" / f"libpython{version}.so",
        *(prefix / "lib").glob(f"libpython{version}.so.*"),
    ]
    shared_object = next((path for path in shared_candidates if path.is_file()), None)
    if shared_object is None:
        raise RuntimeError(f"no libpython{version}.so under {prefix / 'lib'}")
    if not (include_dir / "pyconfig.h").is_file():
        raise RuntimeError(f"{include_dir} has no pyconfig.h (not a CPython header tree)")
    return {
        "TERMUX_PREFIX": str(prefix),
        "TERMUX_PY_VERSION": version,
        "TERMUX_PY_INCLUDE_DIR": str(include_dir),
        "TERMUX_PY_LIBRARY": str(shared_object),
        "TERMUX_PY_SHARED_OBJECT": str(shared_object),
    }


def materialize(
    *,
    repo: str = DEFAULT_REPO,
    arch: str = DEFAULT_ARCH,
    package: str = DEFAULT_PACKAGE,
    dest: Path,
    reuse: bool = True,
    version_prefix: str = "",
    expected_sha256: str = "",
) -> dict[str, str]:
    """Download + extract the Termux package, optionally enforcing an independent pin."""
    dest = dest.resolve()
    stanza = resolve_package(repo, arch, package, version_prefix)
    deb = download_package(stanza, repo, dest / "_downloads")
    digest = _sha256(deb)
    if expected_sha256 and digest != expected_sha256:
        raise RuntimeError("Termux Python deb does not match the independently pinned SHA256")
    extract_root = dest / "root"
    marker = dest / "sysroot-source.json"
    identity = {"deb_sha256": digest, "arch": arch, "package": package,
                "version": stanza.get("Version", "")}
    try:
        reusable = json.loads(marker.read_text()) == identity
    except (FileNotFoundError, ValueError):
        reusable = False
    prefix = find_prefix(extract_root) if reuse and reusable and extract_root.is_dir() else None
    if prefix is None:
        if extract_root.exists():
            shutil.rmtree(extract_root)
        extract_deb(deb, extract_root)
        prefix = find_prefix(extract_root)
        marker.write_text(json.dumps(identity, indent=2) + "\n")
    env = describe_python(prefix)
    if version_prefix and env["TERMUX_PY_VERSION"] != version_prefix:
        raise RuntimeError(
            f"resolved CPython {env['TERMUX_PY_VERSION']} does not match the requested "
            f"{version_prefix}"
        )
    env["TERMUX_PACKAGE"] = f"{package}={stanza.get('Version', 'unknown')}"
    env["TERMUX_DEB"] = str(deb)
    env["TERMUX_DEB_SHA256"] = digest
    env["TERMUX_REPO"] = repo
    return env


def write_env_file(path: Path, env: dict[str, str]) -> None:
    """Write ``KEY=VALUE`` lines, i.e. the format ``$GITHUB_ENV`` accepts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, required=True, help="working directory for download")
    parser.add_argument("--repo", default=os.environ.get("TERMUX_REPO", DEFAULT_REPO))
    parser.add_argument("--arch", default=DEFAULT_ARCH)
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument(
        "--python-version",
        default=os.environ.get("TERMUX_PY_VERSION_PIN", ""),
        help="pin the CPython minor version, e.g. 3.13 (default: newest in the repo)",
    )
    parser.add_argument("--expected-sha256", default="",
                        help="independent trusted target-deb SHA256, verified before extraction")
    parser.add_argument("--write-env", type=Path, help="write KEY=VALUE lines to this file")
    parser.add_argument("--no-reuse", action="store_true", help="re-extract the deb every run")
    args = parser.parse_args(argv)

    env = materialize(
        repo=args.repo,
        arch=args.arch,
        package=args.package,
        dest=args.dest,
        reuse=not args.no_reuse,
        version_prefix=args.python_version,
        expected_sha256=args.expected_sha256,
    )
    for key, value in env.items():
        print(f"{key}={value}")
    if args.write_env:
        write_env_file(args.write_env, env)
        print(f"wrote {args.write_env}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
