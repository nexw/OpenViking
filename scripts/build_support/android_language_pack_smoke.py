"""Device acceptance test of the real offline language-pack implementation.

Run after installing the language-pack wheel and its tree-sitter dependency in
an isolated Termux environment. No application-wide installation is implied.
"""
import argparse
import importlib.metadata
import json
import os
import tempfile
import time
from pathlib import Path

SAMPLES = {
    "python": "import os\ndef hello():\n    return 1\n",
    "javascript": "function hello() { return 1; }",
    "typescript": "function hello(): number { return 1; }",
    "tsx": "export function hello() { return <div>Hello</div>; }",
    "java": "class Hello { int hello() { return 1; } }",
    "cpp": "int hello() { return 1; }",
    "rust": "fn hello() -> i32 { 1 }",
    "go": "package main\nfunc hello() int { return 1 }\n",
    "csharp": "class Hello { public int hello() { return 1; } }",
    "php": "<?php function hello() { return 1; }",
    "lua": "local function hello() return 1 end",
}


def structure_names(items):
    names = []
    for item in items:
        names.append(item.name)
        names.extend(structure_names(item.children))
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    # A cache miss must not be rescued by the machine's usual grammar cache.
    os.environ["TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"] = "http://127.0.0.1:1/disabled"
    import tree_sitter_language_pack as pack

    results = []
    with tempfile.TemporaryDirectory(prefix="offline-language-pack-") as cache:
        os.environ["XDG_CACHE_HOME"] = cache
        pack.prefetch(list(SAMPLES))
        for language, source in SAMPLES.items():
            tree = pack.get_parser(language).parse(source.encode())
            assert not tree.root_node.has_error, (language, tree.root_node)
            result = pack.process(source, pack.ProcessConfig(
                language=language, structure=True, imports=True,
            ))
            names = structure_names(result.structure)
            assert "hello" in names, (language, names)
            if language == "python":
                assert result.imports, "Python imports were not extracted"
            results.append({"language": language, "structure_names": names})
            print(f"PASS real parse/process/prefetch: {language}", flush=True)

        started = time.monotonic()
        assert not pack.has_language("ruby"), "unembedded Ruby advertised as available"
        assert pack.detect_language("example.rb") == "ruby"
        for action in (lambda: pack.prefetch(["ruby"]), lambda: pack.get_parser("ruby"),
                       lambda: pack.process("def hello; end", pack.ProcessConfig(language="ruby"))):
            try:
                action()
            except Exception as exc:
                print("PASS unsupported language rejected:", type(exc).__name__, flush=True)
            else:
                raise AssertionError("unembedded Ruby unexpectedly available")
        assert time.monotonic() - started < 5, "unsupported grammar did not fail promptly"
        for name, arguments in (
            ("download", (["python"],)), ("download_all", ()),
            ("download_group", ("web",)), ("manifest_languages", ()),
            ("downloaded_languages", ()), ("clean_cache", ()), ("cache_dir", ()),
            ("configure", ()), ("init", ()),
        ):
            try:
                getattr(pack, name)(*arguments)
            except RuntimeError as exc:
                assert "offline grammar set" in str(exc), (name, str(exc))
            else:
                raise AssertionError(f"download/cache API {name} was not disabled")
        assert not list(Path(cache).rglob("*.so")), "runtime grammar download/cache was used"

    report = {"version": importlib.metadata.version("tree-sitter-language-pack"),
              "languages": results, "unsupported_languages_fail_closed": True}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {len(results)} real process() language tests and offline policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
