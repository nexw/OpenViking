"""Build the pinned C parser wheels for Termux, locally or with the Android NDK.

This is a dependency-wheel stage, NOT a complete OpenViking distribution. Sources
are checksum-pinned; target sysconfig, headers, libpython and exact-minor wheel
tags prevent host ABI leakage. Requires build, setuptools>=77 and wheel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import shlex
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).with_name("android_parser_sources.json")
GLIBC_LIBRARIES = ("libc.so.6", "libm.so.6", "libstdc++.so.6", "libgcc_s.so.1")


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fetch_source(source: dict, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{source['name']}-{source['version']}.tar.gz"
    if not archive.exists() or sha256(archive) != source["sha256"]:
        error = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(source["url"], timeout=120) as response:
                    payload = response.read()
                if hashlib.sha256(payload).hexdigest() != source["sha256"]:
                    raise ValueError(f"source checksum mismatch: {source['name']}")
                archive.write_bytes(payload)
                break
            except (OSError, ValueError) as exc:
                error = exc
        else:
            raise RuntimeError(f"cannot fetch {source['name']}: {error}")
    return archive


def unpack_source(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")
    roots = [p for p in destination.iterdir() if p.is_dir() and (p / "pyproject.toml").exists()]
    if len(roots) != 1:
        raise ValueError(f"expected one Python project in {archive}")
    return roots[0]


def patch_runtime(source: Path) -> dict:
    """Keep upstream's GCC workaround from being passed to Clang on aarch64."""
    setup = source / "setup.py"
    text = setup.read_text()
    old = 'if machine().startswith("aarch64"):'
    new = (
        'if machine().startswith("aarch64") and '
        '"clang" not in " ".join(self.compiler.compiler):'
    )
    if text.count(old) != 1:
        raise ValueError("tree-sitter optimizer workaround changed; review source patch")
    setup.write_text(text.replace(old, new))
    return {"patch": "gcc-only-early-inlining-workaround", "setup_sha256": sha256(setup)}


def preserve_runtime_notice(wheel: Path, project: Path, directory: Path) -> dict:
    """Include the embedded tree-sitter Unicode/ICU notice, not just binding MIT."""
    notice = project / "tree_sitter/core/lib/src/unicode/LICENSE"
    if not notice.is_file():
        raise ValueError("missing embedded tree-sitter Unicode license notice")
    unpacked = directory / "unpacked"
    subprocess.run([sys.executable, "-m", "wheel", "unpack", str(wheel),
                    "--dest", str(unpacked)], check=True)
    roots = list(unpacked.iterdir())
    if len(roots) != 1:
        raise ValueError("unexpected wheel unpack layout")
    dist_info = next(roots[0].glob("*.dist-info"))
    destination = dist_info / "licenses/tree-sitter-unicode/LICENSE"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(notice.read_bytes())
    subprocess.run([sys.executable, "-m", "wheel", "pack", str(roots[0]),
                    "--dest-dir", str(wheel.parent)], check=True)
    return {"unicode_license_sha256": sha256(notice)}


def target_environment(prefix: Path, version: str, api: int, cc: str, directory: Path) -> dict:
    """Use target ABI settings while replacing nonrelocatable Termux build paths."""
    configs = list((prefix / "lib" / f"python{version}").glob("_sysconfigdata_*.py"))
    if len(configs) != 1:
        raise ValueError(f"expected one target sysconfig module under {prefix}")
    config = dict(runpy.run_path(str(configs[0]))["build_time_vars"])
    if config.get("Py_GIL_DISABLED"):
        raise ValueError("free-threaded Python requires a separate wheel target")
    include = prefix / "include" / f"python{version}"
    if not (include / "Python.h").is_file() or not (include / "pyconfig.h").is_file():
        raise ValueError(f"missing target headers: {include}")
    if not (prefix / "lib" / f"libpython{version}.so").is_file():
        raise ValueError(f"missing target libpython{version}.so")
    if "aarch64" not in config.get("SOABI", "") or "android" not in config.get("SOABI", ""):
        raise ValueError(f"not an Android aarch64 sysconfig: {config.get('SOABI')}")
    cflags = f"-O2 -fPIC -I{shlex.quote(str(include))}"
    ldflags = (f"-L{shlex.quote(str(prefix / 'lib'))} -Wl,--no-as-needed "
               f"-Wl,--no-undefined -lpython{version}")
    overrides = {
        "CC": cc, "CXX": cc + "++", "LDSHARED": cc + " -shared",
        "BLDSHARED": cc + " -shared", "CFLAGS": cflags, "PY_CFLAGS": cflags,
        "PY_CFLAGS_NODIST": "", "LDFLAGS": ldflags, "PY_LDFLAGS": ldflags,
        "PY_LDFLAGS_NODIST": "", "INCLUDEPY": str(include), "CONFINCLUDEPY": str(include),
        "LIBDIR": str(prefix / "lib"), "AR": os.environ.get("AR", "llvm-ar"),
        "ARFLAGS": "rcs", "ANDROID_API_LEVEL": api,
    }
    config.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    module = directory / "ov_android_sysconfig.py"
    module.write_text("build_time_vars = " + repr(config) + "\n")
    env = os.environ.copy()
    env.update({key: str(value) for key, value in overrides.items()})
    env.update({
        "_PYTHON_SYSCONFIGDATA_NAME": module.stem,
        # CPython 3.14 derives the API from sysconfig and maps aarch64 to
        # arm64_v8a itself; this is a host tuple, not the final wheel tag.
        "_PYTHON_HOST_PLATFORM": "android-aarch64",
        "PYTHONPATH": str(directory),
    })
    env.pop("PYTHONHOME", None)
    return env


