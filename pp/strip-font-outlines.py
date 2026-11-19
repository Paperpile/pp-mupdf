#!/usr/bin/env python3
"""Copy MuPDF's fallback fonts with every glyph outline removed.

Text extraction only needs what a font says about its glyphs (names, Unicode map, advance
widths, ascender, descender, bounding box), never the outlines or the layout rules. Emptying the outlines keeps
the font files MuPDF chooses and the metrics it reads identical, so extracted text matches
a build with the full fonts while the fonts shrink to a fraction. Pages that fall back to
these fonts render those glyphs blank.

The 14 standard PDF fonts (resources/fonts/urw) are copied unchanged: they draw ordinary
non-embedded Times, Helvetica and Courier text and are small.

    python3 pp/strip-font-outlines.py <out-dir>

writes <out-dir>/resources/fonts/... mirroring the source tree.
"""

import io
import os
import shutil
import types
import sys

from fontTools.cffLib import CFFFontSet
from fontTools.misc.psCharStrings import T2WidthExtractor
from fontTools.ttLib import TTCollection, TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph

SRC = 'resources/fonts'
KEEP_OUTLINES = {'urw'}
# Hinting, plus the OpenType layout tables: they only matter when laying out new text
# (HarfBuzz); a PDF already places every glyph, and MuPDF's text extraction never reads them.
DROP_TABLES = ('fpgm', 'prep', 'cvt ', 'hdmx', 'LTSH', 'VDMX',
               'GSUB', 'GPOS', 'GDEF', 'BASE', 'JSTF', 'MATH')


def empty_charstrings(cff):
    """Replace every charstring with one that only states the glyph's width."""
    for font_name in cff.fontNames:
        top = cff[font_name]
        charstrings = top.CharStrings
        for name in charstrings.keys():
            cs = charstrings[name]
            private = cs.private
            extractor = T2WidthExtractor(
                getattr(private, 'Subrs', []), cff.GlobalSubrs,
                private.nominalWidthX, private.defaultWidthX)
            extractor.execute(cs)
            width = extractor.width
            program = [] if width == private.defaultWidthX else [width - private.nominalWidthX]
            cs.program = program + ['endchar']
            cs.bytecode = None
        # Nothing calls subroutines any more.
        privates = [fd.Private for fd in top.FDArray] if hasattr(top, 'FDArray') else [top.Private]
        for private in privates:
            if hasattr(private, 'Subrs'):
                del private.Subrs
            private.rawDict.pop('Subrs', None)
    cff.GlobalSubrs.items = []


def strip_sfnt(font):
    if 'glyf' in font:
        glyf = font['glyf']
        for name in font.getGlyphOrder():
            glyf[name] = Glyph()
    if 'CFF ' in font:
        empty_charstrings(font['CFF '].cff)
    for tag in DROP_TABLES:
        if tag in font:
            del font[tag]
    if 'cmap' in font:
        # FreeType always picks a 32-bit Unicode map (format 12) over the 16-bit one
        # (format 4), which only repeats its BMP part; the CJK collection has four of each.
        cmap = font['cmap']
        if any(t.format == 12 for t in cmap.tables):
            cmap.tables = [t for t in cmap.tables if t.format != 4]


def strip_file(src, dst):
    ext = os.path.splitext(src)[1]
    if ext == '.ttc':
        collection = TTCollection(src, recalcBBoxes=False, recalcTimestamp=False, lazy=False)
        done = set()
        for font in collection.fonts:
            # Fonts in a collection share tables; strip each shared CFF once.
            cff = font['CFF '] if 'CFF ' in font else None
            if cff is not None and id(cff) in done:
                continue
            strip_sfnt(font)
            if cff is not None:
                done.add(id(cff))
        collection.save(dst, shareTables=True)
    elif ext == '.cff':
        cff = CFFFontSet()
        with open(src, 'rb') as f:
            data = io.BytesIO(f.read())
        cff.decompile(data, None)
        empty_charstrings(cff)
        with open(dst, 'wb') as f:
            cff.compile(f, types.SimpleNamespace(recalcBBoxes=False))
    else:
        font = TTFont(src, recalcBBoxes=False, recalcTimestamp=False)
        strip_sfnt(font)
        font.save(dst)


def main():
    out = sys.argv[1]
    total_in = total_out = 0
    for forge in sorted(os.listdir(SRC)):
        src_dir = os.path.join(SRC, forge)
        if not os.path.isdir(src_dir):
            continue
        dst_dir = os.path.join(out, SRC, forge)
        os.makedirs(dst_dir, exist_ok=True)
        for name in sorted(os.listdir(src_dir)):
            if os.path.splitext(name)[1] not in ('.cff', '.otf', '.ttf', '.ttc'):
                continue
            src = os.path.join(src_dir, name)
            dst = os.path.join(dst_dir, name)
            if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
                pass
            elif forge in KEEP_OUTLINES:
                shutil.copy2(src, dst)
            else:
                strip_file(src, dst + '.tmp')
                os.replace(dst + '.tmp', dst)
            total_in += os.path.getsize(src)
            total_out += os.path.getsize(dst)
    print('fonts: %.1f MB -> %.1f MB' % (total_in / 1e6, total_out / 1e6))


if __name__ == '__main__':
    main()
