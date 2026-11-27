"""End-to-end tests of the pp-mupdf executable on generated PDFs.

    PP_MUPDF=build/release/pp-mupdf python3 -m unittest discover -s pp/tests -v

Every PDF is built in the test itself (pdfgen.py), modelled on problems found in real files
and in earlier PDF engines: inherited page boxes, rotated and cropped pages, damaged cross
references, object streams, incremental updates, annotation values of every type, text
strings that need escaping, non-embedded CJK fonts without ToUnicode, ligatures, and
annotation appearances that must not count as page text. The tests must pass for both the
full and the small (stripped fonts) build.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from pdfgen import PDF, append_update, encrypt, ref, simple_document, text, text_content

TOOL = os.path.abspath(os.environ.get('PP_MUPDF', 'build/release/pp-mupdf'))
# A folder to keep every PDF the tests build and pp-mupdf writes, named after the test.
KEEP = os.environ.get('PP_TEST_PDFS')

HELLO_WORLD = text_content([b'Hello', b'World'])
# Over both lines of HELLO_WORLD (baselines at 700 and 680), so read reports both.
HIGHLIGHT = (b'<< /Type /Annot /Subtype /Highlight /Rect [70 670 200 715] '
             b'/QuadPoints [70 715 200 715 70 670 200 670] /C [1 1 0] >>')


def apply(m, x, y):
    """A JSON matrix [a b c d e f] applied to a point."""
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def read_pam(path):
    with open(path, 'rb') as f:
        data = f.read()
    header, _, pixels = data.partition(b'ENDHDR\n')
    fields = dict(line.split(b' ', 1) for line in header.split(b'\n')[1:] if b' ' in line)
    width, height, depth = int(fields[b'WIDTH']), int(fields[b'HEIGHT']), int(fields[b'DEPTH'])
    assert len(pixels) == width * height * depth
    return width, height, depth, pixels


def pixel(pam, x, y):
    width, _, depth, pixels = pam
    i = (y * width + x) * depth
    return tuple(pixels[i:i + depth])


class Tool(unittest.TestCase):
    """Runs pp-mupdf in a scratch directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='pp-mupdf-test-')

    def tearDown(self):
        if KEEP:
            name = self.id().split('.', 1)[1]
            for f in sorted(os.listdir(self.dir)):
                if f.endswith('.pdf'):
                    os.makedirs(KEEP, exist_ok=True)
                    shutil.copy(os.path.join(self.dir, f), os.path.join(KEEP, '%s.%s' % (name, f)))
        shutil.rmtree(self.dir)

    def path(self, name):
        return os.path.join(self.dir, name)

    def save(self, data, name='in.pdf'):
        with open(self.path(name), 'wb') as f:
            f.write(data)
        return self.path(name)

    def run_tool(self, *args):
        return subprocess.run([TOOL, *args], capture_output=True, timeout=60)

    def ok(self, *args):
        result = self.run_tool(*args)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result

    def fails(self, *args, code=1):
        result = self.run_tool(*args)
        self.assertEqual(result.returncode, code, 'expected exit code %d' % code)
        return result

    def json_of(self, command, data):
        pdf = self.save(data, command + '-in.pdf')
        self.ok(command, pdf, self.path(command + '.json'))
        with open(self.path(command + '.json'), encoding='utf-8') as f:
            return json.load(f)

    def info(self, data):
        return self.json_of('info', data)

    def read(self, data):
        return self.json_of('read', data)

    def write(self, data, plan, ascii=False):
        pdf = self.save(data, 'write-in.pdf')
        with open(self.path('plan.json'), 'w', encoding='utf-8') as f:
            json.dump(plan, f, ensure_ascii=ascii)
        self.ok('write', pdf, self.path('plan.json'), self.path('out.pdf'))
        with open(self.path('out.pdf'), 'rb') as f:
            return f.read()

    def write_fails(self, data, plan):
        pdf = self.save(data, 'write-in.pdf')
        with open(self.path('plan.json'), 'w', encoding='utf-8') as f:
            json.dump(plan, f, ensure_ascii=False)
        result = self.fails('write', pdf, self.path('plan.json'), self.path('out.pdf'))
        self.assertFalse(os.path.exists(self.path('out.pdf')), 'a failed write left a file')
        return result.stderr.decode(errors='replace')

    def assertOpensCleanly(self, data):
        """check succeeds without any warning, so MuPDF did not have to repair the file."""
        result = self.ok('check', self.save(data, 'clean.pdf'))
        self.assertEqual(result.stderr, b'')

    def chars(self, data, page=0):
        return self.read(data)['pages'][page]['chars']


def one_page(content=HELLO_WORLD, annots=None, **kwargs):
    pdf, root, pages = simple_document([(content, annots, None)], **kwargs)
    return pdf, root, pages[0]


# ---- command line -------------------------------------------------------------------------

class CommandLine(Tool):
    def test_version_names_mupdf(self):
        out = self.ok('version').stdout.decode()
        self.assertRegex(out, r'^pp-mupdf \S+ \(MuPDF 1\.28\.\d+\)')

    def test_wrong_arguments_exit_2(self):
        self.fails(code=2)
        self.fails('info', code=2)
        self.fails('nonsense', 'a', 'b', code=2)

    def test_missing_file_exits_1_with_message(self):
        result = self.fails('info', self.path('missing.pdf'), self.path('out.json'))
        self.assertIn(b'pp-mupdf info:', result.stderr)

    def test_not_a_pdf_fails(self):
        self.fails('check', self.save(b'<html>not a pdf</html>\n' * 20))
        self.fails('check', self.save(b''))


# ---- info -----------------------------------------------------------------------------------

