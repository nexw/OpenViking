"""Real PyYAML/RapidFuzz pure-backend functionality in a fresh environment."""
import argparse
import importlib.metadata
import json
import os
import platform
import re
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).with_name("android_pure_sources.json")


def check_yaml():
    import yaml

    assert yaml.__with_libyaml__ is False
    assert not hasattr(yaml, "CLoader")
    value = {"message": "Termux 原生", "values": [1, True, None], "nested": {"x": 3.5}}
    dumped = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    assert "Termux 原生" in dumped
    assert yaml.safe_load(dumped) == value
    assert yaml.safe_load("list:\n - α\n - β\nflag: true\n") == {
        "list": ["α", "β"], "flag": True}
    try:
        yaml.safe_load("!!python/object/apply:builtins.str [unsafe]")
    except yaml.constructor.ConstructorError:
        pass
    else:
        raise AssertionError("safe_load accepted an unsafe Python-object constructor")
    return {"libyaml": False, "unicode_roundtrip": True, "unsafe_constructor_rejected": True}


def check_rapidfuzz():
    from rapidfuzz import fuzz, process, utils
    from rapidfuzz.distance import DamerauLevenshtein, Levenshtein

    # Default upstream selection must fall back naturally; no native file or shim.
    assert fuzz.ratio.__module__ == "rapidfuzz.fuzz_py", fuzz.ratio.__module__
    assert process.extractOne.__module__ == "rapidfuzz.process_py", process.extractOne.__module__
    assert Levenshtein.distance.__module__ == "rapidfuzz.distance.Levenshtein_py"
    assert fuzz.ratio("abcd", "abce") == 75.0
    assert fuzz.partial_ratio("中文Termux", "Termux") == 100.0
    assert Levenshtein.distance("kitten", "sitting") == 3
    assert DamerauLevenshtein.distance("CA", "AC") == 1
    assert process.extractOne("Termux", ["other", "Termux", "term"]) == ("Termux", 100.0, 1)
    assert utils.default_process("Termux-原生") == "termux 原生"
    return {"implementation": fuzz.ratio.__module__, "ratio": 75.0,
            "levenshtein": 3, "transposition": 1, "unicode": True, "extract_one": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--implementation", choices=("default", "python"), default="default")
    args = parser.parse_args(argv)
    if not __debug__:
        parser.error("smoke checks cannot run with Python assertions disabled")
    if os.environ.get("RAPIDFUZZ_IMPLEMENTATION") is not None:
        parser.error("start a fresh process with RAPIDFUZZ_IMPLEMENTATION unset")
    if args.implementation == "python":
        os.environ["RAPIDFUZZ_IMPLEMENTATION"] = "python"
    manifest = json.loads(args.manifest.read_text())
    expected = {source["name"]: source["version"] for source in manifest["packages"]}
    if set(expected) != {"pyyaml", "rapidfuzz"}:
        parser.error("smoke requires exactly the reviewed PyYAML/RapidFuzz set")
    for name, version in expected.items():
        assert importlib.metadata.version(name) == version, f"incorrect installed {name} version"
        distribution = importlib.metadata.distribution(name)
        assert distribution.files is not None, f"missing installed RECORD for {name}"
        assert not any(re.search(r"\.(?:so(?:\.[0-9]+)*|pyd|dll|dylib|a|o|exe)$", str(path), re.I)
                       for path in distribution.files), f"installed native payload in {name}"
    report = {"python": platform.python_version(), "platform": platform.platform(),
              "versions": expected, "selection": args.implementation,
              "pyyaml": check_yaml(), "rapidfuzz": check_rapidfuzz()}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
