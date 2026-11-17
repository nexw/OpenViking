# Termux/Android distribution: build stages and release gates

The native OpenViking wheel is **not yet a complete, dependency-installable Android
release**. This document tracks the remaining work rather than treating
`pip install --no-deps` or a successful cross-build as a release test.

## Supported target for the current prototype

- Termux (`com.termux` prefix), Android aarch64, minimum API 24.
- CPython 3.14, with the exact minor's `libpython3.14.so` available.
- No manylinux/glibc wheel relabeling, placeholder distributions, or parser shims.
- Native extension wheels linked to versioned libpython use conservative
  `cp314-cp314` tags. The earlier OpenViking native prototype still needs its
  overly broad abi3 tag corrected before general distribution.

## Stage 1: standalone parser dependencies

`Android Parser Wheels` is an opt-in hosted Ubuntu/NDK workflow. It builds all
11 pinned runtime/grammar distributions from
`scripts/build_support/android_parser_sources.json`.

The source manifest records exact archive hashes. PHP 0.24.1 has no PyPI sdist;
its complete official tagged source is pinned to a commit. Some other upstream
sdists omit parser headers or scanners, so their exact official version commits
are used too. The runtime's aarch64 GCC optimizer workaround is restricted to
GCC before invoking Clang; the applied setup.py digest is recorded.

The builder uses the target Python sysconfig, headers and libpython, not the
host's extension suffix/ABI. It updates both wheel filename and internal tags,
rejects unresolved symbols at link time, and checks AArch64 ELF, entry points,
libpython dependencies and glibc leakage. Wheels retain upstream license files;
the embedded runtime's Unicode/ICU notice is included as well. Artifact manifests
record wheel hashes and source provenance; a separate source artifact is uploaded.

### Local build

Use a build interpreter matching the target Python minor and install the tested
build tools **in the build environment**, not the system interpreter:

```sh
python -m pip install setuptools==84.2.1 wheel==0.48.0 build==1.6.1
python scripts/build_support/android_parser_wheels.py \
  --target-prefix /path/to/extracted/termux/usr \
  --python-version 3.14 --android-api 24 \
  --cc clang --cache /path/to/source-cache --out /path/to/wheelhouse
```

For a hosted cross-build, `--cc` is the NDK's
`aarch64-linux-android24-clang`, `AR` is its `llvm-ar`, and `--readelf` can point to
`llvm-readelf`. The workflow obtains the target prefix from a Termux Python deb.

### Device acceptance test

In a fresh, matching Python venv, install all wheels offline, without source
builds or system site-packages:

```sh
sha256sum -c SHA256SUMS
python -m pip install --no-index --only-binary=:all: --find-links . tree-sitter \
  tree-sitter-python tree-sitter-javascript tree-sitter-typescript \
  tree-sitter-java tree-sitter-cpp tree-sitter-rust tree-sitter-go \
  tree-sitter-c-sharp tree-sitter-php tree-sitter-lua
python android_parser_smoke.py
```

Ensure the matching Termux libpython can be found by the dynamic linker. For a
privately extracted interpreter, use a process-local library search path; do not
replace the device's system Python or preload a different Python minor.

Both the local prototype and the real hosted-NDK artifacts passed all 12 actual
parse variants (including TSX and both PHP variants) in a fresh private CPython
3.14.10 venv, with no LD_PRELOAD. Hosted run:
https://github.com/nexw/openviking-android-prebuild-verify/actions/runs/44020777022
The downloaded artifact's SHA256SUMS were verified before offline binary-only
installation and device tests. This is **not** a test of language-pack or the
complete OpenViking service.

## Stage 2: language-pack and the remaining binary closure

A recorded dependency audit of the current native prototype resolved 323
packages. Before building the standalone parser set, 35 dependencies lacked a
compatible published Android/pure wheel; PHP was already supplied locally. The
standalone stage covers 10 of those 35. Counts are a snapshot, not a permanent
lockfile: remaining dependency/build versions must be pinned for a release.

Remaining source-build groups include:

- Rust: orjson, pydantic-core, jiter, rpds-py, fastuuid, tiktoken, tokenizers,
  hf-xet, firecrawl-anydoc, frostwork, LiteLLM itself, and language-pack.
- C/C++: grpcio, lxml, Pillow, CFFI, pycares, argon2 bindings, cryptography,
  pycryptodome, brotli, regex, zope-interface, PyYAML and RapidFuzz.
  Some have supported pure-Python build modes or genuine Termux package recipes;
  use those explicitly and test them, not empty replacement wheels.
- External runtime libraries must also be pinned or declared as Termux package
  prerequisites (for example OpenSSL, libffi, Cairo, libxml2/libxslt and codecs).

Some resolved packages already publish genuine Android wheels, including
`aiohttp`, `xxhash`, `MarkupSafe`, `charset-normalizer` and `pypdfium2`; mirror
compatible wheels and their hashes rather than rebuilding needlessly.

### Important language-pack trap

The application uses `tree-sitter-language-pack==1.13.3`'s Rust `process()` API
and grep-ast integration. Installing the standalone grammars does not replace
this API. Its default downloader maps Android to Linux/aarch64, whose grammar
libraries cannot be used with bionic.

The first prototype should preserve its real API and build a bounded grammar
set into the extension with upstream `TSLP_LINK_MODE=static` and an explicit
`TSLP_LANGUAGES` list. Pin and verify upstream's parser source bundle, use a local
source URL, disable downloads, and test both supported and unsupported languages
offline. The opt-in `Android Offline Language Pack` workflow prototypes this
stage. It uses exact source/grammar hashes, an explicit embedded set, and Cargo
features that remove both the runtime downloader and dynamic grammar loading.
Its real `process()` and offline `prefetch()` implementations remain upstream
code; download/cache/configuration APIs explicitly raise errors rather than
pretend to succeed. OpenViking's optional cache-configuration call already
handles that error. Unsupported languages must also fail promptly.

Initial verification can explicitly resolve and archive the Cargo workspace
lockfile (`--resolve-lock`); repeatable builds must reuse its verified
`--cargo-lock` input. Source grammar notices and resolved Rust dependency notices
are included inside the wheel. This stage has not yet passed an Android
build/runtime acceptance test.

## Full-distribution acceptance criteria

Do not label a bundle generally distributable until all of these pass:

1. A fixed Python/API/architecture, dependency closure, build toolchain and source
   hashes; preserve all license notices and record patches.
2. A complete wheelhouse plus an installer that verifies checksums and checks
   Python/API compatibility before changing the destination.
3. Fresh isolated installation with `--no-index --only-binary=:all:` and **normal
   dependency resolution**; no `--no-deps`, inherited site-packages or compilation.
4. `pip check` or `uv pip check` succeeds.
5. Service startup and health checks, native storage/vector operations, code
   parsing via the real process engine, and representative ingestion pass.
6. No download fallback to glibc parser libraries; offline parser tests pass.
7. Repeat the installation and tests using the exact published artifacts, not
   developer build directories. A successful hosted cross-build alone is not
   evidence of device runtime success.