class Info(Tool):
    def test_page_boxes_and_rotation_are_inherited(self):
        # MediaBox, CropBox and Rotate on the page tree node only, as many writers do.
        pdf, root, _ = one_page(pages_attrs=b'/MediaBox [0 0 500 800] /CropBox [10 20 410 620] '
                                            b'/Rotate 90')
        page = self.info(pdf.build(root))['pages'][0]
        self.assertEqual(page['mediaBox'], [0, 0, 500, 800])
        self.assertEqual(page['cropBox'], [10, 20, 410, 620])
        self.assertEqual(page['rotate'], 90)
        self.assertEqual(page['bounds'], [0, 0, 600, 400])

    def test_crop_box_with_offset(self):
        pdf, root, _ = one_page(pages_attrs=b'/MediaBox [0 0 612 792] /CropBox [50 60 350 460]')
        page = self.info(pdf.build(root))['pages'][0]
        self.assertEqual(page['bounds'], [0, 0, 300, 400])
        self.assertEqual(apply(page['transform'], 50, 460), (0, 0))
        self.assertEqual(apply(page['transform'], 350, 60), (300, 400))

    def test_transform_for_every_rotation(self):
        # The PDF-space corner that ends up at the top left of the displayed page.
        x0, y0, x1, y1 = 50, 60, 350, 460
        top_left = {0: (x0, y1), 90: (x0, y0), 180: (x1, y0), 270: (x1, y1)}
        for rotate, corner in top_left.items():
            with self.subTest(rotate=rotate):
                pdf, root, _ = one_page(pages_attrs=b'/MediaBox [0 0 612 792] '
                                        b'/CropBox [50 60 350 460] /Rotate %d' % rotate)
                page = self.info(pdf.build(root))['pages'][0]
                x, y = apply(page['transform'], *corner)
                self.assertAlmostEqual(x, 0, places=3)
                self.assertAlmostEqual(y, 0, places=3)
                w, h = (300, 400) if rotate in (0, 180) else (400, 300)
                self.assertEqual(page['bounds'], [0, 0, w, h])

    def test_page_labels(self):
        pdf, root, _ = simple_document(
            [(HELLO_WORLD, None, None)] * 4,
            catalog=b'/PageLabels << /Nums [0 << /S /r >> 2 << /S /D /P (A-) >>] >>')
        info = self.info(pdf.build(root))
        self.assertTrue(info['hasPageLabels'])
        self.assertEqual([p['label'] for p in info['pages']], ['i', 'ii', 'A-1', 'A-2'])

    def test_no_page_labels(self):
        pdf, root, _ = simple_document([(HELLO_WORLD, None, None)] * 2)
        info = self.info(pdf.build(root))
        self.assertEqual(info['numberOfPages'], 2)
        self.assertFalse(info['hasPageLabels'])
        self.assertEqual([p['label'] for p in info['pages']], [None, None])

    def test_page_refs(self):
        pdf, root, pages = simple_document([(HELLO_WORLD, None, None)] * 3)
        info = self.info(pdf.build(root))
        self.assertEqual([p['ref'] for p in info['pages']], ['%d 0 R' % n for n in pages])


# ---- opening damaged and modern files ------------------------------------------------------

class Layouts(Tool):
    """The same document in every file layout must read the same and accept new annotations.
    A plain save of a repaired file once dropped new annotations without an error."""

    LAYOUTS = ['table', 'stream', 'broken', 'none']

    def document(self, layout):
        pdf = PDF()
        note = pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Contents (old) >>')
        pdf, root, _ = one_page(annots=b'[%s]' % ref(note), pdf=pdf)
        return pdf.build(root, xref=layout)

    def test_read_every_layout(self):
        for layout in self.LAYOUTS:
            with self.subTest(layout=layout):
                annots = self.read(self.document(layout))['pages'][0]['annots']
                self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:old'])

    def test_write_every_layout(self):
        plan = {'objects': {'@n': {'/Type': '/Annot', '/Subtype': '/Text',
                                   '/Rect': [40, 40, 60, 60], '/Contents': 'u:new'}},
                'pages': [{'index': 0, 'add': ['@n']}]}
        for layout in self.LAYOUTS:
            with self.subTest(layout=layout):
                out = self.write(self.document(layout), plan)
                self.assertOpensCleanly(out)
                annots = self.read(out)['pages'][0]['annots']
                self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:old', 'u:new'])

    def test_incremental_update_wins(self):
        pdf = PDF()
        note = pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Contents (first) >>')
        pdf, root, _ = one_page(annots=b'[%s]' % ref(note), pdf=pdf)
        data = append_update(pdf.build(root), {
            note: b'<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Contents (second) >>'},
            root, max(pdf.objects) + 1)
        annots = self.read(data)['pages'][0]['annots']
        self.assertEqual(annots[0]['dict']['/Contents'], 'u:second')

    def test_write_when_one_object_is_broken(self):
        # Only the save reaches the broken object (here the /Info dictionary). MuPDF repairs
        # the file then, which dropped the new /Annots without an error.
        pdf = PDF()
        info = pdf.add(b'<< /Title (broken offset) >>')
        pdf, root, _ = one_page(pdf=pdf)
        data = pdf.build(root, info=info, bad_offsets={info})
        self.assertEqual(self.read(data)['pages'][0]['annots'], [])
        out = self.write(data, {'objects': {'@n': text_annot('new')},
                                'pages': [{'index': 0, 'add': ['@n']}]})
        self.assertOpensCleanly(out)
        annots = self.read(out)['pages'][0]['annots']
        self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:new'])

    def test_read_with_a_stream_that_is_no_dictionary(self):
        # A damaged file (no xref, so MuPDF rebuilds it) with an object no page uses whose
        # 'stream' follows a string or nothing at all. Correcting that stream's /Length
        # ('not a dict (string)') or loading it ('syntax error in object') failed the whole
        # open; PDFBox reads such files (an oss-fuzz file of pdfplumber's tests).
        tails = [b'9 0 obj\n(a string)\nstream\nxx\nendstream\nendobj\n',
                 b'9 0 obj\n(a string) stream\nxx\nendstream\nendobj\n',
                 b'9 0 obj\nstream\nxx\nendstream\nendobj\n']
        for tail in tails:
            with self.subTest(tail=tail):
                pdf = PDF()
                note = pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Contents (kept) >>')
                pdf, root, _ = one_page(annots=b'[%s]' % ref(note), pdf=pdf)
                annots = self.read(pdf.build(root, xref='none') + tail)['pages'][0]['annots']
                self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:kept'])

    def test_check_accepts_repairable_file(self):
        pdf, root, _ = one_page()
        self.ok('check', self.save(pdf.build(root, xref='broken')))

    def test_check_no_repair_fails_where_mupdf_repairs(self):
        pdf, root, _ = one_page()
        self.ok('check', self.save(pdf.build(root), 'clean.pdf'), '--no-repair')
        result = self.fails('check', self.save(pdf.build(root, xref='broken')), '--no-repair')
        # The error is the last line, after MuPDF's repair warnings.
        last = result.stderr.strip().split(b'\n')[-1]
        self.assertTrue(last.startswith(b'pp-mupdf check: document is damaged'), result.stderr)

    def test_check_no_repair_finds_a_broken_object_no_page_uses(self):
        pdf = PDF()
        info = pdf.add(b'<< /Title (broken offset) >>')
        pdf, root, _ = one_page(pdf=pdf)
        data = pdf.build(root, info=info, bad_offsets={info})
        self.ok('check', self.save(data))
        self.fails('check', self.save(data), '--no-repair')

    def test_resources_shared_by_many_forms(self):
        # Loading a page scans its resources for transparency and overprint. Forms that refer
        # to one shared resource dictionary each have a reference of their own, and MuPDF
        # remembered the answer on the reference: the dictionary was scanned once per form
        # (a LibreOffice export: 44,000 forms, 15,000 entries, two minutes per command).
        n = 8000
        pdf = PDF()
        gs = pdf.add(b'<< /CA 0.5 >>')
        shared = pdf.add(b'<< /ExtGState << %s >> >>' % b' '.join(b'/G%d %s' % (i, ref(gs)) for i in range(n)))
        forms = [pdf.add(b'<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources %s >>' % ref(shared),
                         stream=b'') for _ in range(n)]
        xobjects = b'/XObject << %s >>' % b' '.join(b'/X%d %s' % (i, ref(f)) for i, f in enumerate(forms))
        pdf, root, _ = one_page(pdf=pdf, resources=xobjects)
        data = self.save(pdf.build(root))
        started = time.monotonic()
        self.ok('check', data, '--no-repair')
        self.assertLess(time.monotonic() - started, 5)

    def repair(self, data):
        self.ok('repair', self.save(data, 'repair-in.pdf'), self.path('repaired.pdf'))
        with open(self.path('repaired.pdf'), 'rb') as f:
            out = f.read()
        self.ok('check', self.save(out, 'repaired-check.pdf'), '--no-repair')
        return out

    def test_repair_writes_a_flat_page_tree_with_the_inherited_attributes(self):
        # Two pages under a nested node that carries MediaBox and Rotate, in a file with a
        # broken xref.
        pdf = PDF()
        root, top, node = pdf.reserve(), pdf.reserve(), pdf.reserve()
        contents = [pdf.add(b'<< >>', stream=HELLO_WORLD) for _ in range(2)]
        page_nums = [pdf.add(b'<< /Type /Page /Parent %s /Contents %s >>' % (ref(node), ref(c)))
                     for c in contents]
        pdf.set(node, b'<< /Type /Pages /Parent %s /Kids [%s %s] /Count 2 '
                      b'/MediaBox [0 0 400 500] /Rotate 90 >>' % (ref(top), ref(page_nums[0]),
                                                                  ref(page_nums[1])))
        pdf.set(top, b'<< /Type /Pages /Kids [%s] /Count 2 >>' % ref(node))
        pdf.set(root, b'<< /Type /Catalog /Pages %s >>' % ref(top))
        data = pdf.build(root, xref='broken')
        before = self.info(data)['pages']

        out = self.repair(data)
        self.assertEqual(self.info(out)['pages'], before)
        self.assertEqual([(p['mediaBox'], p['rotate']) for p in before], [([0, 0, 400, 500], 90)] * 2)
        self.assertNotIn(b'/Parent %s' % ref(node), out)

    def test_repair_keeps_the_annotations(self):
        pdf = PDF()
        annot = pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Contents (kept) >>')
        pdf, root, _ = one_page(annots=b'[%s]' % ref(annot), pdf=pdf)
        out = self.repair(pdf.build(root, xref='broken'))
        annots = self.read(out)['pages'][0]['annots']
        self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:kept'])

    def test_repair_refuses_a_page_tree_with_non_page_kids(self):
        # What an incremental update that reused page object numbers for other objects
        # leaves behind: MuPDF would count the annotation as a page.
        pdf = PDF()
        root, pages_num = pdf.reserve(), pdf.reserve()
        content = pdf.add(b'<< >>', stream=HELLO_WORLD)
        page = pdf.add(b'<< /Type /Page /Parent %s /Contents %s >>' % (ref(pages_num), ref(content)))
        annot = pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] >>')
        pdf.set(pages_num, b'<< /Type /Pages /Kids [%s %s] /Count 2 /MediaBox [0 0 612 792] >>' % (
            ref(page), ref(annot)))
        pdf.set(root, b'<< /Type /Catalog /Pages %s >>' % ref(pages_num))
        result = self.fails('repair', self.save(pdf.build(root)), self.path('repaired.pdf'))
        self.assertIn(b'page 2 is not a page object', result.stderr)
        self.assertFalse(os.path.exists(self.path('repaired.pdf')))


