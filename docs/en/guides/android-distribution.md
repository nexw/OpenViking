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
`llvm-readelf`. Output directories must be empty; failed batches do not publish
partial wheelhouses or a completion manifest.

The hosted workflows use `scripts/build_support/android_target.json`: CPython
3.14.10, its independently pinned Termux deb SHA256, NDK 30.0.16248370, and (for
Rust extensions) Rust 1.100.0. The target deb is verified **before** extraction or
executing its sysconfig. Mirror/package changes fail closed; update the reviewed
pins intentionally rather than silently selecting new toolchains. Downloadable
`toolchain.json` records the source commit, resolved package/deb hash, target
sysconfig/libpython hashes, compiler, interpreter and installed build-tool
versions. SHA256SUMS also covers metadata and the device smoke scripts.

### Device acceptance test

In a fresh, matching Python venv, install all wheels offline, without source
builds or system site-packages:

```sh
sha256sum -c SHA256SUMS
python -m pip install --no-index --only-binary=:all: --find-links . tree-sitter \
  tree-sitter-python tree-sitter-javascript tree-sitter-typescript \
  tree-sitter-java tree-sitter-cpp tree-sitter-rust tree-sitter-go \
  tree-sitter-c-sharp tree-sitter-php tree-sitter-lua
python android_parser_smoke.py --manifest source-manifest.json --output parser-smoke.json
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
standalone stage covers 10 of those 35. The successful 322-dependency snapshot is
now recorded in `scripts/build_support/android_dependency_constraints.txt`, and
component tests require their pins to agree with that snapshot. Apply it with
`-c` while normally resolving OpenViking. It is a **candidate constraints file**,
not proof that every wheel exists or a hash-complete release lock; remaining
sources/build inputs and all runtime acceptance gates still need validation.

Remaining source-build groups include:

- Rust: orjson, pydantic-core, jiter, rpds-py, fastuuid, tiktoken, tokenizers,
  hf-xet, firecrawl-anydoc, frostwork, and LiteLLM itself.
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

The opt-in `Android Offline Language Pack` workflow builds eleven embedded
languages: Python, JavaScript, TypeScript, TSX, Java, C++, Rust, Go, C#, PHP and
Lua. It uses upstream `TSLP_LINK_MODE=static` and an explicit `TSLP_LANGUAGES`
list with a checksum-verified local source bundle. It uses exact source/grammar hashes, an explicit embedded set, and Cargo
features that remove both the runtime downloader and dynamic grammar loading.
Its real `process()` and offline `prefetch()` implementations remain upstream
code; download/cache/configuration APIs explicitly raise errors rather than
pretend to succeed. OpenViking's optional cache-configuration call already
handles that error. Unsupported languages must also fail promptly.

The workflow uses the checked-in, independently hashed Cargo workspace lock
with `--locked`. `--resolve-lock` exists only for explicit prototype/update work.
Grammar notices and resolved Rust dependency notices are included inside the
wheel. Pure `grep-ast==0.9.0` and `pathspec==1.1.1` wheels are mirrored with
pinned hashes for the optional integration test.

Device acceptance caught an upstream C++ defect: function names nested inside
declarator nodes were missing from `process().structure`. The builder applies
a guarded, recorded patch to the exact checksum-pinned core crate; it does not
replace `process()` with a Python shim or weaken the assertion. Tests cover
ordinary functions, pointer-return functions and namespace-nested functions.

A real hosted NDK build and Termux CPython 3.14.10 acceptance passed all eleven
languages, structure/import extraction, real `prefetch()`, and excluded-language
failures, without LD_PRELOAD:
https://github.com/nexw/openviking-android-prebuild-verify/actions/runs/44026271032
The same artifact also passed real grep-ast `TreeContext` integration with a
process-local seccomp filter denying network syscalls, including native Rust
network calls. This remains a **parser-component** test, not application/service
acceptance.

From the language-pack artifact directory, with the standalone parser wheelhouse
available and a fresh matching venv:

```sh
sha256sum -c SHA256SUMS
python -m pip install --no-index --only-binary=:all: \\
  --find-links . --find-links /path/to/parser-wheelhouse \\
  tree-sitter-language-pack==1.13.3 grep-ast==0.9.0
