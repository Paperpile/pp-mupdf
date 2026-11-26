# pp-mupdf: MuPDF's libraries built for PDF only, without JavaScript, linked into one tool.
# Run from the repository root:
#
#   make -f pp/Makefile                 # full: every MuPDF font; reads, writes, renders
#   make -f pp/Makefile fonts=stripped  # small: same text, fallback fonts render blank
#   make -f pp/Makefile test            # pp/tests against the build (add fonts=stripped)
#
# The full build is for anything that renders pages (thumbnails). The small build keeps every
# font MuPDF can choose with its Unicode map and metrics but without glyph outlines or layout
# tables (pp/strip-font-outlines.py, needs fontTools), so it extracts byte-identical text
# (pp/compare-text.sh) at a fraction of the size; text drawn with a fallback font renders
# blank. The 14 standard PDF fonts keep their outlines in both.
#
# Platforms:
#   Linux    make -f pp/Makefile static=yes          fully static (with musl: no libc needed)
#   macOS    make -f pp/Makefile arch=arm64|x86_64   one architecture; lipo joins them
#   Windows  make -f pp/Makefile OS=MINGW CC=x86_64-w64-mingw32-gcc \
#                AR=x86_64-w64-mingw32-ar STRIP=x86_64-w64-mingw32-strip   (cross, static)
#
# MuPDF and its third-party libraries are always linked statically. Upstream files stay
# untouched; this file only passes switches to MuPDF's Makefile.

build ?= release
OS ?= $(shell uname)
STRIP ?= strip

# The HTML engine lays out the annotation summary (compose); SVG draws its inline icons.
# Neither opens documents: the HTML, EPUB, ... and SVG document handlers stay off.
MUPDF_SWITCHES := build=$(build) mujs=no xps=no extract=no tesseract=no barcode=no USE_CMARK_GFM=no
FEATURE_FLAGS := -DFZ_ENABLE_HTML_ENGINE=1 -DFZ_ENABLE_HTML=0 -DFZ_ENABLE_EPUB=0 -DFZ_ENABLE_FB2=0 \
	-DFZ_ENABLE_MD=0 -DFZ_ENABLE_MOBI=0 -DFZ_ENABLE_OFFICE=0 -DFZ_ENABLE_TXT=0 \
	-DFZ_ENABLE_CBZ=0 -DFZ_ENABLE_IMG=0 -DFZ_ENABLE_OCR_OUTPUT=0 \
	-DFZ_ENABLE_ODT_OUTPUT=0 -DFZ_ENABLE_DOCX_OUTPUT=0 -DFZ_ENABLE_HYPHEN=0
# MuPDF is C and handles errors with setjmp, so no unwind tables are needed.
CODE_FLAGS := -fno-asynchronous-unwind-tables -fno-unwind-tables

ifeq ($(fonts),stripped)
  SUFFIX := -stripped
  STRIPPED_FONTS := build/stripped-fonts
  # MuPDF embeds fonts with ld where it can (Linux) and as generated C elsewhere. Both are
  # pointed at the stripped copies; the symbol names stay what MuPDF expects.
  MUPDF_SWITCHES += 'OBJCOPY_CMD=$$(QUIET_OBJCOPY) $$(MKTGTDIR) ; cd $(STRIPPED_FONTS) && $$(LD) -r -b binary -z noexecstack -o $$(abspath $$@) $$<'
  MUPDF_SWITCHES += HEXDUMP_SH=pp/hexdump-stripped.sh
endif

ifneq ($(arch),)
  SUFFIX := $(SUFFIX)-$(arch)
  CODE_FLAGS += -arch $(arch)
endif

OUT := build/$(build)$(SUFFIX)
MUPDF_SWITCHES += OUT=$(OUT)

EXE :=
LIBS := -lm -lpthread
ifeq ($(OS),Darwin)
  LINK_FLAGS := -Wl,-dead_strip
else ifeq ($(OS),MINGW)
  # MuPDF assumes MSVC on Windows and enables SSE4.1 code (scan deskewing, unused here)
  # that gcc would only compile with -msse4.1.
  CODE_FLAGS += -DARCH_HAS_SSE=0
  EXE := .exe
  LIBS := -lm
  LINK_FLAGS := -Wl,--gc-sections -static
else
  # A position-dependent executable needs no dynamic relocation table.
  LINK_FLAGS := -Wl,--gc-sections -no-pie
  ifeq ($(static),yes)
    LINK_FLAGS += -static
  endif
endif

TOOL := $(OUT)/pp-mupdf$(EXE)

all: $(TOOL)

# Generated font sources (macOS, Windows) live in generated/, shared by both variants:
# regenerate the fallback fonts whenever the variant changes. (The 14 standard fonts are the
# same in both and checked in upstream.)
GENERATED_FALLBACK := generated/resources/fonts/droid generated/resources/fonts/han \
	generated/resources/fonts/noto generated/resources/fonts/sil
libs: $(STRIPPED_FONTS)
	@mkdir -p build; if [ "$$(cat build/generated-fonts 2>/dev/null)" != "$(fonts)" ]; then \
		rm -rf $(GENERATED_FALLBACK); echo "$(fonts)" > build/generated-fonts; fi
	$(MAKE) $(MUPDF_SWITCHES) XCFLAGS="$(FEATURE_FLAGS) $(CODE_FLAGS)" libs

# brotli-encoder-stub.c comes before the libraries so the Brotli encoder is never linked.
$(TOOL): pp/pp-mupdf.c pp/brotli-encoder-stub.c libs
	$(CC) -O2 $(FEATURE_FLAGS) $(CODE_FLAGS) -DFZ_ENABLE_JS=0 -Iinclude \
		-Ithirdparty/brotli/c/include -o $@ pp/pp-mupdf.c pp/brotli-encoder-stub.c \
		$(OUT)/libmupdf.a $(OUT)/libmupdf-third.a $(LIBS) $(LINK_FLAGS)
	$(STRIP) $@
	@ls -l $@

# MuPDF's own mutool with the same switches, to compare against upstream output.
mutool: $(STRIPPED_FONTS)
	$(MAKE) $(MUPDF_SWITCHES) XCFLAGS="$(FEATURE_FLAGS) $(CODE_FLAGS)" tools

build/stripped-fonts: pp/strip-font-outlines.py
	rm -rf $@ build/*/resources/fonts $(GENERATED_FALLBACK)
	python3 pp/strip-font-outlines.py $@
	touch $@

# End-to-end tests of the tool on generated PDFs; PP_TEST_PDFS=<dir> keeps the PDFs.
test: $(TOOL)
	PP_MUPDF=$(TOOL) python3 -m unittest discover -s pp/tests -v

clean:
	rm -f $(TOOL)

.PHONY: all libs mutool test clean