# ---- read: annotation dictionaries ---------------------------------------------------------

class ReadAnnotations(Tool):
    def test_entries_in_order_direct_indirect_and_missing(self):
        pdf = PDF()
        link = pdf.add(b'<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] '
                       b'/A << /S /URI /URI (https://example.com) >> >>')
        direct = b'<< /Type /Annot /Subtype /Text /Rect [0 0 10 10] /Contents (direct) >>'
        pdf, root, _ = one_page(annots=b'[%s %s 99 0 R null]' % (ref(link), direct), pdf=pdf)
        annots = self.read(pdf.build(root))['pages'][0]['annots']
        self.assertEqual(len(annots), 4)
        self.assertEqual(annots[0]['ref'], '%d 0 R' % link)
        self.assertEqual(annots[0]['dict']['/Subtype'], '/Link')
        self.assertIsNone(annots[1]['ref'])
        self.assertEqual(annots[1]['dict']['/Contents'], 'u:direct')
        self.assertEqual(annots[2], {'ref': '99 0 R', 'dict': None})
        self.assertEqual(annots[3], {'ref': None, 'dict': None})

    def test_link_destination_page_stays_a_reference(self):
        # A link's direct /Dest array names the target page. Inlining that page (once with
        # 98,568 /ExtGState entries) made a read take minutes and hundreds of megabytes.
        pdf = PDF()
        link = pdf.reserve()
        huge = b' '.join(b'/K%d %d' % (i, i) for i in range(60000))
        pdf, root, pages = simple_document([(HELLO_WORLD, b'[%s]' % ref(link), None),
                                            (HELLO_WORLD, None, b'/PieceInfo << %s >>' % huge)],
                                           pdf=pdf)
        pdf.set(link, b'<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] '
                      b'/Dest [%s /XYZ 0 0 0] >>' % ref(pages[1]))
        started = time.monotonic()
        annots = self.read(pdf.build(root))['pages'][0]['annots']
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(annots[0]['dict']['/Dest'], ['%d 0 R' % pages[1], '/XYZ', 0, 0, 0])

    def test_page_with_many_characters(self):
        # A dense page (once 64 seconds) must not take time quadratic in its characters.
        pdf = PDF()
        highlight = pdf.add(b'<< /Type /Annot /Subtype /Highlight /Rect [0 0 612 792] '
                            b'/QuadPoints [0 792 612 792 0 0 612 0] >>')
        lines = [b'x' * 200] * 600
        pdf, root, _ = one_page(content=text_content(lines, size=1, leading=1),
                                annots=b'[%s]' % ref(highlight), pdf=pdf)
        started = time.monotonic()
        chars = self.read(pdf.build(root))['pages'][0]['chars']
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(chars), 120000)

    def test_annots_array_behind_a_reference(self):
        pdf = PDF()
        note = pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [0 0 10 10] >>')
        array = pdf.add(b'[%s]' % ref(note))
        pdf, root, _ = one_page(annots=ref(array), pdf=pdf)
        annots = self.read(pdf.build(root))['pages'][0]['annots']
        self.assertEqual([a['ref'] for a in annots], ['%d 0 R' % note])

    def test_value_types(self):
        pdf = PDF()
        color = pdf.add(b'[1 0.5 0]')
        popup = pdf.reserve()
        ap = pdf.add(b'<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] >>', stream=b'')
        annot = pdf.reserve()
        pdf.set(popup, b'<< /Type /Annot /Subtype /Popup /Rect [0 0 1 1] /Parent %s >>' % ref(annot))
        pdf, root, page_ref = one_page(annots=b'[%s]' % ref(annot), pdf=pdf)
        pdf.set(annot,
            b'<< /Type /Annot /Subtype /Highlight /Rect [70 690 200 715] /C %s /CA 0.5 /F 4 '
            b'/Open true /NM (abc) /T %s /P %s /Popup %s /AP << /N %s >> '
            b'/QuadPoints [70 715 200 715 70 690 200 690] /Border [0 0 1] >>'
            % (ref(color), text('Grüße 日本'), ref(page_ref), ref(popup), ref(ap)))
        d = self.read(pdf.build(root))['pages'][0]['annots'][0]['dict']
        self.assertEqual(d['/Subtype'], '/Highlight')
        self.assertEqual(d['/C'], [1, 0.5, 0])          # indirect array resolved
        self.assertEqual(d['/CA'], 0.5)
        self.assertEqual(d['/F'], 4)
        self.assertIs(d['/Open'], True)
        self.assertEqual(d['/NM'], 'u:abc')
        self.assertEqual(d['/T'], 'u:Grüße 日本')
        self.assertEqual(d['/P'], '%d 0 R' % page_ref)   # kept as a reference
        self.assertEqual(d['/Popup'], '%d 0 R' % popup)  # kept as a reference
        self.assertEqual(d['/AP'], {'/N': '%d 0 R' % ap})  # streams stay references
        self.assertEqual(d['/Border'], [0, 0, 1])

    def test_text_strings(self):
        values = ['Unbalanced ) and ( and \\', 'Line 1\nLine 2', 'Grüße', '日本語 😀', '€ and •']
        pdf = PDF()
        nums = [pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [0 0 1 1] /Contents %s >>' % text(v))
                for v in values]
        # The same strings as PDFDocEncoded and escaped literals, as other writers produce them.
        nums.append(pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [0 0 1 1] '
                            b'/Contents (\\(a\\) \\\\ \\240 \\200) >>'))
        pdf, root, _ = one_page(annots=b'[%s]' % b' '.join(ref(n) for n in nums), pdf=pdf)
        annots = self.read(pdf.build(root))['pages'][0]['annots']
        self.assertEqual([a['dict']['/Contents'] for a in annots],
                         ['u:' + v for v in values] + ['u:(a) \\ € •'])

    def test_control_characters_and_astral_text_are_valid_json(self):
        # MuPDF's own JSON writer broke both: U+0014..U+001F went out unescaped (invalid JSON),
        # characters above U+FFFF as "ὠ0" (read back as U+1F60 and "0").
        values = ['unit\x1fseparator \x14', 'emoji 😀 and 𝔸']
        pdf = PDF()
        nums = [pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [0 0 1 1] /Contents %s >>' % text(v))
                for v in values]
        pdf, root, _ = one_page(annots=b'[%s]' % b' '.join(ref(n) for n in nums), pdf=pdf)
        annots = self.read(pdf.build(root))['pages'][0]['annots']
        self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:' + v for v in values])

    def test_reference_cycle_terminates(self):
        pdf = PDF()
        annot = pdf.reserve()
        loop = pdf.reserve()
        pdf.set(loop, b'<< /Back %s /Self %s >>' % (ref(annot), ref(loop)))
        pdf.set(annot, b'<< /Type /Annot /Subtype /Text /Rect [0 0 1 1] /Foo %s >>' % ref(loop))
        pdf, root, _ = one_page(annots=b'[%s]' % ref(annot), pdf=pdf)
        d = self.read(pdf.build(root))['pages'][0]['annots'][0]['dict']
        self.assertEqual(d['/Foo']['/Self'], '%d 0 R' % loop)

    def test_no_chars_option(self):
        # A writer only needs the annotation list; --no-chars skips the text extraction.
        pdf = PDF()
        highlight = pdf.add(HIGHLIGHT)
        pdf, root, _ = one_page(annots=b'[%s]' % ref(highlight), pdf=pdf)
        data = pdf.build(root)
        self.ok('read', self.save(data), self.path('r.json'), '--no-chars')
        with open(self.path('r.json'), encoding='utf-8') as f:
            page = json.load(f)['pages'][0]
        self.assertEqual(len(page['annots']), 1)
        self.assertNotIn('chars', page)
        self.assertIn('chars', self.read(data)['pages'][0])

    def test_page_without_text_markup_has_no_chars(self):
        pdf = PDF()
        note = pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [0 0 10 10] >>')
        pdf, root, page = one_page(annots=b'[%s]' % ref(note), pdf=pdf)
        p = self.read(pdf.build(root))['pages'][0]
        self.assertEqual(p['ref'], '%d 0 R' % page)
        self.assertNotIn('chars', p)


