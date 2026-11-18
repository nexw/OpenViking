"""Shared Android/Termux Rust cross environment and genuine Cargo notices."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import tomllib

from scripts.build_support.android_parser_wheels import sha256

TARGET = "aarch64-linux-android"


def build_environment(prefix: Path, version: str, api: int, cc: str,
                      target_dir: Path | None = None) -> dict:
    """Link the exact target libpython, never a host interpreter or its flags."""
    if api < 24:
        raise ValueError("Termux requires Android API >=24")
    if not re.fullmatch(r"3\.\d+", version):
        raise ValueError("expected a Python major.minor version")
    if not (prefix / "lib" / f"libpython{version}.so").is_file():
        raise ValueError("missing target libpython")
    if any(char.isspace() for char in str(prefix)):
        raise ValueError("the Rust target prefix must not contain whitespace")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("PYO3_", "TSLP_")) or key in (
            "PYTHONHOME", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "PROJECT_ROOT",
        ):
            env.pop(key)
    ndk_bin = Path(cc).parent
    env.update({
        "PYO3_CROSS": "1", "PYO3_CROSS_PYTHON_VERSION": version,
        "PYO3_CROSS_LIB_DIR": str(prefix / "lib"),
        "CC_aarch64_linux_android": cc, "CXX_aarch64_linux_android": cc + "++",
        "AR_aarch64_linux_android": str(ndk_bin / "llvm-ar"),
        "CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER": cc,
        # Keep libpython even when maturin suppresses PyO3's link directives.
        "CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS": (
            f"-Lnative={prefix / 'lib'} -Clink-arg=-Wl,--no-as-needed "
            f"-Clink-arg=-lpython{version} -Clink-arg=-Wl,--no-undefined"
        ),
        "CARGO_BUILD_JOBS": env.get("CARGO_BUILD_JOBS", "2"),
        "CARGO_PROFILE_RELEASE_LTO": "false",
        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "16",
    })
    if target_dir is not None:
        env["CARGO_TARGET_DIR"] = str(target_dir.resolve())
    return env


def _notice_files(root: Path, license_file: str | None = None) -> list[Path]:
    """Include vendored/subdirectory notices too, without following symlinks out."""
    files = set()
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in (".git", "target", "__pycache__"))
        for name in names:
            path = Path(directory) / name
            if name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
                if path.is_file() and path.resolve().is_relative_to(root.resolve()):
                    files.add(path)
    if license_file:
        path = root / license_file
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"missing or unsafe Cargo license-file: {license_file}")
        files.add(path)
    return sorted(files)


def maturin_cargo_options(project: Path) -> list[str]:
    """Resolve notices with the SAME Cargo features maturin reads for the build.

    Default-only metadata omits optional dependencies such as frostwork's PyO3,
    which is activated by tool.maturin.features=["python"]. Never substitute
    --all-features: that would include unbuilt features (e.g. frostwork mutate).
    """
    pyproject = project / "pyproject.toml"
    if not pyproject.is_file():
        return []
    config = tomllib.loads(pyproject.read_text()).get("tool", {}).get("maturin", {})
    features = config.get("features", [])
    if (not isinstance(features, list) or any(
            not isinstance(feature, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_][A-Za-z0-9_-]*)?", feature)
            for feature in features)):
        raise ValueError("invalid maturin Cargo features for notice resolution")
    options = ["--features", ",".join(features)] if features else []
    for setting in ("all-features", "no-default-features"):
        value = config.get(setting, False)
        if type(value) is not bool:
            raise ValueError(f"maturin {setting} must be boolean")
        if value:
            options.append("--" + setting)
    return options


def include_notices(project: Path, wheel: Path, directory: Path, env: dict,
                    cargo_manifest: Path) -> dict:
    """Repack with real source/Cargo notices and SPDX metadata, updating RECORD."""
    options = maturin_cargo_options(project)
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--format-version", "1",
         "--filter-platform", TARGET, "--manifest-path", str(cargo_manifest), *options],
        cwd=project, env=env, text=True,
    ))
    if Path(metadata["workspace_root"]).resolve() != project.resolve():
        raise ValueError("Cargo workspace root must be the extracted source root")
    unpacked = directory / "notice-unpacked"
    subprocess.run([sys.executable, "-m", "wheel", "unpack", str(wheel),
                    "--dest", str(unpacked)], check=True)
    roots = list(unpacked.iterdir())
    if len(roots) != 1:
        raise ValueError("unexpected wheel unpack layout")
    dist_infos = list(roots[0].glob("*.dist-info"))
    if len(dist_infos) != 1:
        raise ValueError("missing or ambiguous wheel dist-info")
    dist_info = dist_infos[0]

    def copy_notices(root: Path, destination: Path, license_file=None) -> list[dict]:
        copied = []
        for path in _notice_files(root, license_file):
            output = destination / path.relative_to(root)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(path.read_bytes())
            copied.append({"file": output.relative_to(dist_info).as_posix(),
                           "source_file": path.relative_to(root).as_posix(),
                           "sha256": sha256(output)})
        return copied

    notices = dist_info / "licenses"
    # Include the sdist's own notices even for a virtual workspace root.
    source_notices = copy_notices(project, notices / "source")
    packages = []
    for number, package in enumerate(sorted(metadata["packages"], key=lambda item: item["id"])):
        root = Path(package["manifest_path"]).parent
        # The numeric prefix also disambiguates equal name/version from different registries.
        destination = notices / "rust" / f"{number:04d}-{package['name']}-{package['version']}"
        copied = copy_notices(root, destination, package.get("license_file"))
        packages.append({"name": package["name"], "version": package["version"],
                         "id": f"{package.get('source') or 'workspace'}#{package['name']}@{package['version']}",
                         "license": package.get("license"),
                         "source": package.get("source"), "notices": copied})
    result = {"source_notices": source_notices, "rust_packages": packages,
              "cargo_metadata_options": options}
    (dist_info / "ANDROID-THIRD-PARTY.json").write_text(json.dumps(result, indent=2) + "\n")
    subprocess.run([sys.executable, "-m", "wheel", "pack", str(roots[0]),
                    "--dest-dir", str(wheel.parent)], check=True)
    return {"rust_packages": len(packages), "cargo_metadata_options": options,
            "metadata": f"{dist_info.name}/ANDROID-THIRD-PARTY.json"}
