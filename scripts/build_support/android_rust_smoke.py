"""Functional device tests for the pinned Rust dependency wheel component.

Install with normal offline dependency resolution in a fresh matching venv.
This is not an OpenViking application/service acceptance test.
"""
import argparse
import importlib
import importlib.metadata
import json
import platform
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).with_name("android_rust_sources.json")


def check_orjson():
    import orjson
    value = {"unicode": "终端", "numbers": [0, 1, 1000.5], "nested": {"ok": True}}
    assert orjson.loads(orjson.dumps(value)) == value
    try:
        orjson.loads(b'{"broken":')
    except orjson.JSONDecodeError:
        pass
    else:
        raise AssertionError("invalid JSON was accepted")
    return "orjson.orjson"


def check_pydantic_core():
    from pydantic_core import SchemaSerializer, SchemaValidator, ValidationError
    schema = {"type": "typed-dict", "fields": {
        "count": {"type": "typed-dict-field", "schema": {"type": "int"}},
    }}
    validator = SchemaValidator(schema)
    assert validator.validate_python({"count": "42"}) == {"count": 42}
    assert validator.validate_json(b'{"count":"42"}') == {"count": 42}
    assert SchemaSerializer(schema).to_json({"count": 42}) == b'{"count":42}'
    try:
        validator.validate_python({"count": "not-an-integer"})
    except ValidationError:
        pass
    else:
        raise AssertionError("invalid integer was accepted")
    return "pydantic_core._pydantic_core"


def check_jiter():
    import jiter
    value = {"unicode": "终端", "items": [None, True, 42, 1.5]}
    assert jiter.from_json(json.dumps(value, ensure_ascii=False).encode()) == value
    try:
        jiter.from_json(b'{"broken":')
    except ValueError:
        pass
    else:
        raise AssertionError("invalid JSON was accepted")
    return "jiter" if jiter.__file__.endswith(".so") else "jiter.jiter"


def check_rpds():
    import rpds
    from rpds import HashTrieMap, HashTrieSet
    original = HashTrieMap([("a", 1)])
    updated = original.insert("b", 2)
    assert original["a"] == 1 and "b" not in original
    assert updated["a"] == 1 and updated["b"] == 2
    values = HashTrieSet([1, 2])
    changed = values.insert(3)
    assert 3 not in values and 3 in changed and len(changed) == 3
    return "rpds" if rpds.__file__.endswith(".so") else "rpds.rpds"


def check_fastuuid():
    import uuid

    import fastuuid

    random = fastuuid.uuid4()
    standard = uuid.UUID(bytes=random.bytes)
    assert random.version == standard.version == 4
    assert standard.variant == uuid.RFC_4122
    assert str(random) == str(standard)
    assert fastuuid.UUID(bytes=random.bytes).int == standard.int
    # The upstream API takes a native UUID namespace and BYTES, unlike stdlib.
    namespace = fastuuid.UUID(bytes=uuid.NAMESPACE_DNS.bytes)
    named = fastuuid.uuid5(namespace, b"python.org")
    expected = uuid.uuid5(uuid.NAMESPACE_DNS, "python.org")
    assert named.version == 5
    assert named.bytes == expected.bytes
    assert str(named) == str(expected) == "886313e1-3b8a-5372-9b90-0c9aee199e5d"
    return "fastuuid" if fastuuid.__file__.endswith(".so") else "fastuuid.fastuuid"


def check_frostwork():
    import frostwork
    from frostwork import _frostwork as native

    # Exercise native entity decoding + WHATWG encoding with no optional extras.
    text = '<meta charset="windows-1252"><p>caf\u00e9 &amp; tea &#x20ac;</p>'
    body = text.encode("cp1252")
    assert native.detect_encoding(body) == "windows-1252"
    decoded = native.decode(body, "windows-1252")
    assert decoded == text and decoded.encode("cp1252") == body
    expected = [["caf\u00e9 & tea \u20ac"]]
    assert native.extract(body, ["p::text"]) == expected
    assert frostwork.extract(decoded, ["//p/text()"]) == expected
    # The genuine URL API resolves relative URLs against the document's base.
    html = b'<base href="/shop/"><meta http-equiv="refresh" content="5;url=sale">'
    url = "https://example.com/p"
    navigation = ("https://example.com/shop/", (5.0, "https://example.com/shop/sale"))
    assert native.navigation(html, url) == navigation
    wrapped = frostwork.navigation(html, url)
    assert wrapped.base_url == navigation[0]
    assert wrapped.meta_refresh is not None
    assert wrapped.meta_refresh.delay == navigation[1][0]
    assert wrapped.meta_refresh.url == navigation[1][1]
    return "frostwork._frostwork"


CHECKS = {"orjson": check_orjson, "pydantic-core": check_pydantic_core,
          "jiter": check_jiter, "rpds-py": check_rpds,
          "fastuuid": check_fastuuid, "frostwork": check_frostwork}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--packages", nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    pins = {source["name"]: source["version"] for source in manifest["packages"]}
    selected = args.packages or list(pins)
    if set(selected) - CHECKS.keys() or set(selected) - pins.keys():
        parser.error("unknown or unpinned functional test package")
    for source in manifest.get("support_wheels", []):
        assert importlib.metadata.version(source["name"]) == source["version"], source["name"]
    results = []
    for name in selected:
        version = importlib.metadata.version(name)
        assert version == pins[name], (name, version, pins[name])
        native_name = CHECKS[name]()
        native = importlib.import_module(native_name)
        assert native.__file__.endswith(".so"), (native_name, native.__file__)
        results.append({"name": name, "version": version, "native_module": native_name})
        print(f"PASS real native functionality: {name}=={version}", flush=True)
    report = {"python_version": platform.python_version(), "results": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
