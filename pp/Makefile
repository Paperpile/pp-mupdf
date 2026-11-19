# pp-mupdf: MuPDF's libraries built for PDF only, without JavaScript, linked into one tool.
# Run from the repository root:
#
#   make -f pp/Makefile                 # full: every MuPDF font; reads, writes, renders
#   make -f pp/Makefile fonts=stripped  # small: same text, fallback fonts render blank
#
# The full build is for anything that renders pages (thumbnails). The small build keeps every
# font MuPDF can choose with its Unicode map and metrics but without glyph outlines or layout
# tables (pp/strip-font-outlines.py, needs fontTools), so it extracts byte-identical text
# (pp/compare-text.sh) at a fraction of the size; text drawn with a fallback font renders
# blank. The 14 standard PDF fonts keep their outlines in both.
#
# make -f pp/Makefile test [fonts=stripped] runs pp/tests against the build.
#
# MuPDF and its third-party libraries are linked statically; only the system C library is
# dynamic. Upstream files stay untouched; this file only passes switches to MuPDF's Makefile.

build ?= release

MUPDF_SWITCHES := build=$(build) mujs=no html=no xps=no svg=no extract=no tesseract=no barcode=no
FEATURE_FLAGS := -DFZ_ENABLE_CBZ=0 -DFZ_ENABLE_IMG=0 -DFZ_ENABLE_OCR_OUTPUT=0 \
	-DFZ_ENABLE_ODT_OUTPUT=0 -DFZ_ENABLE_DOCX_OUTPUT=0 -DFZ_ENABLE_HYPHEN=0
# MuPDF is C and handles errors with setjmp, so no unwind tables are needed.
CODE_FLAGS := -fno-asynchronous-unwind-tables -fno-unwind-tables

ifeq ($(fonts),stripped)
  SUFFIX := -stripped
  STRIPPED_FONTS := build/stripped-fonts
  MUPDF_SWITCHES += OUT=build/$(build)$(SUFFIX)
  # Same as MuPDF's OBJCOPY_CMD, but reads the font from the stripped tree; running ld from
  # there keeps the symbol names (_binary_resources_fonts_...) MuPDF expects.
  MUPDF_SWITCHES += 'OBJCOPY_CMD=$$(QUIET_OBJCOPY) $$(MKTGTDIR) ; cd $(STRIPPED_FONTS) && $$(LD) -r -b binary -z noexecstack -o $$(abspath $$@) $$<'
endif

OUT := build/$(build)$(SUFFIX)
TOOL := $(OUT)/pp-mupdf

CC ?= cc
ifeq ($(shell uname),Darwin)
  LINK_FLAGS := -Wl,-dead_strip
else
  # A position-dependent executable needs no dynamic relocation table.
  LINK_FLAGS := -Wl,--gc-sections -no-pie
endif

all: $(TOOL)

libs: $(STRIPPED_FONTS)
	$(MAKE) $(MUPDF_SWITCHES) XCFLAGS="$(FEATURE_FLAGS) $(CODE_FLAGS)" libs

# brotli-encoder-stub.c comes before the libraries so the Brotli encoder is never linked.
$(TOOL): pp/pp-mupdf.c pp/brotli-encoder-stub.c libs
	$(CC) -O2 $(FEATURE_FLAGS) $(CODE_FLAGS) -DFZ_ENABLE_JS=0 -Iinclude \
		-Ithirdparty/brotli/c/include -o $@ pp/pp-mupdf.c pp/brotli-encoder-stub.c \
		$(OUT)/libmupdf.a $(OUT)/libmupdf-third.a -lm -lpthread $(LINK_FLAGS)
	strip $@
	@ls -l $@

# MuPDF's own mutool with the same switches, to compare against upstream output.
mutool: $(STRIPPED_FONTS)
	$(MAKE) $(MUPDF_SWITCHES) XCFLAGS="$(FEATURE_FLAGS) $(CODE_FLAGS)" tools

build/stripped-fonts: pp/strip-font-outlines.py
	rm -rf $@ $(OUT)/resources/fonts
	python3 pp/strip-font-outlines.py $@
	touch $@

# End-to-end tests of the tool on generated PDFs; PP_TEST_PDFS=<dir> keeps the PDFs.
test: $(TOOL)
	PP_MUPDF=$(TOOL) python3 -m unittest discover -s pp/tests -v

clean:
	rm -f $(TOOL)

.PHONY: all libs mutool test clean