# ---- read: characters under text markups ---------------------------------------------------

class ReadChars(Tool):
    def doc(self, content=HELLO_WORLD, annots_extra=b'', **kwargs):
        pdf = kwargs.pop('pdf', None) or PDF()
        highlight = pdf.add(HIGHLIGHT)
        pdf, root, _ = one_page(content, annots=b'[%s %s]' % (ref(highlight), annots_extra),
                                pdf=pdf, **kwargs)
        return pdf.build(root)

    def text_of(self, chars):
        return ''.join(chr(c[0]) for c in chars)

    def test_codepoints_origins_and_lines(self):
        chars = self.chars(self.doc())
        self.assertEqual(self.text_of(chars), 'HelloWorld')
        self.assertEqual([c[3] for c in chars], [1] * 5 + [2] * 5)
        # Origins in page space (y down): first line at y = 792 - 700, the next 20 below.
        self.assertEqual(chars[0][1:3], [72, 92])
        self.assertEqual(chars[5][1:3], [72, 112])
        self.assertTrue(all(a[1] < b[1] for a, b in zip(chars[:4], chars[1:5])))

    def test_origins_follow_page_rotation(self):
        data = self.doc(pages_attrs=b'/MediaBox [0 0 612 792] /Rotate 90')
        page = self.read(data)['pages'][0]
        self.assertEqual(self.text_of(page['chars']), 'HelloWorld')
        expected = apply(page['transform'], 72, 700)
        self.assertAlmostEqual(page['chars'][0][1], expected[0], places=2)
        self.assertAlmostEqual(page['chars'][0][2], expected[1], places=2)

    def test_annotation_appearances_are_not_page_text(self):
        # A FreeText note lying under a highlight must not end up in the highlight's text.
        pdf = PDF()
        font = pdf.add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
        ap = pdf.add(b'<< /Type /XObject /Subtype /Form /BBox [70 690 200 715] '
                     b'/Resources << /Font << /Helv %s >> >> >>' % ref(font),
                     stream=b'BT /Helv 10 Tf 75 695 Td (NOTE) Tj ET')
        free_text = pdf.add(b'<< /Type /Annot /Subtype /FreeText /Rect [70 690 200 715] '
                            b'/Contents (NOTE) /DA (/Helv 10 Tf 0 g) /AP << /N %s >> >>' % ref(ap))
        chars = self.chars(self.doc(annots_extra=ref(free_text), pdf=pdf))
        self.assertEqual(self.text_of(chars), 'HelloWorld')

    def markup(self, quads, subtype=b'Highlight'):
        return b'<< /Type /Annot /Subtype /%s /Rect [0 0 612 792] /QuadPoints %s >>' % (subtype, quads)

    def chars_under(self, *markups, content=text_content([b'One', b'Two', b'Three', b'Four'])):
        pdf = PDF()
        refs = b' '.join(ref(pdf.add(m)) for m in markups)
        pdf, root, _ = one_page(content, annots=b'[%s]' % refs, pdf=pdf)
        return self.chars(pdf.build(root))

    def test_only_lines_under_a_quad(self):
        # The markup text comes from characters inside a quad and their neighbours on the
        # same line, so read leaves out every other line; the line numbers stay the page's.
        # Baselines: One 700, Two 680, Three 660, Four 640.
        chars = self.chars_under(self.markup(b'[78 688 84 688 78 676 84 676]'))
        self.assertEqual(self.text_of(chars), 'Two')
        self.assertEqual([c[3] for c in chars], [2, 2, 2])

    def test_whole_line_when_a_quad_covers_part_of_it(self):
        chars = self.chars_under(self.markup(b'[70 668 80 668 70 656 80 656]'))
        self.assertEqual(self.text_of(chars), 'Three')

    def test_quads_of_every_markup_and_quad(self):
        chars = self.chars_under(
            self.markup(b'[70 708 80 708 70 696 80 696 70 648 80 648 70 636 80 636]'),
            self.markup(b'[70 668 80 668 70 656 80 656]', b'Squiggly'),
            self.markup(b'[70 688 80 688 70 676 80 676]', b'Link'))
        self.assertEqual(self.text_of(chars), 'OneThreeFour')

    def test_quads_follow_page_rotation(self):
        pdf = PDF()
        highlight = pdf.add(self.markup(b'[70 688 80 688 70 676 80 676]'))
        pdf, root, _ = one_page(text_content([b'One', b'Two', b'Three']), annots=b'[%s]' % ref(highlight),
                                pdf=pdf, pages_attrs=b'/MediaBox [0 0 612 792] /Rotate 90')
        self.assertEqual(self.text_of(self.chars(pdf.build(root))), 'Two')

    def test_no_usable_quads_no_chars(self):
        self.assertEqual(self.chars_under(self.markup(b'/None'), self.markup(b'[70 688 80]')), [])

    def test_unusual_quad_values_keep_every_line(self):
        # The markup-text code reads a reference or a non-number in /QuadPoints its own way
        # (as 0, or not at all), so such quads keep the whole page.
        for quads in (b'[70 688 80 688 70 676 80 /X]', b'[70 688 80 688 70 676 80 %s]'):
            with self.subTest(quads=quads):
                pdf = PDF()
                if b'%s' in quads:
                    quads = quads % ref(pdf.add(b'676'))
                highlight = pdf.add(self.markup(quads))
                pdf, root, _ = one_page(text_content([b'One', b'Two']), annots=b'[%s]' % ref(highlight), pdf=pdf)
                self.assertEqual(self.text_of(self.chars(pdf.build(root))), 'OneTwo')
        pdf = PDF()
        quads = pdf.add(b'[70 688 80 688 70 676 80 676]')
        highlight = pdf.add(self.markup(ref(quads)))
        pdf, root, _ = one_page(text_content([b'One', b'Two']), annots=b'[%s]' % ref(highlight), pdf=pdf)
        self.assertEqual(self.text_of(self.chars(pdf.build(root))), 'OneTwo')

    def test_ligature_glyph_becomes_two_characters(self):
        # The "fi" glyph (via /Differences) comes back as f + i, never as U+FB01 or a gap.
        pdf = PDF()
        font = pdf.add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding '
                       b'<< /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [1 /fi] >> >>')
        content = b'BT /F2 12 Tf 72 700 Td (\\001nd) Tj ET'
        chars = self.chars(self.doc(content, pdf=pdf, fonts={b'F2': font}))
        self.assertEqual(self.text_of(chars), 'find')
        # MuPDF puts f at the ligature's origin and i where the next character starts.
        self.assertEqual([c[1] for c in chars[:3]], sorted(c[1] for c in chars[:3]))

    def test_soft_hyphen_is_reported(self):
        # A ToUnicode map that says U+00AD: it comes through as a character, and dropping it
        # is the caller's job. (Through WinAnsiEncoding alone, 0xAD is a plain hyphen.)
        pdf = PDF()
        cmap = pdf.add(b'<< >>', stream=(
            b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap '
            b'/CMapName /Test def 1 begincodespacerange <00> <FF> endcodespacerange '
            b'1 beginbfchar <AD> <00AD> endbfchar endcmap '
            b'CMapName currentdict /CMap defineresource pop end end'))
        font = pdf.add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica '
                       b'/Encoding /WinAnsiEncoding /ToUnicode %s >>' % ref(cmap))
        content = b'BT /F2 12 Tf 72 700 Td (TBS-\xadT) Tj ET'
        chars = self.chars(self.doc(content, pdf=pdf, fonts={b'F2': font}))
        self.assertEqual(self.text_of(chars), 'TBS-­T')

    def test_text_in_every_colour_space(self):
        # read runs without colour management (only render needs it): text in calibrated,
        # CMYK and spot colours is extracted all the same.
        pdf = PDF()
        tint = pdf.add(b'<< /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [0 1 0 0] /N 1 >>')
        content = (b'BT /F1 12 Tf 72 700 Td 0 0 0 1 k (Ab) Tj /CS1 cs 1 scn (Cd) Tj '
                   b'/CS2 cs 0.5 0.5 0.5 sc (Ef) Tj ET')
        resources = (b'/ColorSpace << /CS1 [/Separation /Spot /DeviceCMYK %s] '
                     b'/CS2 [/CalRGB << /WhitePoint [0.9505 1 1.089] >>] >>' % ref(tint))
        chars = self.chars(self.doc(content, pdf=pdf, resources=resources))
        self.assertEqual(self.text_of(chars), 'AbCdEf')

    def test_non_embedded_cjk_font_without_tounicode(self):
        # Ryumin-Light, Identity-H, no ToUnicode, not embedded: the Unicode values come from
        # MuPDF's Adobe-Japan1 table, which needs the CJK fallback font to be present (also in
        # the small build, without outlines). Without it every character came back as U+FFFD.
        pdf = PDF()
        descriptor = pdf.add(b'<< /Type /FontDescriptor /FontName /Ryumin-Light /Flags 6 '
                             b'/FontBBox [-170 -331 1024 903] /ItalicAngle 0 /Ascent 752 '
                             b'/Descent -271 /CapHeight 742 /StemV 54 >>')
        cid_font = pdf.add(b'<< /Type /Font /Subtype /CIDFontType0 /BaseFont /Ryumin-Light '
                           b'/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> '
                           b'/FontDescriptor %s /DW 1000 >>' % ref(descriptor))
        font = pdf.add(b'<< /Type /Font /Subtype /Type0 /BaseFont /Ryumin-Light '
                       b'/Encoding /Identity-H /DescendantFonts [%s] >>' % ref(cid_font))
        # CIDs 3284 3722 1952 710: 日 本 語 ℃ in Adobe-Japan1.
        content = b'BT /F2 12 Tf 72 700 Td <0CD40E8A07A002C6> Tj ET'
        chars = self.chars(self.doc(content, pdf=pdf, fonts={b'F2': font}))
        self.assertEqual(self.text_of(chars), '日本語℃')
        self.assertEqual([c[1] for c in chars], [72, 84, 96, 108])  # /DW 1000 at 12 pt