def inspect_wheel(wheel: Path, version: str, api: int, readelf: str) -> dict:
    tag = f"cp{version.replace('.', '')}-cp{version.replace('.', '')}-android_{api}_arm64_v8a"
    if not wheel.name.endswith(f"-{tag}.whl"):
        raise ValueError(f"wrong wheel tag: {wheel.name}")
    libraries = {}
    with zipfile.ZipFile(wheel) as archive, tempfile.TemporaryDirectory() as temporary:
        wheel_metadata = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        if len(wheel_metadata) != 1:
            raise ValueError(f"missing or ambiguous WHEEL metadata: {wheel}")
        tags = [line[5:] for line in archive.read(wheel_metadata[0]).decode().splitlines()
                if line.startswith("Tag: ")]
        if tags != [tag]:
            raise ValueError(f"internal wheel tags disagree: {tags}")
        extensions = [name for name in archive.namelist() if name.endswith(".so")]
        if not extensions:
            raise ValueError(f"no native payload in {wheel}")
        for number, name in enumerate(extensions):
            payload = archive.read(name)
            if (payload[:6] != b"\x7fELF\x02\x01" or payload[18:20] != b"\xb7\x00"):
                raise ValueError(f"not ELF64/little-endian/AArch64: {name}")
            if ".cpython-" in name and "aarch64-linux-android" not in name:
                raise ValueError(f"host extension suffix: {name}")
            binary = Path(temporary) / f"extension-{number}.so"
            binary.write_bytes(payload)
            dynamic = subprocess.check_output([readelf, "-d", str(binary)], text=True)
            if any(lib in dynamic for lib in GLIBC_LIBRARIES):
                raise ValueError(f"glibc dependency: {name}")
            if f"[libpython{version}.so]" not in dynamic:
                raise ValueError(f"missing target libpython dependency: {name}")
            symbols = subprocess.check_output(
                [readelf, "--dyn-syms", "--wide", str(binary)], text=True
            )
            entry = "PyInit_" + Path(name).name.split(".", 1)[0]
            if not any(entry == line.split()[-1] and " UND " not in line
                       for line in symbols.splitlines() if line.split()):
                raise ValueError(f"missing exported {entry}: {name}")
            libraries[name] = [line.split("[", 1)[1].split("]", 1)[0]
                               for line in dynamic.splitlines() if "(NEEDED)" in line]
    return {"wheel": wheel.name, "sha256": sha256(wheel), "needed": libraries}


def build_one(source: dict, prefix: Path, version: str, api: int, cc: str,
              cache: Path, output: Path, readelf: str) -> dict:
    archive = fetch_source(source, cache)
    with tempfile.TemporaryDirectory(prefix="android-parser-") as temporary:
        directory = Path(temporary)
        project = unpack_source(archive, directory / "source")
        patch = patch_runtime(project) if source["name"] == "tree-sitter" else {}
        env = target_environment(prefix, version, api, cc, directory / "config")
        wheels = directory / "wheels"
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel",
                        str(project), "--outdir", str(wheels)], env=env, check=True)
        built = list(wheels.glob("*.whl"))
        if len(built) != 1:
            raise ValueError(f"expected one wheel for {source['name']}")
        expected = f"{source['name'].replace('-', '_')}-{source['version']}-"
        if not built[0].name.startswith(expected):
            raise ValueError(f"source metadata disagrees with source manifest: {built[0].name}")
        if source["name"] == "tree-sitter":
            patch.update(preserve_runtime_notice(built[0], project, directory))
        py_tag = "cp" + version.replace(".", "")
        # Upstream grammar backends override get_tag(); update metadata as well
        # as the filename, because our explicit libpython linkage is minor-specific.
        subprocess.run([sys.executable, "-m", "wheel", "tags", "--python-tag", py_tag,
                        "--abi-tag", py_tag, "--platform-tag", f"android_{api}_arm64_v8a",
                        "--remove", str(built[0])], check=True)
        wheel = next(wheels.glob("*.whl"))
        result = inspect_wheel(wheel, version, api, readelf)
        result.update({"source": source, **patch})
        destination = output / wheel.name
        destination.write_bytes(wheel.read_bytes())
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--target-prefix", type=Path, required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--android-api", type=int, default=24)
    parser.add_argument("--cc", default="clang")
    parser.add_argument("--readelf", default="readelf")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--packages", nargs="+", help="subset, e.g. tree-sitter tree-sitter-php")
    args = parser.parse_args(argv)
    if args.python_version != f"{sys.version_info.major}.{sys.version_info.minor}":
        parser.error("build interpreter must match the target Python minor version")
    if args.android_api < 24:
        parser.error("Termux requires Android API >=24")
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("schema") != 1:
        parser.error("unsupported source manifest schema")
    sources = manifest["packages"]
    if args.packages:
        unknown = set(args.packages) - {item["name"] for item in sources}
        if unknown:
            parser.error(f"unknown packages: {sorted(unknown)}")
        sources = [item for item in sources if item["name"] in args.packages]
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for source in sources:
        print(f"Building {source['name']}=={source['version']}", flush=True)
        results.append(build_one(source, args.target_prefix.resolve(), args.python_version,
                                 args.android_api, args.cc, args.cache, args.out, args.readelf))
        (args.out / "parser-wheels.json").write_text(json.dumps({
            "schema": 1, "python": args.python_version, "android_api": args.android_api,
            "wheels": results,
        }, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
