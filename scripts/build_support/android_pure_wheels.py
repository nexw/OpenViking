"""Build genuine upstream-supported pure wheels, without compiling then retagging.

No NDK/sysroot is involved. PEP 517 backends must naturally produce py3-none-any
wheels. This is a dependency subset, not a complete Android application release.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import tomllib

from scripts.build_support.android_parser_wheels import fetch_source, sha256, unpack_source
from scripts.build_support.android_published_wheels import source_commit
from scripts.build_support.android_rust_wheels import canonical_name, inspect_metadata

DEFAULT_MANIFEST = Path(__file__).with_name("android_pure_sources.json")
# Reviewed backend switches, NOT optional compilation failures or runtime selectors.
MODES = {
    "pyyaml": {"version": "6.0.3", "backend": "_pyyaml_pep517",
               "backend_path": ["packaging"],
               "environment": {"PYYAML_FORCE_LIBYAML": "0"}, "config_settings": {}},
    "rapidfuzz": {"version": "3.14.6", "backend": "scikit_build_core.build",
                  "backend_path": [], "environment": {},
                  "config_settings": {"wheel.cmake": "false"}},
}


def validate_source(source):
    mode = MODES.get(canonical_name(source["name"]))
    if mode is None or any(source.get(key) != value for key, value in mode.items()):
        raise ValueError("source must use an exact reviewed upstream pure-build mode")
    if (not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", ""))
            or not source.get("url", "").startswith("https://files.pythonhosted.org/")
            or source.get("license_files") != ["LICENSE"]):
        raise ValueError("pure source requires official archive/SHA256 and full upstream LICENSE")


def build_environment(source, inherited=None):
    validate_source(source)
    env = dict(os.environ if inherited is None else inherited)
    foreign = {"PYTHONHOME", "PYTHONPATH", "_PYTHON_HOST_PLATFORM",
               "_PYTHON_SYSCONFIGDATA_NAME", "_PYTHON_SYSCONFIGDATA_PATH",
               "CC", "CXX", "AR", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
               "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CMAKE_ARGS",
               "PYYAML_FORCE_LIBYAML", "RAPIDFUZZ_BUILD_EXTENSION", "RAPIDFUZZ_IMPLEMENTATION",
               "CIBUILDWHEEL", "CONDA_BUILD", "PIWHEELS_BUILD", "ARCHFLAGS", "MACOSX_DEPLOYMENT_TARGET"}
    for key in list(env):
        if key in foreign or key.startswith(("PYO3_", "TSLP_", "SKBUILD_", "SETUPTOOLS_SCM_")):
            env.pop(key)
    env.update(source["environment"])
    env["PYTHONNOUSERSITE"] = "1"
    return env


def inspect_pure_wheel(wheel, source, licenses):
    result = inspect_metadata(wheel, source, pure=True)
    filename = f"{canonical_name(source['name']).replace('-', '_')}-{source['version']}-py3-none-any.whl"
    if wheel.name != filename:
        raise ValueError("backend must naturally emit an exact py3-none-any filename")
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        root = next(name.rsplit("/", 1)[0] for name in members
                    if name.count("/") == 1 and name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(f"{root}/METADATA"))
        info = BytesParser().parsebytes(archive.read(f"{root}/WHEEL"))
        if info.get_all("Tag", []) != ["py3-none-any"]:
            raise ValueError("backend must naturally emit only the py3-none-any internal tag")
        if re.sub(r"\s+", "", metadata.get("Requires-Python", "")) != source["requires_python"]:
            raise ValueError("Requires-Python changed from reviewed upstream metadata")
        for name in members:
            with archive.open(name) as stream:
                magic = stream.read(8)
            if (magic.startswith((b"\x7fELF", b"MZ", b"!<arch>\n", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"))
                    or re.search(r"\.(?:so(?:\.[0-9]+)*|pyd|dll|dylib|a|o|exe)$", name, re.I)):
                raise ValueError(f"pure wheel contains a native payload: {name}")
        notices = []
        for source_path, expected in licenses.items():
            matches = [name for name in members if name.startswith(f"{root}/")
                       and "license" in name.lower()
                       and hashlib.sha256(archive.read(name)).hexdigest() == expected]
            if not matches:
                raise ValueError(f"wheel must preserve full upstream license bytes: {source_path}")
            notices.append({"source_path": source_path, "sha256": expected, "wheel_path": matches[0]})
    return {**result, "wheel": wheel.name, "sha256": sha256(wheel), "licenses": notices,
            "source": source, "native_payloads": [], "tags": ["py3-none-any"]}


def build_one(source, cache, output):
    validate_source(source)
    archive = fetch_source(source, cache)  # Independent SHA checked BEFORE extraction/execution.
    with tempfile.TemporaryDirectory(prefix="openviking-pure-source-") as temporary:
        temporary = Path(temporary)
        project = unpack_source(archive, temporary / "source")
        backend = tomllib.loads((project / "pyproject.toml").read_text())["build-system"]
        if (backend["build-backend"] != source["backend"]
                or backend.get("backend-path", []) != source["backend_path"]):
            raise ValueError("upstream PEP 517 backend changed")
        licenses = {name: sha256(project / name) for name in source["license_files"]}
        built = temporary / "built"
        command = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(built)]
        for key, value in source["config_settings"].items():
            command.extend(["--config-setting", f"{key}={value}"])
        subprocess.run(command, cwd=project, env=build_environment(source), check=True)
        wheels = list(built.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("each backend must produce exactly one pure wheel")
        result = inspect_pure_wheel(wheels[0], source, licenses)
        destination = output / wheels[0].name
        shutil.copyfile(wheels[0], destination)  # No repacking, renaming or tag repair.
        if sha256(destination) != result["sha256"]:
            raise ValueError("wheel changed during publication staging")
        return result


def tool_versions(pins):
    versions = {name: importlib.metadata.version(name) for name in pins}
    if versions != pins:
        raise ValueError("build tools differ from the reviewed pure-build pins")
    return versions


def build_batch(manifest_path, cache, output):
    data = manifest_path.read_bytes()
    manifest = json.loads(data)
    if manifest.get("schema") != 1 or not manifest.get("packages"):
        raise ValueError("pure manifest schema/packages are required")
    names = []
    for source in manifest["packages"]:
        validate_source(source)  # Entire batch preflight before fetching/executing ANY source.
        names.append(canonical_name(source["name"]))
    if len(names) != len(set(names)):
        raise ValueError("duplicate pure distribution")
    if output.is_symlink() or (output.exists() and (
            not output.is_dir() or any(output.iterdir()))):
        raise ValueError("output directory must be empty and not a symlink")
    cache, output = cache.resolve(), output.resolve()
    if cache == output or cache.is_relative_to(output):
        raise ValueError("cache must be outside output directory")
    versions = tool_versions(manifest["build_tools"])
    result = {"schema": 1, "stage": "upstream-pure-wheel-subset", "source_commit": source_commit(),
              "manifest_sha256": hashlib.sha256(data).hexdigest(),
              "host_python": platform.python_version(), "build_tools": versions, "wheels": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".android-pure-", dir=output.parent) as temporary:
        staged = Path(temporary) / "wheels"
        staged.mkdir()
        for source in manifest["packages"]:
            result["wheels"].append(build_one(source, cache, staged))
        (staged / "pure-wheels.json").write_text(json.dumps(result, indent=2) + "\n")
        if output.is_symlink() or (output.exists() and (
                not output.is_dir() or any(output.iterdir()))):
            raise ValueError("output directory must remain empty")
        os.replace(staged, output)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        build_batch(args.manifest, args.cache, args.out)
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