# ---- write ---------------------------------------------------------------------------------

def text_annot(contents, rect=(10, 10, 30, 30)):
    return {'/Type': '/Annot', '/Subtype': '/Text', '/Rect': list(rect), '/Contents': 'u:' + contents}


class Write(Tool):
    def three_annotations(self, annots_as_ref=False):
        """A page with a link, an earlier highlight and a note, as a page looks before a save."""
        pdf = PDF()
        link = pdf.add(b'<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] >>')
        old = pdf.add(HIGHLIGHT)
        note = pdf.add(b'<< /Type /Annot /Subtype /Text /Rect [0 0 10 10] /Contents (note) >>')
        array = b'[%s %s %s]' % (ref(link), ref(old), ref(note))
        if annots_as_ref:
            array = ref(pdf.add(array))
        pdf, root, page = one_page(annots=array, pdf=pdf)
        return pdf.build(root), page

    def subtypes(self, data):
        return [a['dict']['/Subtype'] for a in self.read(data)['pages'][0]['annots']]

    def test_keep_and_add(self):
        for annots_as_ref in (False, True):
            with self.subTest(annots_as_ref=annots_as_ref):
                data, _ = self.three_annotations(annots_as_ref)
                out = self.write(data, {'objects': {'@n': text_annot('new')},
                                        'pages': [{'index': 0, 'keep': [0, 2], 'add': ['@n']}]})
                self.assertEqual(self.subtypes(out), ['/Link', '/Text', '/Text'])
                self.assertOpensCleanly(out)

    def test_keep_absent_keeps_everything(self):
        data, _ = self.three_annotations()
        out = self.write(data, {'objects': {'@n': text_annot('new')},
                                'pages': [{'index': 0, 'add': ['@n']}]})
        self.assertEqual(self.subtypes(out), ['/Link', '/Highlight', '/Text', '/Text'])

    def test_header_keeps_the_version_of_the_original(self):
        # A catalog /Version only raises the version; a header without a "<digit>.<digit>"
        # version is written as 1.4, as PDFBox writes it, not as %PDF-0.0 or %PDF-1.0.
        cases = [(b'1.7', b'/Version /1.4', b'%PDF-1.7\n'), (b'1.4', b'/Version /1.6', b'%PDF-1.6\n'),
                 (b'1.3', b'', b'%PDF-1.3\n'), (b'', b'', b'%PDF-1.4\n'), (b'1', b'', b'%PDF-1.4\n')]
        for version, catalog, header in cases:
            with self.subTest(version=version, catalog=catalog):
                pdf, root, _ = one_page(pdf=PDF(version=version), catalog=catalog)
                out = self.write(pdf.build(root), {'objects': {}, 'pages': []})
                self.assertEqual(out[:len(header)], header)

    def test_empty_keep_removes_everything(self):
        data, _ = self.three_annotations()
        out = self.write(data, {'pages': [{'index': 0, 'keep': []}]})
        self.assertEqual(self.subtypes(out), [])

    def test_repeated_saves_replace_earlier_annotations(self):
        # Each save drops the previous save's annotations by leaving them out of "keep".
        data, _ = self.three_annotations()
        first = self.write(data, {'objects': {'@a': text_annot('first')},
                                  'pages': [{'index': 0, 'keep': [0], 'add': ['@a']}]})
        second = self.write(first, {'objects': {'@b': text_annot('second')},
                                    'pages': [{'index': 0, 'keep': [0], 'add': ['@b']}]})
        annots = self.read(second)['pages'][0]['annots']
        self.assertEqual([a['dict'].get('/Contents') for a in annots], [None, 'u:second'])

    def test_value_types_round_trip(self):
        values = {'/Int': 3, '/Neg': -12.5, '/Real': 0.25, '/Bool': True, '/False': False,
                  '/Name': '/Highlight', '/Text': 'u:text', '/Arr': [1, '/N', 'u:s', [2, 3]],
                  '/Dict': {'/K': '/V', '/Nested': {'/X': 1}}}
        annot = dict(text_annot('values'), **values)
        out = self.write(self.three_annotations()[0],
                         {'objects': {'@n': annot}, 'pages': [{'index': 0, 'keep': [], 'add': ['@n']}]})
        d = self.read(out)['pages'][0]['annots'][0]['dict']
        for key, value in values.items():
            self.assertEqual(d[key], value, key)

    def test_names_are_written_as_names_and_text_as_strings(self):
        # An earlier engine wrote /Subtype (Highlight) as a string; readers then skip the note.
        out = self.write(self.three_annotations()[0], {
            'objects': {'@n': {'/Type': '/Annot', '/Subtype': '/Highlight', '/Rect': [0, 0, 1, 1],
                               '/T': 'u:Highlight'}},
            'pages': [{'index': 0, 'keep': [], 'add': ['@n']}]})
        self.assertRegex(out, rb'/Subtype\s*/Highlight')
        self.assertRegex(out, rb'/T\s*\(Highlight\)')

    def test_text_strings_round_trip(self):
        values = ['Unbalanced ) and ( and \\ backslash', 'Line 1\nLine 2\r\nLine 3',
                  'Grüße', '日本語 😀', '€ and •', '']
        objects = {'@n%d' % i: text_annot(v) for i, v in enumerate(values)}
        out = self.write(self.three_annotations()[0],
                         {'objects': objects, 'pages': [{'index': 0, 'keep': [], 'add': list(objects)}]})
        annots = self.read(out)['pages'][0]['annots']
        self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:' + v for v in values])

    def test_plan_with_escaped_characters(self):
        # The same text whether the plan has raw UTF-8 or \u escapes, surrogate pairs included.
        values = ['Grüße', '日本語 😀 𝔸', 'tab\tand\x1f and \x7f']
        objects = {'@n%d' % i: text_annot(v) for i, v in enumerate(values)}
        out = self.write(self.three_annotations()[0],
                         {'objects': objects, 'pages': [{'index': 0, 'keep': [], 'add': list(objects)}]},
                         ascii=True)
        annots = self.read(out)['pages'][0]['annots']
        self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:' + v for v in values])

    def test_double_quotes_survive(self):
        # MuPDF's JSON parser dropped the \" escape, and every JSON writer escapes quotes.
        values = ['say "hi"', '"', 'a \\"b\\" c']
        for ascii in (False, True):
            with self.subTest(ascii=ascii):
                objects = {'@n%d' % i: text_annot(v) for i, v in enumerate(values)}
                out = self.write(self.three_annotations()[0],
                                 {'objects': objects,
                                  'pages': [{'index': 0, 'keep': [], 'add': list(objects)}]},
                                 ascii=ascii)
                annots = self.read(out)['pages'][0]['annots']
                self.assertEqual([a['dict']['/Contents'] for a in annots], ['u:' + v for v in values])

    def test_same_input_same_bytes(self):
        # The document ID is kept, so a write is reproducible (with and without an /ID).
        plan = {'objects': {'@n': text_annot('note')}, 'pages': [{'index': 0, 'add': ['@n']}]}
        pdf, root, _ = one_page()
        for trailer in (b'', b'/ID [<00112233445566778899aabbccddeeff> <00112233445566778899aabbccddeeff>]'):
            with self.subTest(trailer=trailer):
                data = pdf.build(root, trailer=trailer)
                first = self.write(data, plan)
                self.assertEqual(self.write(data, plan), first)
                if trailer:
                    self.assertIn(b'<00112233445566778899AABBCCDDEEFF>', first.upper())

    def test_encrypted_input_is_written_decrypted(self):
        # Older PDFs are often encrypted with an empty user password. They read like any
        # other, and the written file is not encrypted, as with PDFBox's writer.
        pdf = PDF()
        highlight = pdf.add(HIGHLIGHT)
        pdf, root, _ = one_page(annots=b'[%s]' % ref(highlight), pdf=pdf)
        trailer = encrypt(pdf)
        data = pdf.build(root, trailer=trailer)
        self.assertIn(b'/Encrypt', data)
        self.assertEqual(''.join(chr(c[0]) for c in self.chars(data)), 'HelloWorld')
        out = self.write(data, {'objects': {'@n': text_annot('new')},
                                'pages': [{'index': 0, 'add': ['@n']}]})
        self.assertNotIn(b'/Encrypt', out)
        self.assertOpensCleanly(out)
        page = self.read(out)['pages'][0]
        self.assertEqual([a['dict']['/Subtype'] for a in page['annots']], ['/Highlight', '/Text'])
        self.assertEqual(''.join(chr(c[0]) for c in page['chars']), 'HelloWorld')

    def test_binary_string(self):
        out = self.write(self.three_annotations()[0], {
            'objects': {'@n': dict(text_annot('x'), **{'/NM': 'b:48656c6c6f'})},
            'pages': [{'index': 0, 'keep': [], 'add': ['@n']}]})
        self.assertEqual(self.read(out)['pages'][0]['annots'][0]['dict']['/NM'], 'u:Hello')

    def test_references_between_new_and_existing_objects(self):
        # Highlight, popup and reply refer to each other (in a cycle) and to the page.
        data, page = self.three_annotations()
        page_ref = '%d 0 R' % page
        plan = {'objects': {
            '@h': {'/Type': '/Annot', '/Subtype': '/Highlight', '/Rect': [70, 690, 200, 715],
                   '/QuadPoints': [70, 715, 200, 715, 70, 690, 200, 690], '/P': page_ref,
                   '/Popup': '@p', '/NM': 'u:h1'},
            '@p': {'/Type': '/Annot', '/Subtype': '/Popup', '/Rect': [110, 590, 290, 710],
                   '/Parent': '@h', '/F': 28, '/NM': 'u:h1p'},
            '@r': {'/Type': '/Annot', '/Subtype': '/Text', '/Rect': [0, 0, 1, 1], '/IRT': '@h',
                   '/Contents': 'u:reply', '/P': page_ref}},
            'pages': [{'index': 0, 'keep': [], 'add': ['@h', '@p', '@r']}]}
        out = self.write(data, plan)
        page_out = self.read(out)['pages'][0]
        h, p, r = page_out['annots']
        self.assertEqual(h['dict']['/Popup'], p['ref'])
        self.assertEqual(p['dict']['/Parent'], h['ref'])
        self.assertEqual(r['dict']['/IRT'], h['ref'])
        self.assertEqual(h['dict']['/P'], page_out['ref'])

    def test_quad_points_outside_the_page_are_kept(self):
        # Real notes have quads far off the page; writing must not reject or clip them.
        quads = [70, -700, 200, -700, 70, -716, 200, -716]
        out = self.write(self.three_annotations()[0], {
            'objects': {'@h': {'/Type': '/Annot', '/Subtype': '/Highlight',
                               '/Rect': [70, -716, 200, -700], '/QuadPoints': quads}},
            'pages': [{'index': 0, 'keep': [], 'add': ['@h']}]})
        self.assertEqual(self.read(out)['pages'][0]['annots'][0]['dict']['/QuadPoints'], quads)

    def test_existing_numbers_keep_their_precision(self):
        # An earlier engine rewrote 782.64 as 782.64001 on save, moving rendered pages by a pixel.
        pdf, root, _ = one_page(pages_attrs=b'/MediaBox [0 0 595.276 782.64]')
        out = self.write(pdf.build(root), {'objects': {'@n': text_annot('x')},
                                           'pages': [{'index': 0, 'add': ['@n']}]})
        self.assertRegex(out, rb'595\.276\s+782\.64\s*\]')
        self.assertNotIn(b'782.64001', out)

    def test_stream_data_is_latin1(self):
        ap = {'stream': {'dict': {'/Type': '/XObject', '/Subtype': '/Form', '/BBox': [0, 0, 100, 20]},
                         'data': 'BT /F1 12 Tf (Grüße) Tj ET'}}
        annot = dict(text_annot('x'), **{'/AP': {'/N': '@ap'}})
        out = self.write(self.three_annotations()[0], {
            'objects': {'@ap': ap, '@n': annot}, 'pages': [{'index': 0, 'keep': [], 'add': ['@n']}]})
        self.assertIn(b'(Gr\xfc\xdfe) Tj', out)

    def test_page_index_and_other_pages(self):
        pdf, root, _ = simple_document([(HELLO_WORLD, None, None)] * 3)
        out = self.write(pdf.build(root), {'objects': {'@n': text_annot('p2')},
                                           'pages': [{'index': 2, 'add': ['@n']}]})
        pages = self.read(out)['pages']
        self.assertEqual([len(p['annots']) for p in pages], [0, 0, 1])

    def test_long_ink_list(self):
        # Building PDF arrays from the plan must take time linear in their length: an ink
        # list of 80,000 numbers took 4 seconds, 200,000 would take half a minute.
        points = [float(i % 500) for i in range(200000)]
        ink = {'/Type': '/Annot', '/Subtype': '/Ink', '/Rect': [0, 0, 500, 500], '/InkList': [points]}
        pdf, root, _ = one_page()
        started = time.monotonic()
        out = self.write(pdf.build(root), {'objects': {'@i': ink}, 'pages': [{'index': 0, 'add': ['@i']}]})
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.read(out)['pages'][0]['annots'][0]['dict']['/InkList'], [points])

    def test_write_of_a_clean_file_prints_nothing(self):
        pdf, root, _ = one_page()
        with open(self.path('plan.json'), 'w', encoding='utf-8') as f:
            json.dump({'objects': {'@n': text_annot('note')}, 'pages': [{'index': 0, 'add': ['@n']}]}, f)
        result = self.ok('write', self.save(pdf.build(root)), self.path('plan.json'), self.path('out.pdf'))
        self.assertEqual(result.stderr, b'')

    def test_invalid_plans_fail_and_write_nothing(self):
        data, _ = self.three_annotations()
        cases = {
            'stream data above U+00FF': (
                {'objects': {'@s': {'stream': {'dict': {}, 'data': '日本'}}}, 'pages': []},
                'U+65E5'),
            'unknown object id': ({'pages': [{'index': 0, 'add': ['@missing']}]}, '@missing'),
            'object id without @': ({'objects': {'n': {}}, 'pages': []}, 'must start with @'),
            'key without slash': ({'objects': {'@n': {'Type': '/Annot'}}, 'pages': []}, 'must start with /'),
            'page index out of range': ({'pages': [{'index': 3}]}, 'out of range'),
            'keep index out of range': ({'pages': [{'index': 0, 'keep': [5]}]}, 'no /Annots entry 5'),
            'page without index': ({'pages': [{'keep': []}]}, 'without an index'),
            'plain string value': ({'objects': {'@n': {'/T': 'text'}}, 'pages': []}, 'neither'),
        }
        for name, (plan, message) in cases.items():
            with self.subTest(name):
                self.assertIn(message, self.write_fails(data, plan))


