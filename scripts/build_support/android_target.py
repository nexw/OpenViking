"""Trusted target pins and downloadable toolchain provenance for Android jobs.

Run with -m scripts.build_support.android_target. The deb's independent digest is
checked before target extraction/sysconfig execution, not only after the build.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from scripts.build_support.termux_sysroot import describe_python, write_env_file

DEFAULT_TARGET = Path(__file__).with_suffix(".json")


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load_target(path):
    target = json.loads(Path(path).read_text())
    if target.get("schema") != 1 or target.get("architecture") != "aarch64":
        raise ValueError("unsupported Android target profile")
    if not re.fullmatch(r"[0-9a-f]{64}", target["termux_python_deb_sha256"]):
        raise ValueError("invalid independent Python deb checksum")
    if not target["build_python"].startswith(target["python_minor"] + "."):
        raise ValueError("build interpreter and target minor disagree")
    return target


def target_env(target):
    return {
        "TERMUX_PY_VERSION_PIN": target["python_minor"],
        "TERMUX_PYTHON_DEB_SHA256": target["termux_python_deb_sha256"],
        "ANDROID_BUILD_PYTHON": target["build_python"],
        "ANDROID_NDK_REVISION": target["ndk_revision"],
        "ANDROID_RUST_TOOLCHAIN": target["rust_toolchain"],
    }


def record(target, prefix, compiler):
    deb = Path(os.environ["TERMUX_DEB"])
    deb_digest = digest(deb)
    if deb_digest != target["termux_python_deb_sha256"]:
        raise ValueError("provenance target deb differs from independently pinned input")
    resolved = describe_python(prefix)
    if resolved["TERMUX_PY_VERSION"] != target["python_minor"]:
        raise ValueError("resolved target Python minor differs from pinned input")
    if platform.python_version() != target["build_python"]:
        raise ValueError("build interpreter patch version differs from pinned input")
    ndk_version = os.environ.get("NDK_VERSION")
    if ndk_version != target["ndk_revision"]:
        raise ValueError("resolved NDK revision differs from pinned input")
    tools = {}
    for name in ("setuptools", "wheel", "build", "maturin"):
        try:
            tools[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    config = list((prefix / "lib" / f"python{target['python_minor']}").glob("_sysconfigdata_*.py"))
    if len(config) != 1:
        raise ValueError("expected one target sysconfig file")
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except subprocess.CalledProcessError:
        commit = os.environ.get("GITHUB_SHA", "")
    rust = {}
    if "maturin" in tools:
        rust = {"rustc": subprocess.check_output(["rustc", "-vV"], text=True),
                "cargo": subprocess.check_output(["cargo", "--version"], text=True)}
    return {
        "schema": 1, "commit": commit, "target": target,
        "termux_package": os.environ.get("TERMUX_PACKAGE"),
        "termux_repo": os.environ.get("TERMUX_REPO"),
        "termux_deb_sha256": deb_digest,
        "target_sysconfig_sha256": digest(config[0]),
        "target_libpython_sha256": digest(resolved["TERMUX_PY_LIBRARY"]),
        "ndk_revision": ndk_version, "python": sys.version,
        "build_tools": tools, "rust": rust,
        "compiler": subprocess.check_output([compiler, "--version"], text=True),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    commands = parser.add_subparsers(dest="command", required=True)
    environment = commands.add_parser("env")
    environment.add_argument("--out", type=Path, required=True)
    provenance = commands.add_parser("record")
    provenance.add_argument("--prefix", type=Path, required=True)
    provenance.add_argument("--compiler", required=True)
    provenance.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    target = load_target(args.target)
    if args.command == "env":
        write_env_file(args.out, target_env(target))
    else:
        data = record(target, args.prefix.resolve(), args.compiler)
        args.out.write_text(json.dumps(data, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
