"""Device acceptance test of the real offline language-pack implementation.

Run after installing the language-pack wheel and its tree-sitter dependency in
an isolated Termux environment. No application-wide installation is implied.
"""
import argparse
import importlib.metadata
import json
import os
import platform
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


def deny_network():
    """Deny network syscalls for this test process, including native Rust code.

    A process-local seccomp filter can only tighten Android's existing policy.
    Unsupported kernels fail the requested test instead of silently skipping it.
    """
    import ctypes
    import errno
    import socket

    if platform.machine().lower() not in ("aarch64", "arm64"):
        raise RuntimeError("network-denial syscall table is only for Android aarch64")

    class Instruction(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("instructions", ctypes.POINTER(Instruction))]

    # aarch64: socket, socketpair, connect, sendto, sendmsg. Return EPERM.
    instructions = [Instruction(0x20, 0, 0, 0)]  # BPF_LD|BPF_W|BPF_ABS: syscall number
    for syscall in (198, 199, 203, 206, 211):
        instructions.extend((Instruction(0x15, 0, 1, syscall),
                             Instruction(0x06, 0, 0, 0x00050000 | errno.EPERM)))
    instructions.append(Instruction(0x06, 0, 0, 0x7FFF0000))  # SECCOMP_RET_ALLOW
    filters = (Instruction * len(instructions))(*instructions)
    program = Program(len(filters), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.prctl.restype = ctypes.c_int
    for arguments in ((38, 1, 0, 0, 0),  # PR_SET_NO_NEW_PRIVS
                      (22, 2, ctypes.cast(ctypes.pointer(program), ctypes.c_void_p).value, 0, 0)):
        if libc.prctl(*arguments) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "could not enforce process-local offline test")
    try:
        connection = socket.socket()
    except OSError as exc:
        assert exc.errno == errno.EPERM, exc
    else:
        connection.close()
        raise AssertionError("seccomp did not block socket creation")
    print("PASS process-local seccomp denies native/Python network syscalls", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--grep-ast", action="store_true", help="also test grep-ast==0.9.0")
    parser.add_argument("--deny-network", action="store_true",
                        help="enforce offline tests with a process-local aarch64 seccomp filter")
    args = parser.parse_args(argv)
    if args.deny_network:
        deny_network()
    # A cache miss must not be rescued by the machine's usual grammar cache.
    os.environ["TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"] = "http://127.0.0.1:1/disabled"
    import tree_sitter_language_pack as pack

    version = importlib.metadata.version("tree-sitter-language-pack")
    runtime_version = importlib.metadata.version("tree-sitter")
    assert version == "1.13.3", version
    assert runtime_version == "0.25.2", runtime_version
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

        cpp = pack.process(
            "int *hello_ptr() { return nullptr; }\n"
            "namespace demo { int hello_nested() { return 1; } }",
            pack.ProcessConfig(language="cpp", structure=True, imports=True),
        )
        assert {"hello_ptr", "hello_nested"}.issubset(structure_names(cpp.structure))
        print("PASS C++ pointer and nested declarator regression", flush=True)

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
        try:
            pack.DownloadManager.new("1.13.3")
        except RuntimeError as exc:
            assert "offline grammar set" in str(exc)
        else:
            raise AssertionError("native DownloadManager unexpectedly available")
        if args.grep_ast:
            from grep_ast import TreeContext
            from grep_ast.tsl import USING_TSL_PACK
            assert importlib.metadata.version("grep-ast") == "0.9.0"
            assert USING_TSL_PACK, "grep-ast silently fell back to another parser package"
            context = TreeContext("hello.py", SAMPLES["python"], color=False)
            context.add_lines_of_interest([1])
            context.add_context()
            assert "hello" in context.format()
            started = time.monotonic()
            try:
                TreeContext("hello.rb", "def hello; end", color=False)
            except Exception:
                pass
            else:
                raise AssertionError("grep-ast unexpectedly parsed excluded Ruby")
            assert time.monotonic() - started < 5
            print("PASS grep-ast TreeContext and excluded-language rejection", flush=True)
        assert not list(Path(cache).rglob("*.so")), "runtime grammar download/cache was used"

    report = {"version": version, "runtime_version": runtime_version,
              "python_version": platform.python_version(), "grep_ast_tested": args.grep_ast,
              "network_syscalls_denied": args.deny_network,
              "languages": results, "unsupported_languages_fail_closed": True}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {len(results)} real process() language tests and offline policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
