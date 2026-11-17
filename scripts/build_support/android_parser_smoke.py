"""Device-side smoke test for the complete pinned standalone parser wheel set.

Run in a clean Termux venv after installing all parser wheels. This validates
real grammar execution, not OpenViking's separate language-pack process() API.
"""
import argparse
import importlib
import importlib.metadata
import json
from pathlib import Path

SAMPLES = (
    ("python", "language", b"def hello():\n    return 1\n"),
    ("javascript", "language", b"function hello() { return 1; }"),
    ("typescript", "language_typescript", b"function hello(): number { return 1; }"),
    ("typescript", "language_tsx", b"const hello = () => <div>hello</div>;"),
    ("java", "language", b"class Hello { int value() { return 1; } }"),
    ("cpp", "language", b"int main() { return 0; }"),
    ("rust", "language", b"fn main() { let value = 1; }"),
    ("go", "language", b"package main\nfunc main() {}\n"),
    ("c_sharp", "language", b"class Hello { int Value() { return 1; } }"),
    ("php", "language_php", b"<?php function hello() { return 1; }"),
    ("php", "language_php_only", b"function hello() { return 1; }"),
    ("lua", "language", b"local function hello() return 1 end"),
)


def main(argv=None):
    from tree_sitter import Language, Parser

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    results = []
    for language, function, source in SAMPLES:
        module = importlib.import_module(f"tree_sitter_{language}")
        grammar = Language(getattr(module, function)())
        tree = Parser(grammar).parse(source)
        if tree.root_node.has_error or not tree.root_node.child_count:
            raise AssertionError(f"invalid parse for {language}/{function}: {tree.root_node}")
        result = {
            "language": language, "binding": function,
            "version": importlib.metadata.version("tree-sitter-" + language.replace("_", "-")),
            "root": tree.root_node.type,
        }
        results.append(result)
        print(f"PASS {language}/{function}: {tree.root_node.type}", flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(f"PASS: {len(results)} real parser variants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
