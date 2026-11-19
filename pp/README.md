# pp-mupdf

A command-line tool on [MuPDF](https://mupdf.com) 1.28.6 that reads, writes and renders PDF
annotations, exchanging PDF objects as JSON files. It knows PDF objects, not annotation
semantics: the caller decides what to read and describes everything it writes.

Everything added to MuPDF lives in `pp/`. The rest of the tree is upstream MuPDF, unchanged
except for `.gitmodules`, whose relative submodule URLs point to Artifex's GitHub repositories
instead, and a one-line fix in `source/fitz/json.c` (the JSON parser dropped `\"`).

## Commands

    pp-mupdf version
    pp-mupdf info   <in.pdf> <out.json>
    pp-mupdf check  <in.pdf>
    pp-mupdf read   <in.pdf> <out.json> [--no-chars]
    pp-mupdf write  <in.pdf> <plan.json> <out.pdf>
    pp-mupdf render <in.pdf> <pageIndex> <info.json> <scale> <out.pam> [<scale> <out.pam> ...]

The exit code is the only failure signal; errors go to stderr. PDF objects in JSON follow
qpdf's JSON v2 conventions (`"/Name"`, `"u:text"`, `"b:hex"`, `"12 0 R"`), plus `"@id"` for
objects a write plan creates. See the comments in `pp/pp-mupdf.c` for each command's input
and output.

## Building

Needs a C compiler, make and Python 3 with [fontTools](https://github.com/fonttools/fonttools)
(only for the small variant).

    git submodule update --init thirdparty/brotli thirdparty/freetype thirdparty/jbig2dec \
        thirdparty/lcms2 thirdparty/libjpeg thirdparty/openjpeg thirdparty/zlib

    make -f pp/Makefile                 # full:  build/release/pp-mupdf
    make -f pp/Makefile fonts=stripped  # small: build/release-stripped/pp-mupdf

Platforms (MuPDF and its libraries are always linked statically):

    make -f pp/Makefile static=yes                  # Linux; with musl fully static
    make -f pp/Makefile arch=arm64                  # macOS, one architecture each, joined with lipo
    make -f pp/Makefile OS=MINGW CC=x86_64-w64-mingw32-gcc \
        AR=x86_64-w64-mingw32-ar STRIP=x86_64-w64-mingw32-strip   # Windows, cross-compiled

`.github/workflows/pp-mupdf.yml` builds both variants for Linux (x64, arm64), macOS
(universal) and Windows (x64), runs the tests on each, and attaches the binaries to a release
for tags `v*`. `make -f pp/Makefile test` runs the tests against a local build.

The full variant has every MuPDF font. The small variant keeps every font with its Unicode
map and metrics but without glyph outlines or layout tables (`pp/strip-font-outlines.py`):
it extracts the same text, but text that falls back to those fonts renders blank.
`pp/compare-text.sh` compares the text two builds extract from a list of PDFs.

## Updating MuPDF

    git remote add upstream https://github.com/ArtifexSoftware/mupdf.git
    git fetch upstream tag <version>
    git merge <version>

## License

GNU AGPL v3, like MuPDF (see `COPYING`).