python android_language_pack_smoke.py --grep-ast --deny-network \\
  --output language-pack-smoke.json
```

`--deny-network` only tightens this test process's Android/aarch64 seccomp
policy. It verifies socket creation returns EPERM; unsupported kernels fail the
requested test explicitly instead of silently skipping enforcement. The normal
smoke test can also run without that flag. No system Python or global loader
configuration needs to be changed.

### Locked parser-component acceptance

The pinned workflows were rerun, and their exact downloadable artifacts were
installed together in a **new** private CPython 3.14.10 venv using normal
dependency resolution, `--no-index --only-binary=:all:`, and no system
site-packages. All 14 distributions passed `uv pip check`; the shipped smoke
scripts passed all 12 standalone parse variants and all 11 language-pack
variants, C++ regressions, grep-ast integration and seccomp-enforced offline
tests. No LD_PRELOAD was used.

- Pinned standalone parsers: https://github.com/nexw/openviking-android-prebuild-verify/actions/runs/44028777341
- Pinned language-pack: https://github.com/nexw/openviking-android-prebuild-verify/actions/runs/44028772195

These verification-repository URLs are historical evidence, not workflow
configuration. Upstream-owned artifact/device acceptance must be repeated when
upstream enables the workflows.

## Stage 3: foundational Rust dependencies

The opt-in `Android Rust Dependency Wheels` workflow builds the current audited
closure's `orjson==3.13.0`, `pydantic-core==2.50.4`, `jiter==0.19.0` and
`rpds-py==2026.9.1`. It preserves their real upstream maturin/PyO3 modules and
shipped Cargo locks. The support wheel `typing-extensions==4.16.0` is mirrored
unchanged for normal offline dependency resolution. The actual NDK artifact from
https://github.com/nexw/openviking-android-prebuild-verify/actions/runs/44033731115
passed SHA256 verification, a fresh five-package offline/binary-only install
with normal dependency resolution, `uv pip check`, and all four real functional
tests on private Termux CPython 3.14.10 without LD_PRELOAD. The shared Rust
cross-environment also passed the language-pack rebuild:
https://github.com/nexw/openviking-android-prebuild-verify/actions/runs/44032288106

The orjson build script uses build-host cfg for AVX-512 selection; a guarded
source patch makes this decision use Cargo's target architecture/OS instead.
Patch/source/lock hashes, genuine license notices, exact-minor tags and native
linkage are recorded and checked. Missing locks are errors, not an invitation to
resolve new versions silently. The separate source manifest uses the successful
322-dependency audit, not the repository's older uv.lock-derived snapshot.

```sh
sha256sum -c SHA256SUMS
python -m pip install --no-index --only-binary=:all: --find-links . \\
  orjson==3.13.0 pydantic-core==2.50.4 jiter==0.19.0 rpds-py==2026.9.1
python android_rust_smoke.py --manifest source-manifest.json --output rust-smoke.json
```

The functional test checks JSON roundtrips/error handling, actual pydantic-core
validation/serialization, and persistent Rust map/set operations—not just
imports. Fastuuid needs its upstream `uuid_unstable` cfg preserved, and tiktoken
needs a setuptools-rust adapter plus a reviewed Cargo lock; neither is silently
substituted into this maturin-only stage.

## Stage 4: unchanged published wheels

`android_published_wheels.json` freezes filenames, source URLs and hashes for
286 compatible wheels: 281 pure wheels and five published Android wheels. Its
36-item source-build queue includes the 16 native distributions covered by the
parser/language-pack/foundational-Rust stages; **20 native distributions remain**
until further builders pass acceptance. The queue is explicit data, never
placeholder wheels. Both queues partition the same 322-dependency constraints.

The opt-in `Android Published Dependency Wheels` workflow mirrors these upstream
wheels unchanged. It checks target tags (including compatible lower Android API
levels), Requires-Python, metadata, CRC and native ELF dependencies without
executing their code. No Linux wheel is relabeled. This download-only stage does
not need a target sysroot or NDK; downloading the published subset is not a full
installation or service test.

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