# ---- render ----------------------------------------------------------------------------------

class Render(Tool):
    def render(self, data, page=0, *scales):
        pdf = self.save(data, 'render.pdf')
        args = []
        for i, scale in enumerate(scales or (1,)):
            args += [str(scale), self.path('page%d.pam' % i)]
        self.ok('render', pdf, str(page), self.path('render.json'), *args)
        with open(self.path('render.json')) as f:
            info = json.load(f)
        return info, [read_pam(self.path('page%d.pam' % i)) for i in range(len(scales or (1,)))]

    def test_sizes_and_transparent_background(self):
        pdf, root, _ = one_page(text_content([b'XXXX'], size=72, x=72, y=600))
        info, (half, full) = self.render(pdf.build(root), 0, 0.5, 1)
        self.assertEqual(half[:3], (306, 396, 4))
        self.assertEqual(full[:3], (612, 792, 4))
        self.assertEqual(pixel(full, 5, 5)[3], 0, 'background must be transparent')
        # The 14 standard fonts keep their outlines in both builds: the text draws.
        ink = [pixel(full, x, y) for x in range(72, 300) for y in range(140, 192)]
        self.assertGreater(sum(1 for p in ink if p[3] == 255 and p[0] < 64), 500)

    def test_info_matches_info_command(self):
        pdf, root, _ = one_page(pages_attrs=b'/MediaBox [0 0 612 792] /CropBox [50 60 350 460] /Rotate 90')
        data = pdf.build(root)
        info, (pam,) = self.render(data, 0, 1)
        page = self.info(data)['pages'][0]
        self.assertEqual(info['bounds'], page['bounds'])
        self.assertEqual(info['transform'], page['transform'])
        self.assertEqual(pam[:2], (400, 300))

    def test_written_appearance_is_drawn(self):
        # A square with its own appearance stream, written by pp-mupdf, renders in place.
        pdf, root, _ = one_page(b'')
        ap = {'stream': {'dict': {'/Type': '/XObject', '/Subtype': '/Form', '/BBox': [100, 600, 200, 700]},
                         'data': '1 0 0 rg 100 600 100 100 re f'}}
        out = self.write(pdf.build(root), {
            'objects': {'@ap': ap, '@sq': {'/Type': '/Annot', '/Subtype': '/Square', '/F': 4,
                                           '/Rect': [100, 600, 200, 700], '/AP': {'/N': '@ap'}}},
            'pages': [{'index': 0, 'add': ['@sq']}]})
        _, (pam,) = self.render(out, 0, 1)
        self.assertEqual(pixel(pam, 150, 142), (255, 0, 0, 255))
        self.assertEqual(pixel(pam, 50, 142)[3], 0)

    def test_translucent_colours_are_not_premultiplied(self):
        # The PAM holds straight colour, as MuPDF's own writer gives it: a half-transparent
        # (0.6, 0.2, 1) fill reads back as about that colour, not as the colour times its
        # alpha (75, 25, 126). The exact values are MuPDF's rounding.
        pdf, root, _ = one_page(b'/GS1 gs 0.6 0.2 1 rg 100 100 200 200 re f',
                                resources=b'/ExtGState << /GS1 << /ca 0.5 >> >>')
        _, (pam,) = self.render(pdf.build(root), 0, 1)
        self.assertEqual(pixel(pam, 200, 592), (152, 51, 255, 126))
        self.assertEqual(pixel(pam, 50, 50), (0, 0, 0, 0))

    def test_missing_page_fails(self):
        pdf, root, _ = one_page()
        self.fails('render', self.save(pdf.build(root)), '3', self.path('i.json'), '1', self.path('p.pam'))


if __name__ == '__main__':
    unittest.main()
