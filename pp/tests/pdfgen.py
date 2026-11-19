"""A minimal PDF writer for building test files by hand, standard library only.

Objects are written as PDF syntax, so each test shows exactly what its file contains:

    pdf = PDF()
    font = pdf.add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    content = pdf.add(b'<< >>', stream=b'BT /F1 12 Tf 100 700 Td (Hi) Tj ET')
    ...
    data = pdf.build(root)

build() can also produce the damaged and modern layouts real files come in: wrong xref
offsets, no xref at all, cross-reference and object streams, incremental updates.
"""

import zlib


def text(s):
    """A PDF text string: PDFDocEncoding-safe ASCII as a literal, anything else UTF-16BE."""
    if all(32 <= ord(c) < 127 for c in s):
        return b'(' + s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)').encode() + b')'
    return b'<FEFF' + s.encode('utf-16-be').hex().upper().encode() + b'>'


def ref(num):
    return b'%d 0 R' % num


class PDF:
    def __init__(self, version=b'1.7'):
        self.version = version
        self.objects = {}

    def reserve(self):
        num = len(self.objects) + 1
        self.objects[num] = None
        return num

    def add(self, body, stream=None):
        num = self.reserve()
        self.set(num, body, stream)
        return num

    def set(self, num, body, stream=None):
        if isinstance(body, str):
            body = body.encode('latin-1')
        if isinstance(stream, str):
            stream = stream.encode('latin-1')
        self.objects[num] = (body, stream)

    def _object(self, num, body, stream):
        if stream is None:
            return b'%d 0 obj\n%s\nendobj\n' % (num, body)
        assert body.rstrip().endswith(b'>>'), 'a stream needs a dictionary'
        body = body.rstrip()[:-2] + b' /Length %d >>' % len(stream)
        return b'%d 0 obj\n%s\nstream\n%s\nendstream\nendobj\n' % (num, body, stream)

    def build(self, root, xref='table', info=None):
        """xref: 'table', 'stream' (objects in an object stream, cross-reference stream),
        'broken' (a table with every offset wrong) or 'none' (no table, no trailer)."""
        out = bytearray(b'%%PDF-%s\n%%\xe2\xe3\xcf\xd3\n' % self.version)
        if xref == 'stream':
            return self._build_compressed(out, root)
        offsets = {}
        for num in sorted(self.objects):
            body, stream = self.objects[num]
            offsets[num] = len(out)
            out += self._object(num, body, stream)
        if xref == 'none':
            return bytes(out)
        start = len(out)
        size = max(self.objects) + 1
        out += b'xref\n0 %d\n0000000000 65535 f \n' % size
        for num in range(1, size):
            offset = offsets[num] + (7 if xref == 'broken' else 0)
            out += b'%010d 00000 n \n' % offset
        out += b'trailer\n<< /Size %d /Root %s' % (size, ref(root))
        if info:
            out += b' /Info ' + ref(info)
        out += b' >>\nstartxref\n%d\n%%%%EOF\n' % start
        return bytes(out)

    def _build_compressed(self, out, root):
        # Every non-stream object goes into one object stream, as PDF 1.5 writers do.
        inline = [n for n in sorted(self.objects) if self.objects[n][1] is None]
        streams = [n for n in sorted(self.objects) if self.objects[n][1] is not None]
        objstm = max(self.objects) + 1
        xref_num = objstm + 1
        header, body = bytearray(), bytearray()
        for i, num in enumerate(inline):
            header += b'%d %d ' % (num, len(body))
            body += self.objects[num][0] + b'\n'
        data = zlib.compress(bytes(header) + bytes(body))
        entries = {}
        for i, num in enumerate(inline):
            entries[num] = (2, objstm, i)
        for num in streams:
            entries[num] = (1, len(out), 0)
            out += self._object(num, *self.objects[num])
        entries[objstm] = (1, len(out), 0)
        out += self._object(objstm, b'<< /Type /ObjStm /N %d /First %d /Filter /FlateDecode >>'
                            % (len(inline), len(header)), data)
        entries[xref_num] = (1, len(out), 0)
        rows = bytearray(bytes([0, 0, 0, 0, 0xff, 0xff]))
        for num in range(1, xref_num + 1):
            kind, a, b = entries[num]
            rows += bytes([kind]) + a.to_bytes(4, 'big') + b.to_bytes(1, 'big')
        dict_ = b'<< /Type /XRef /Size %d /W [1 4 1] /Root %s >>' % (xref_num + 1, ref(root))
        start = len(out)
        out += self._object(xref_num, dict_, bytes(rows))
        out += b'startxref\n%d\n%%%%EOF\n' % start
        return bytes(out)


def append_update(data, objects, root, size):
    """An incremental update: objects {num: body} appended with a table chained by /Prev."""
    prev = int(data.rsplit(b'startxref', 1)[1].split()[0])
    out = bytearray(data)
    offsets = {}
    for num, body in sorted(objects.items()):
        offsets[num] = len(out)
        out += b'%d 0 obj\n%s\nendobj\n' % (num, body)
    start = len(out)
    out += b'xref\n0 1\n0000000000 65535 f \n'
    for num in sorted(offsets):
        out += b'%d 1\n%010d 00000 n \n' % (num, offsets[num])
    out += b'trailer\n<< /Size %d /Root %s /Prev %d >>\nstartxref\n%d\n%%%%EOF\n' % (
        size, ref(root), prev, start)
    return bytes(out)


def simple_document(pages, page_attrs=b'', pages_attrs=b'/MediaBox [0 0 612 792]', fonts=None,
                    catalog=b'', pdf=None):
    """A document whose pages are given as (content, annots_or_None, attrs) tuples, annots
    being the raw /Annots value. Font /F1 is Helvetica (WinAnsi); fonts adds more as
    {b'F2': object number}. Returns (pdf, root, page_numbers). Pass pdf to create objects
    (annotations, fonts) before the pages."""
    pdf = pdf or PDF()
    root = pdf.reserve()
    pages_num = pdf.reserve()
    font = pdf.add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica '
                   b'/Encoding /WinAnsiEncoding >>')
    font_dict = b'/F1 ' + ref(font) + b''.join(
        b' /' + name + b' ' + ref(num) for name, num in (fonts or {}).items())
    page_nums = []
    for content, annots, attrs in pages:
        content_num = pdf.add(b'<< >>', stream=content)
        page = b'<< /Type /Page /Parent %s /Resources << /Font << %s >> >> /Contents %s' % (
            ref(pages_num), font_dict, ref(content_num))
        if annots is not None:
            page += b' /Annots ' + annots
        page += b' ' + (attrs or page_attrs) + b' >>'
        page_nums.append(pdf.add(page))
    kids = b' '.join(ref(n) for n in page_nums)
    pdf.set(pages_num, b'<< /Type /Pages /Kids [%s] /Count %d %s >>' % (
        kids, len(page_nums), pages_attrs))
    pdf.set(root, b'<< /Type /Catalog /Pages %s %s >>' % (ref(pages_num), catalog))
    return pdf, root, page_nums


def text_content(lines, size=12, x=72, y=700, leading=20):
    """Content stream showing each line (a bytes string in WinAnsi) with /F1."""
    out = [b'BT /F1 %d Tf %d %d Td %d TL' % (size, x, y, leading)]
    for i, line in enumerate(lines):
        escaped = line.replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)')
        out.append((b'(%s) Tj' if i == 0 else b'T* (%s) Tj') % escaped)
    out.append(b'ET')
    return b'\n'.join(out)
