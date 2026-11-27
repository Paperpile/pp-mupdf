/*
 * pp-mupdf: PDF annotation operations on MuPDF, as a command line tool.
 *
 * The tool knows PDF objects, not annotations: the caller interprets what it reads and
 * describes everything it writes as PDF objects. All structured input and output is JSON
 * in files; the exit code is the only failure signal (MuPDF prints repair warnings to
 * stderr on healthy files).
 *
 *   pp-mupdf version
 *   pp-mupdf info   <in.pdf> <out.json>
 *   pp-mupdf check  <in.pdf>
 *   pp-mupdf read   <in.pdf> <out.json> [--no-chars]
 *   pp-mupdf write  <in.pdf> <plan.json> <out.pdf>
 *   pp-mupdf render <in.pdf> <pageIndex> <info.json> <scale> <out.pam> [<scale> <out.pam> ...]
 *
 * PDF values in JSON follow qpdf's JSON v2 conventions: names "/Name", text strings
 * "u:text", binary strings "b:<hex>", references "12 0 R". In a write plan "@id" refers to
 * an object the plan creates. Numbers are written with the shortest digits that reproduce
 * MuPDF's 32-bit value, so a reader that rounds them to float32 gets the exact value.
 */
#include "mupdf/fitz.h"
#include "mupdf/pdf.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PP_MUPDF_VERSION "0.2.1"
#define MAX_DEPTH 8

static void usage(void)
{
	fprintf(stderr,
		"usage:\n"
		"  pp-mupdf version\n"
		"  pp-mupdf info   <in.pdf> <out.json>\n"
		"  pp-mupdf check  <in.pdf> [--no-repair]\n"
		"  pp-mupdf read   <in.pdf> <out.json> [--no-chars]\n"
		"  pp-mupdf write  <in.pdf> <plan.json> <out.pdf>\n"
		"  pp-mupdf repair <in.pdf> <out.pdf>\n"
		"  pp-mupdf render <in.pdf> <pageIndex> <info.json> <scale> <out.pam> [<scale> <out.pam> ...]\n");
}

/* ---- JSON output helpers ------------------------------------------------- */

static fz_json *json_ref(fz_context *ctx, fz_pool *pool, pdf_obj *ref)
{
	char buf[64];
	fz_snprintf(buf, sizeof buf, "%d %d R", pdf_to_num(ctx, ref), pdf_to_gen(ctx, ref));
	return fz_json_new_string(ctx, pool, buf);
}

/*
 * Appends to a JSON array in constant time. fz_json_array_push walks the whole array for
 * every item, which takes a minute on a page with many characters.
 */
typedef struct
{
	fz_json_array **tail;
} json_list;

static json_list json_list_of(fz_json *array)
{
	json_list list;
	list.tail = &array->u.array;
	while (*list.tail)
		list.tail = &(*list.tail)->next;
	return list;
}

static void json_list_push(fz_context *ctx, fz_pool *pool, json_list *list, fz_json *item)
{
	fz_json_array *entry = fz_pool_alloc(ctx, pool, sizeof *entry);
	entry->value = item;
	entry->next = NULL;
	*list->tail = entry;
	list->tail = &entry->next;
}

static fz_json *json_numbers(fz_context *ctx, fz_pool *pool, const float *v, int n)
{
	fz_json *arr = fz_json_new_array(ctx, pool);
	int i;
	for (i = 0; i < n; i++)
		fz_json_array_push(ctx, pool, arr, fz_json_new_number(ctx, pool, v[i]));
	return arr;
}

static fz_json *json_rect(fz_context *ctx, fz_pool *pool, fz_rect r)
{
	float v[4] = { r.x0, r.y0, r.x1, r.y1 };
	return json_numbers(ctx, pool, v, 4);
}

static fz_json *json_matrix(fz_context *ctx, fz_pool *pool, fz_matrix m)
{
	float v[6] = { m.a, m.b, m.c, m.d, m.e, m.f };
	return json_numbers(ctx, pool, v, 6);
}

/* A JSON string as UTF-8, escaping only what JSON requires. MuPDF's own writer (1.28)
 * escapes characters above U+FFFF as "ὠ0" instead of a surrogate pair, and leaves the
 * control characters U+0014 to U+001F unescaped. */
static void write_json_string(fz_context *ctx, fz_output *out, const char *s)
{
	char utf8[FZ_UTFMAX];
	int c;
	fz_write_byte(ctx, out, '"');
	while (*s)
	{
		s += fz_chartorune(&c, s);
		switch (c)
		{
		case '"': fz_write_string(ctx, out, "\\\""); break;
		case '\\': fz_write_string(ctx, out, "\\\\"); break;
		case '\n': fz_write_string(ctx, out, "\\n"); break;
		case '\r': fz_write_string(ctx, out, "\\r"); break;
		case '\t': fz_write_string(ctx, out, "\\t"); break;
		default:
			if (c < 0x20)
				fz_write_printf(ctx, out, "\\u%04x", c);
			else
				fz_write_data(ctx, out, utf8, fz_runetochar(utf8, c));
		}
	}
	fz_write_byte(ctx, out, '"');
}

/* fz_write_json with the string writer above; numbers, booleans and null as MuPDF writes them. */
static void write_json(fz_context *ctx, fz_output *out, fz_json *value)
{
	fz_json_array *arr;
	fz_json_object *obj;
	switch (value->type)
	{
	case FZ_JSON_STRING:
		write_json_string(ctx, out, value->u.string);
		break;
	case FZ_JSON_ARRAY:
		fz_write_byte(ctx, out, '[');
		for (arr = value->u.array; arr; arr = arr->next)
		{
			if (arr != value->u.array)
				fz_write_byte(ctx, out, ',');
			write_json(ctx, out, arr->value);
		}
		fz_write_byte(ctx, out, ']');
		break;
	case FZ_JSON_OBJECT:
		fz_write_byte(ctx, out, '{');
		for (obj = value->u.object; obj; obj = obj->next)
		{
			if (obj != value->u.object)
				fz_write_byte(ctx, out, ',');
			write_json_string(ctx, out, obj->key);
			fz_write_byte(ctx, out, ':');
			write_json(ctx, out, obj->value);
		}
		fz_write_byte(ctx, out, '}');
		break;
	default:
		fz_write_json(ctx, out, value);
	}
}

static void write_json_file(fz_context *ctx, const char *path, fz_json *json)
{
	fz_output *out = fz_new_output_with_path(ctx, path, 0);
	fz_try(ctx)
	{
		write_json(ctx, out, json);
		fz_close_output(ctx, out);
	}
	fz_always(ctx)
		fz_drop_output(ctx, out);
	fz_catch(ctx)
		fz_rethrow(ctx);
}

/* "/Name", "u:text" or "b:hex" for a name or string. */
static fz_json *json_name(fz_context *ctx, fz_pool *pool, pdf_obj *obj)
{
	const char *name = pdf_to_name(ctx, obj);
	size_t n = strlen(name);
	char *s = fz_pool_alloc(ctx, pool, n + 2);
	s[0] = '/';
	memcpy(s + 1, name, n + 1);
	return fz_json_new_string(ctx, pool, s);
}

static fz_json *json_text(fz_context *ctx, fz_pool *pool, pdf_obj *obj)
{
	const char *text = pdf_to_text_string(ctx, obj);
	size_t n = strlen(text);
	char *s = fz_pool_alloc(ctx, pool, n + 3);
	s[0] = 'u';
	s[1] = ':';
	memcpy(s + 2, text, n + 1);
	return fz_json_new_string(ctx, pool, s);
}

/* Keys whose values stay references: they point at other annotations, the page or
 * appearance streams, which the reader resolves by reference itself. */
static int keep_as_ref(fz_context *ctx, pdf_obj *key)
{
	static const char *keys[] = { "P", "Parent", "Popup", "IRT", "AP", "A", "Dest", "StructParent", NULL };
	const char *k = pdf_to_name(ctx, key);
	int i;
	for (i = 0; keys[i]; i++)
		if (!strcmp(k, keys[i]))
			return 1;
	return 0;
}

static fz_json *obj_to_json(fz_context *ctx, fz_pool *pool, pdf_obj *obj, int depth);

/*
 * The keys of a PDF dictionary are unique, so each member is appended at the end.
 * fz_json_object_set looks through the members before it adds one, which takes minutes on
 * dictionaries with tens of thousands of keys.
 */
static fz_json *dict_to_json(fz_context *ctx, fz_pool *pool, pdf_obj *dict, int depth)
{
	fz_json *out = fz_json_new_object(ctx, pool);
	fz_json_object **tail = &out->u.object;
	int i, n = pdf_dict_len(ctx, dict);
	for (i = 0; i < n; i++)
	{
		pdf_obj *key = pdf_dict_get_key(ctx, dict, i);
		pdf_obj *val = pdf_dict_get_val(ctx, dict, i);
		const char *k = pdf_to_name(ctx, key);
		size_t kn = strlen(k);
		char *jk = fz_pool_alloc(ctx, pool, kn + 2);
		fz_json_object *member = fz_pool_alloc(ctx, pool, sizeof *member);
		jk[0] = '/';
		memcpy(jk + 1, k, kn + 1);
		member->key = jk;
		if (pdf_is_indirect(ctx, val) && keep_as_ref(ctx, key))
			member->value = json_ref(ctx, pool, val);
		else
			member->value = obj_to_json(ctx, pool, val, depth + 1);
		member->next = NULL;
		*tail = member;
		tail = &member->next;
	}
	return out;
}

/* A PDF value as JSON. References are resolved inline (with a depth limit and a cycle
 * guard), except streams, which stay references. */
static fz_json *obj_to_json(fz_context *ctx, fz_pool *pool, pdf_obj *obj, int depth)
{
	fz_json *out = NULL;
	pdf_obj *resolved;

	if (!obj || pdf_is_null(ctx, obj))
		return fz_json_new_null(ctx, pool);

	if (pdf_is_indirect(ctx, obj))
	{
		resolved = pdf_resolve_indirect(ctx, obj);
		if (!resolved || pdf_is_stream(ctx, obj) || depth >= MAX_DEPTH)
			return json_ref(ctx, pool, obj);
		/* A page reached from an annotation (a /Dest array, say) stays a reference: its
		 * resources can be huge, and every link on every page would repeat them. */
		if (pdf_name_eq(ctx, pdf_dict_get(ctx, resolved, PDF_NAME(Type)), PDF_NAME(Page)) ||
			pdf_name_eq(ctx, pdf_dict_get(ctx, resolved, PDF_NAME(Type)), PDF_NAME(Pages)))
			return json_ref(ctx, pool, obj);
		if (pdf_mark_obj(ctx, resolved))
			return json_ref(ctx, pool, obj); /* a cycle */
		fz_try(ctx)
			out = obj_to_json(ctx, pool, resolved, depth);
		fz_always(ctx)
			pdf_unmark_obj(ctx, resolved);
		fz_catch(ctx)
			fz_rethrow(ctx);
		return out;
	}

	if (pdf_is_bool(ctx, obj))
		return fz_json_new_boolean(ctx, pool, pdf_to_bool(ctx, obj));
	if (pdf_is_int(ctx, obj))
		return fz_json_new_number(ctx, pool, (double)pdf_to_int64(ctx, obj));
	if (pdf_is_real(ctx, obj))
		return fz_json_new_number(ctx, pool, pdf_to_real(ctx, obj));
	if (pdf_is_name(ctx, obj))
		return json_name(ctx, pool, obj);
	if (pdf_is_string(ctx, obj))
		return json_text(ctx, pool, obj);
	if (pdf_is_array(ctx, obj))
	{
		int i, n = pdf_array_len(ctx, obj);
		json_list items;
		out = fz_json_new_array(ctx, pool);
		items = json_list_of(out);
		for (i = 0; i < n; i++)
			json_list_push(ctx, pool, &items, obj_to_json(ctx, pool, pdf_array_get(ctx, obj, i), depth + 1));
		return out;
	}
	if (pdf_is_dict(ctx, obj))
		return dict_to_json(ctx, pool, obj, depth);
	return fz_json_new_null(ctx, pool);
}

/* ---- page helpers ---------------------------------------------------------- */

static fz_json *page_box(fz_context *ctx, fz_pool *pool, pdf_obj *page_obj, pdf_obj *key)
{
	pdf_obj *box = pdf_dict_get_inheritable(ctx, page_obj, key);
	float v[4];
	int i;
	if (!pdf_is_array(ctx, box) || pdf_array_len(ctx, box) < 4)
		return fz_json_new_null(ctx, pool);
	for (i = 0; i < 4; i++)
		v[i] = pdf_array_get_real(ctx, box, i);
	return json_numbers(ctx, pool, v, 4);
}

static int is_text_markup(const char *subtype)
{
	return !strcmp(subtype, "Highlight") || !strcmp(subtype, "Underline") ||
		!strcmp(subtype, "StrikeOut") || !strcmp(subtype, "Squiggly");
}

/* The page's structured text from its content stream only: annotation appearances are left
 * out, so the text of a FreeText annotation under a highlight never counts as highlighted. */
static fz_stext_page *page_text(fz_context *ctx, fz_page *page)
{
	fz_stext_options opts = { 0 };
	fz_stext_page *text;
	fz_device *dev = NULL;

	opts.flags = FZ_STEXT_PRESERVE_WHITESPACE;
	text = fz_new_stext_page(ctx, fz_bound_page(ctx, page));
	fz_var(dev);
	fz_try(ctx)
	{
		dev = fz_new_stext_device(ctx, text, &opts);
		fz_run_page_contents(ctx, page, dev, fz_identity, NULL);
		fz_close_device(ctx, dev);
	}
	fz_always(ctx)
		fz_drop_device(ctx, dev);
	fz_catch(ctx)
	{
		fz_drop_stext_page(ctx, text);
		fz_rethrow(ctx);
	}
	return text;
}

/* How far outside a quad a character may start and still be kept. The markup-text code
 * reaches 1pt left of a quad and rounds quad points to one decimal; the rest is slack. */
#define QUAD_PAD 2

/* Where the page's text markups can take characters from: each quad's bounds in page space,
 * padded, as the markup-text code (markupText.ts) builds its regions. Sets *all when a quad
 * is not plain numbers, which that code reads differently; the caller then keeps every line. */
static fz_rect *markup_regions(fz_context *ctx, pdf_obj *annots, fz_matrix ctm, int *n, int *all)
{
	fz_rect *regions = NULL;
	int k, i, j, count = pdf_array_len(ctx, annots), cap = 0;

	*n = 0;
	*all = 0;
	fz_var(regions);
	fz_try(ctx)
	{
		for (k = 0; k < count && !*all; k++)
		{
			pdf_obj *entry = pdf_array_get(ctx, annots, k), *qp;
			int len;
			if (!pdf_is_dict(ctx, entry) || !is_text_markup(pdf_dict_get_name(ctx, entry, PDF_NAME(Subtype))))
				continue;
			qp = pdf_dict_get(ctx, entry, PDF_NAME(QuadPoints));
			if (pdf_is_indirect(ctx, qp))
			{
				*all = 1;
				break;
			}
			if (!pdf_is_array(ctx, qp))
				continue;
			len = pdf_array_len(ctx, qp);
			for (i = 0; i < len; i++)
			{
				pdf_obj *v = pdf_array_get(ctx, qp, i);
				if (pdf_is_indirect(ctx, v) || !pdf_is_number(ctx, v))
					*all = 1;
			}
			for (i = 0; i + 8 <= len && !*all; i += 8)
			{
				fz_rect r = fz_empty_rect;
				for (j = 0; j < 8; j += 2)
				{
					fz_point pt = fz_transform_point_xy(pdf_array_get_real(ctx, qp, i + j), pdf_array_get_real(ctx, qp, i + j + 1), ctm);
					r = fz_include_point_in_rect(r, pt);
				}
				if (*n == cap)
				{
					cap = cap ? cap * 2 : 16;
					regions = fz_realloc_array(ctx, regions, cap, fz_rect);
				}
				regions[(*n)++] = fz_expand_rect(r, QUAD_PAD);
			}
		}
	}
	fz_catch(ctx)
	{
		fz_free(ctx, regions);
		fz_rethrow(ctx);
	}
	return regions;
}

static int line_in_regions(fz_stext_line *line, const fz_rect *regions, int n)
{
	fz_stext_char *ch;
	int i;
	for (ch = line->first_char; ch; ch = ch->next)
		for (i = 0; i < n; i++)
			if (fz_is_point_inside_rect(ch->origin, regions[i]))
				return 1;
	return 0;
}

/* The page's characters in page space: [codepoint, originX, originY, line] each, the
 * line counter increasing per structured-text line, as the markup-text code expects.
 * Only lines with a character in one of the regions are written, unless all is set: the
 * markup-text code takes characters inside a quad and looks at their neighbours on the same
 * line, so other lines cannot change its result. Most of a page is such lines, and writing
 * them made reading and parsing the JSON a large part of a read. */
static fz_json *page_chars(fz_context *ctx, fz_pool *pool, fz_page *page, const fz_rect *regions, int n, int all)
{
	fz_stext_page *text;
	fz_stext_block *block;
	fz_stext_line *line;
	fz_stext_char *ch;
	fz_json *chars = fz_json_new_array(ctx, pool);
	json_list list = json_list_of(chars);
	int line_no = 0;

	text = page_text(ctx, page);
	fz_try(ctx)
	{
		for (block = text->first_block; block; block = block->next)
		{
			if (block->type != FZ_STEXT_BLOCK_TEXT)
				continue;
			for (line = block->u.t.first_line; line; line = line->next)
			{
				line_no++;
				if (!all && !line_in_regions(line, regions, n))
					continue;
				for (ch = line->first_char; ch; ch = ch->next)
				{
					fz_json *c = fz_json_new_array(ctx, pool);
					fz_json_array_push(ctx, pool, c, fz_json_new_number(ctx, pool, ch->c));
					fz_json_array_push(ctx, pool, c, fz_json_new_number(ctx, pool, ch->origin.x));
					fz_json_array_push(ctx, pool, c, fz_json_new_number(ctx, pool, ch->origin.y));
					fz_json_array_push(ctx, pool, c, fz_json_new_number(ctx, pool, line_no));
					json_list_push(ctx, pool, &list, c);
				}
			}
		}
	}
	fz_always(ctx)
		fz_drop_stext_page(ctx, text);
	fz_catch(ctx)
		fz_rethrow(ctx);
	return chars;
}

/* Opens a PDF. One that needs a password to be read is refused: MuPDF opens it without
 * one, and then every string and stream is undecrypted noise, which read would return as
 * notes and write would save, without /Encrypt, as a file nothing can open again. A PDF
 * with only an owner password (an empty user password) opens. */
static pdf_document *open_document(fz_context *ctx, const char *in_path)
{
	pdf_document *doc = pdf_open_document(ctx, in_path);
	fz_try(ctx)
	{
		if (pdf_needs_password(ctx, doc) && !pdf_authenticate_password(ctx, doc, ""))
			fz_throw(ctx, FZ_ERROR_ARGUMENT, "the PDF needs a password");
	}
	fz_catch(ctx)
	{
		pdf_drop_document(ctx, doc);
		fz_rethrow(ctx);
	}
	return doc;
}

/* ---- info ------------------------------------------------------------------ */

static void cmd_info(fz_context *ctx, const char *in_path, const char *out_path)
{
	pdf_document *doc = open_document(ctx, in_path);
	fz_pool *pool = fz_new_pool(ctx);
	fz_try(ctx)
	{
		fz_json *root = fz_json_new_object(ctx, pool);
		fz_json *pages = fz_json_new_array(ctx, pool);
		json_list page_list = json_list_of(pages);
		pdf_obj *labels = pdf_dict_get(ctx, pdf_dict_get(ctx, pdf_trailer(ctx, doc), PDF_NAME(Root)), PDF_NAME(PageLabels));
		int has_labels = !pdf_is_null(ctx, labels);
		int i, n = pdf_count_pages(ctx, doc);

		fz_json_object_set(ctx, pool, root, "numberOfPages", fz_json_new_number(ctx, pool, n));
		fz_json_object_set(ctx, pool, root, "hasPageLabels", fz_json_new_boolean(ctx, pool, has_labels));
		/* From the page objects alone: loading a page also loads its annotations and scans
		 * its resources for transparency, none of which info reports. The bounds are what
		 * fz_bound_page gives, the crop box through the page transform. */
		for (i = 0; i < n; i++)
		{
			pdf_obj *page_obj = pdf_lookup_page_obj(ctx, doc, i);
			fz_json *p = fz_json_new_object(ctx, pool);
			fz_rect cropbox;
			fz_matrix ctm;
			pdf_page_obj_transform(ctx, page_obj, &cropbox, &ctm);
			fz_json_object_set(ctx, pool, p, "ref", json_ref(ctx, pool, page_obj));
			fz_json_object_set(ctx, pool, p, "bounds", json_rect(ctx, pool, fz_transform_rect(cropbox, ctm)));
			fz_json_object_set(ctx, pool, p, "transform", json_matrix(ctx, pool, ctm));
			fz_json_object_set(ctx, pool, p, "mediaBox", page_box(ctx, pool, page_obj, PDF_NAME(MediaBox)));
			fz_json_object_set(ctx, pool, p, "cropBox", page_box(ctx, pool, page_obj, PDF_NAME(CropBox)));
			fz_json_object_set(ctx, pool, p, "rotate", fz_json_new_number(ctx, pool, pdf_dict_get_inheritable_int(ctx, page_obj, PDF_NAME(Rotate))));
			if (has_labels)
			{
				char label[256];
				pdf_page_label(ctx, doc, i, label, sizeof label);
				fz_json_object_set(ctx, pool, p, "label", fz_json_new_string(ctx, pool, label));
			}
			else
				fz_json_object_set(ctx, pool, p, "label", fz_json_new_null(ctx, pool));
			json_list_push(ctx, pool, &page_list, p);
		}
		fz_json_object_set(ctx, pool, root, "pages", pages);
		write_json_file(ctx, out_path, root);
	}
	fz_always(ctx)
	{
		fz_drop_pool(ctx, pool);
		pdf_drop_document(ctx, doc);
	}
	fz_catch(ctx)
		fz_rethrow(ctx);
}

/* ---- check ------------------------------------------------------------------ */

static void load_all_objects(fz_context *ctx, pdf_document *doc);

/* Loads every page and, on pages with text markups, their quads and the page text: the
 * work a later read does, so a document that fails here would fail there.
 *
 * With --no-repair it also fails where MuPDF had to repair the file (a broken xref, objects
 * at wrong offsets) to get there. MuPDF reads such files, but viewers with weaker repair may
 * not: the caller can store the clean copy `repair` writes instead.
 * Every object is loaded first, because a broken object that no page uses is otherwise only
 * found, and repaired, by a later save. */
static void cmd_check(fz_context *ctx, const char *in_path, int no_repair)
{
	pdf_document *doc = open_document(ctx, in_path);
	fz_try(ctx)
	{
		int i, n;
		if (no_repair)
			load_all_objects(ctx, doc);
		n = pdf_count_pages(ctx, doc);
		for (i = 0; i < n; i++)
		{
			pdf_page *page = pdf_load_page(ctx, doc, i);
			fz_stext_page *text = NULL;
			fz_try(ctx)
			{
				pdf_annot *annot;
				for (annot = pdf_first_annot(ctx, page); annot; annot = pdf_next_annot(ctx, annot))
				{
					int k, count;
					if (!is_text_markup(pdf_dict_get_name(ctx, pdf_annot_obj(ctx, annot), PDF_NAME(Subtype))))
						continue;
					if (!text)
						text = page_text(ctx, (fz_page *)page);
					count = pdf_annot_quad_point_count(ctx, annot);
					for (k = 0; k < count; k++)
						(void)pdf_annot_quad_point(ctx, annot, k);
				}
			}
			fz_always(ctx)
			{
				fz_drop_stext_page(ctx, text);
				fz_drop_page(ctx, (fz_page *)page);
			}
			fz_catch(ctx)
				fz_rethrow(ctx);
		}
		if (no_repair && pdf_was_repaired(ctx, doc))
			fz_throw(ctx, FZ_ERROR_FORMAT, "document is damaged: MuPDF had to repair it");
	}
	fz_always(ctx)
		pdf_drop_document(ctx, doc);
	fz_catch(ctx)
		fz_rethrow(ctx);
}

/* ---- read ------------------------------------------------------------------- */

static void cmd_read(fz_context *ctx, const char *in_path, const char *out_path, int with_chars)
{
	pdf_document *doc = open_document(ctx, in_path);
	fz_pool *pool = fz_new_pool(ctx);
	fz_try(ctx)
	{
		fz_json *root = fz_json_new_object(ctx, pool);
		fz_json *pages = fz_json_new_array(ctx, pool);
		json_list page_list = json_list_of(pages);
		int i, n = pdf_count_pages(ctx, doc);
		for (i = 0; i < n; i++)
		{
			pdf_obj *page_obj = pdf_lookup_page_obj(ctx, doc, i);
			pdf_obj *annots = pdf_dict_get(ctx, page_obj, PDF_NAME(Annots));
			fz_json *p = fz_json_new_object(ctx, pool);
			fz_json *list = fz_json_new_array(ctx, pool);
			json_list entries = json_list_of(list);
			int k, count = pdf_array_len(ctx, annots), has_markup = 0;

			for (k = 0; k < count; k++)
			{
				pdf_obj *entry = pdf_array_get(ctx, annots, k);
				fz_json *a = fz_json_new_object(ctx, pool);
				fz_json_object_set(ctx, pool, a, "ref", pdf_is_indirect(ctx, entry) ? json_ref(ctx, pool, entry) : fz_json_new_null(ctx, pool));
				if (pdf_is_dict(ctx, entry))
				{
					fz_json_object_set(ctx, pool, a, "dict", dict_to_json(ctx, pool, pdf_resolve_indirect(ctx, entry), 0));
					if (is_text_markup(pdf_dict_get_name(ctx, entry, PDF_NAME(Subtype))))
						has_markup = 1;
				}
				else
					fz_json_object_set(ctx, pool, a, "dict", fz_json_new_null(ctx, pool));
				json_list_push(ctx, pool, &entries, a);
			}

			fz_json_object_set(ctx, pool, p, "ref", json_ref(ctx, pool, page_obj));
			fz_json_object_set(ctx, pool, p, "annots", list);
			if (has_markup && with_chars)
			{
				pdf_page *page = pdf_load_page(ctx, doc, i);
				fz_rect *regions = NULL;
				fz_var(regions);
				fz_try(ctx)
				{
					fz_rect mediabox;
					fz_matrix ctm;
					int nregions, all;
					pdf_page_transform(ctx, page, &mediabox, &ctm);
					regions = markup_regions(ctx, annots, ctm, &nregions, &all);
					fz_json_object_set(ctx, pool, p, "transform", json_matrix(ctx, pool, ctm));
					fz_json_object_set(ctx, pool, p, "chars", page_chars(ctx, pool, (fz_page *)page, regions, nregions, all));
				}
				fz_always(ctx)
				{
					fz_free(ctx, regions);
					fz_drop_page(ctx, (fz_page *)page);
				}
				fz_catch(ctx)
					fz_rethrow(ctx);
			}
			json_list_push(ctx, pool, &page_list, p);
		}
		fz_json_object_set(ctx, pool, root, "pages", pages);
		write_json_file(ctx, out_path, root);
	}
	fz_always(ctx)
	{
		fz_drop_pool(ctx, pool);
		pdf_drop_document(ctx, doc);
	}
	fz_catch(ctx)
		fz_rethrow(ctx);
}

/* ---- write ------------------------------------------------------------------ */

typedef struct
{
	const char *id;
	int num;
} new_object;

typedef struct
{
	new_object *list;
	int len;
} new_objects;

static int lookup_new(fz_context *ctx, new_objects *objs, const char *id)
{
	int i;
	for (i = 0; i < objs->len; i++)
		if (!strcmp(objs->list[i].id, id))
			return objs->list[i].num;
	fz_throw(ctx, FZ_ERROR_SYNTAX, "plan refers to %s, which it does not create", id);
}

static int hexval(int c)
{
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}

/* A text string that reads back as written. pdf_new_text_string keeps pure ASCII as single
 * bytes, but in PDFDocEncoding bytes 0x18 to 0x1F are accents (0x1F reads back as U+02DC)
 * and 0x7F is undefined, so text with those characters goes out as UTF-16BE instead. */
static pdf_obj *new_text_string(fz_context *ctx, const char *s)
{
	const unsigned char *p;
	unsigned char *buf;
	size_t n = 0, i = 0;
	int c, hi, lo;
	pdf_obj *str = NULL;

	for (p = (const unsigned char *)s; *p; p++)
		if ((*p >= 0x18 && *p <= 0x1F) || *p == 0x7F)
			break;
	if (!*p)
		return pdf_new_text_string(ctx, s);

	for (p = (const unsigned char *)s; *p; n++)
	{
		p += fz_chartorune(&c, (const char *)p);
		if (c >= 0x10000)
			n++;
	}
	buf = fz_malloc(ctx, 2 * n + 2);
	buf[i++] = 0xFE;
	buf[i++] = 0xFF;
	while (*s)
	{
		s += fz_chartorune(&c, s);
		if (c >= 0x10000)
		{
			hi = 0xD800 + ((c - 0x10000) >> 10);
			lo = 0xDC00 + ((c - 0x10000) & 0x3FF);
			buf[i++] = hi >> 8; buf[i++] = hi & 0xFF;
			buf[i++] = lo >> 8; buf[i++] = lo & 0xFF;
		}
		else
		{
			buf[i++] = c >> 8; buf[i++] = c & 0xFF;
		}
	}
	fz_try(ctx)
		str = pdf_new_string(ctx, (const char *)buf, i);
	fz_always(ctx)
		fz_free(ctx, buf);
	fz_catch(ctx)
		fz_rethrow(ctx);
	return str;
}

/* JSON -> PDF value: the inverse of obj_to_json, plus "@id" for created objects. */
static pdf_obj *json_to_obj(fz_context *ctx, pdf_document *doc, new_objects *objs, fz_json *v)
{
	if (fz_json_is_null(ctx, v))
		return PDF_NULL;
	if (fz_json_is_boolean(ctx, v))
		return fz_json_to_boolean(ctx, v) ? PDF_TRUE : PDF_FALSE;
	if (fz_json_is_number(ctx, v))
	{
		double d = fz_json_to_number(ctx, v);
		if (d > -1e15 && d < 1e15 && d == (double)(int64_t)d)
			return pdf_new_int(ctx, (int64_t)d);
		return pdf_new_real(ctx, (float)d);
	}
	if (fz_json_is_string(ctx, v))
	{
		const char *s = fz_json_to_string(ctx, v);
		int num, gen;
		char tail;
		if (s[0] == '/')
			return pdf_new_name(ctx, s + 1);
		if (s[0] == '@')
			return pdf_new_indirect(ctx, doc, lookup_new(ctx, objs, s), 0);
		if (!strncmp(s, "u:", 2))
			return new_text_string(ctx, s + 2);
		if (!strncmp(s, "b:", 2))
		{
			size_t i, n = strlen(s + 2) / 2;
			unsigned char *bytes = fz_malloc(ctx, n + 1);
			pdf_obj *str = NULL;
			for (i = 0; i < n; i++)
			{
				int hi = hexval(s[2 + 2 * i]), lo = hexval(s[3 + 2 * i]);
				if (hi < 0 || lo < 0)
				{
					fz_free(ctx, bytes);
					fz_throw(ctx, FZ_ERROR_SYNTAX, "bad hex string in plan: %s", s);
				}
				bytes[i] = (unsigned char)(hi * 16 + lo);
			}
			fz_try(ctx)
				str = pdf_new_string(ctx, (const char *)bytes, n);
			fz_always(ctx)
				fz_free(ctx, bytes);
			fz_catch(ctx)
				fz_rethrow(ctx);
			return str;
		}
		if (sscanf(s, "%d %d R%c", &num, &gen, &tail) == 2)
			return pdf_new_indirect(ctx, doc, num, gen);
		fz_throw(ctx, FZ_ERROR_SYNTAX, "plan value is neither a name, string nor reference: %s", s);
	}
	if (fz_json_is_array(ctx, v))
	{
		/* Walks the list: fz_json_array_get and _length each walk it from the start,
		 * which made a long /InkList take seconds. */
		fz_json_array *item;
		pdf_obj *arr = pdf_new_array(ctx, doc, 8);
		fz_try(ctx)
			for (item = v->u.array; item; item = item->next)
				pdf_array_push_drop(ctx, arr, json_to_obj(ctx, doc, objs, item->value));
		fz_catch(ctx)
		{
			pdf_drop_obj(ctx, arr);
			fz_rethrow(ctx);
		}
		return arr;
	}
	if (fz_json_is_object(ctx, v))
	{
		fz_json_object *o;
		pdf_obj *dict = pdf_new_dict(ctx, doc, 8);
		fz_try(ctx)
			for (o = v->u.object; o; o = o->next)
			{
				if (o->key[0] != '/')
					fz_throw(ctx, FZ_ERROR_SYNTAX, "dictionary key must start with /: %s", o->key);
				pdf_dict_put_drop(ctx, dict, pdf_new_name(ctx, o->key + 1), json_to_obj(ctx, doc, objs, o->value));
			}
		fz_catch(ctx)
		{
			pdf_drop_obj(ctx, dict);
			fz_rethrow(ctx);
		}
		return dict;
	}
	fz_throw(ctx, FZ_ERROR_SYNTAX, "unexpected JSON value in plan");
}

/* Stream data as Latin-1: each code point below 256 is one byte. The plan's content
 * streams are built for single-byte fonts, so anything else is an error in the plan. */
static fz_buffer *latin1_buffer(fz_context *ctx, const char *utf8)
{
	fz_buffer *buf = fz_new_buffer(ctx, strlen(utf8));
	fz_try(ctx)
	{
		const char *s = utf8;
		while (*s)
		{
			int c;
			s += fz_chartorune(&c, s);
			if (c > 255)
				fz_throw(ctx, FZ_ERROR_SYNTAX, "stream data has a character above U+00FF (U+%04X)", c);
			fz_append_byte(ctx, buf, c);
		}
	}
	fz_catch(ctx)
	{
		fz_drop_buffer(ctx, buf);
		fz_rethrow(ctx);
	}
	return buf;
}

static void save(fz_context *ctx, pdf_document *doc, const char *out_path)
{
	pdf_write_options opts;
	/* Garbage collection on save: a plain save of a document MuPDF repaired on open can
	 * drop newly added annotations without an error. The document ID is kept, so the same
	 * input and plan give the same bytes, and encryption is removed (as PDFBox's writer
	 * does), which also avoids fresh random encryption values on every save. */
	pdf_parse_write_options(ctx, &opts, "garbage,regenerate-id=no,encrypt=none");
	fz_try(ctx)
		pdf_save_document(ctx, doc, out_path, &opts);
	fz_catch(ctx)
	{
		fz_report_error(ctx);
		fz_warn(ctx, "garbage-collecting save failed, saving without");
		pdf_parse_write_options(ctx, &opts, "regenerate-id=no,encrypt=none");
		pdf_save_document(ctx, doc, out_path, &opts);
	}
}

/*
 * Plan:
 *   { "objects": { "@id": <value> | { "stream": { "dict": {...}, "data": "...", "deflate": true } }, ... },
 *     "pages": [ { "index": 3, "keep": [0, 2], "add": ["@a1", "@p1"] }, ... ] }
 * "keep" lists the indices of the page's current /Annots entries that stay (all when
 * absent); "add" appends created objects. A stream's data is stored as given, or, with
 * "deflate": true, compressed with /Filter /FlateDecode set on its dictionary.
 */

/*
 * Loads every object once, so a broken one makes MuPDF repair the document now. A repair
 * rebuilds the xref and drops what was changed in memory: when the save's garbage pass is
 * the first to hit a broken object, the new /Annots are lost without an error.
 */
static void load_all_objects(fz_context *ctx, pdf_document *doc)
{
	int num;
	for (num = 1; num < pdf_xref_len(ctx, doc); num++)
	{
		pdf_obj *obj = NULL;
		fz_var(obj);
		fz_try(ctx)
			obj = pdf_load_object(ctx, doc, num);
		fz_always(ctx)
			pdf_drop_obj(ctx, obj);
		fz_catch(ctx)
			fz_ignore_error(ctx); /* stays broken; the save writes what it can */
	}
}

static void cmd_write(fz_context *ctx, const char *in_path, const char *plan_path, const char *out_path)
{
	pdf_document *doc = open_document(ctx, in_path);
	fz_pool *pool = fz_new_pool(ctx);
	fz_buffer *plan_buf = NULL;
	new_objects objs = { NULL, 0 };
	fz_try(ctx)
	{
		fz_json *plan, *objects, *pages;
		fz_json_object *o;
		fz_json_array *page_item;
		int i, page_count;

		load_all_objects(ctx, doc);
		page_count = pdf_count_pages(ctx, doc);

		plan_buf = fz_read_file(ctx, plan_path);
		plan = fz_parse_json(ctx, pool, fz_string_from_buffer(ctx, plan_buf));
		objects = fz_json_object_get(ctx, plan, "objects");
		pages = fz_json_object_get(ctx, plan, "pages");

		/* Numbers first, so objects can refer to each other in any order. */
		if (objects && fz_json_is_object(ctx, objects))
		{
			for (o = objects->u.object; o; o = o->next)
				objs.len++;
			objs.list = fz_malloc_array(ctx, objs.len, new_object);
			i = 0;
			for (o = objects->u.object; o; o = o->next, i++)
			{
				if (o->key[0] != '@')
					fz_throw(ctx, FZ_ERROR_SYNTAX, "object ids must start with @: %s", o->key);
				objs.list[i].id = o->key;
				objs.list[i].num = pdf_create_object(ctx, doc);
			}
			i = 0;
			for (o = objects->u.object; o; o = o->next, i++)
			{
				fz_json *stream = fz_json_is_object(ctx, o->value) ? fz_json_object_get(ctx, o->value, "stream") : NULL;
				if (stream)
				{
					fz_json *data = fz_json_object_get(ctx, stream, "data");
					pdf_obj *dict = json_to_obj(ctx, doc, &objs, fz_json_object_get(ctx, stream, "dict"));
					pdf_obj *ref = NULL;
					fz_buffer *buf = NULL;
					fz_var(ref);
					fz_var(buf);
					fz_try(ctx)
					{
						fz_json *deflate = fz_json_object_get(ctx, stream, "deflate");
						int compressed = 0;
						buf = latin1_buffer(ctx, data ? fz_json_to_string(ctx, data) : "");
						if (deflate && fz_json_is_boolean(ctx, deflate) && fz_json_to_boolean(ctx, deflate))
						{
							size_t n;
							unsigned char *z = fz_new_deflated_data_from_buffer(ctx, &n, buf, FZ_DEFLATE_DEFAULT);
							fz_drop_buffer(ctx, buf);
							buf = NULL;
							buf = fz_new_buffer_from_data(ctx, z, n);
							pdf_dict_put(ctx, dict, PDF_NAME(Filter), PDF_NAME(FlateDecode));
							compressed = 1;
						}
						pdf_update_object(ctx, doc, objs.list[i].num, dict);
						ref = pdf_new_indirect(ctx, doc, objs.list[i].num, 0);
						pdf_update_stream(ctx, doc, ref, buf, compressed);
					}
					fz_always(ctx)
					{
						fz_drop_buffer(ctx, buf);
						pdf_drop_obj(ctx, ref);
						pdf_drop_obj(ctx, dict);
					}
					fz_catch(ctx)
						fz_rethrow(ctx);
				}
				else
				{
					pdf_obj *value = json_to_obj(ctx, doc, &objs, o->value);
					fz_try(ctx)
						pdf_update_object(ctx, doc, objs.list[i].num, value);
					fz_always(ctx)
						pdf_drop_obj(ctx, value);
					fz_catch(ctx)
						fz_rethrow(ctx);
				}
			}
		}

		if (pages && !fz_json_is_array(ctx, pages))
			fz_throw(ctx, FZ_ERROR_SYNTAX, "plan pages must be an array");
		for (page_item = pages ? pages->u.array : NULL; page_item; page_item = page_item->next)
		{
			fz_json *p = page_item->value;
			fz_json *keep = fz_json_object_get(ctx, p, "keep");
			fz_json *add = fz_json_object_get(ctx, p, "add");
			fz_json *index = fz_json_object_get(ctx, p, "index");
			fz_json_array *item;
			int page_index, k;
			pdf_obj *page_obj, *old, *annots;

			if (!index || !fz_json_is_number(ctx, index))
				fz_throw(ctx, FZ_ERROR_SYNTAX, "plan page without an index");
			page_index = (int)fz_json_to_number(ctx, index);
			if (page_index < 0 || page_index >= page_count)
				fz_throw(ctx, FZ_ERROR_ARGUMENT, "plan page index %d out of range", page_index);
			page_obj = pdf_lookup_page_obj(ctx, doc, page_index);
			old = pdf_dict_get(ctx, page_obj, PDF_NAME(Annots));
			annots = pdf_new_array(ctx, doc, 8);
			fz_try(ctx)
			{
				if (keep && fz_json_is_array(ctx, keep))
				{
					for (item = keep->u.array; item; item = item->next)
					{
						int j = (int)fz_json_to_number(ctx, item->value);
						if (j < 0 || j >= pdf_array_len(ctx, old))
							fz_throw(ctx, FZ_ERROR_ARGUMENT, "page %d has no /Annots entry %d", page_index, j);
						pdf_array_push(ctx, annots, pdf_array_get(ctx, old, j));
					}
				}
				else
				{
					for (k = 0; k < pdf_array_len(ctx, old); k++)
						pdf_array_push(ctx, annots, pdf_array_get(ctx, old, k));
				}
				if (add && fz_json_is_array(ctx, add))
					for (item = add->u.array; item; item = item->next)
						pdf_array_push_drop(ctx, annots, json_to_obj(ctx, doc, &objs, item->value));
				pdf_dict_put(ctx, page_obj, PDF_NAME(Annots), annots);
			}
			fz_always(ctx)
				pdf_drop_obj(ctx, annots);
			fz_catch(ctx)
				fz_rethrow(ctx);
		}

		save(ctx, doc, out_path);
	}
	fz_always(ctx)
	{
		fz_free(ctx, objs.list);
		fz_drop_buffer(ctx, plan_buf);
		fz_drop_pool(ctx, pool);
		pdf_drop_document(ctx, doc);
	}
	fz_catch(ctx)
		fz_rethrow(ctx);
}

/* ---- repair ----------------------------------------------------------------- */

/*
 * Writes a clean copy of a document MuPDF can open but had to repair (check --no-repair
 * fails): a new xref, and a new flat page tree of the pages MuPDF finds, in order, each with
 * the attributes it inherited copied onto it. A save alone keeps a broken page tree as it is,
 * non-page objects among the kids included, and weaker readers then lose pages.
 */
static void cmd_repair(fz_context *ctx, const char *in_path, const char *out_path)
{
	pdf_document *doc = open_document(ctx, in_path);
	pdf_obj *pages = NULL, *kids = NULL;
	fz_var(pages);
	fz_var(kids);
	fz_try(ctx)
	{
		int i, n;
		load_all_objects(ctx, doc);
		n = pdf_count_pages(ctx, doc);
		if (n == 0)
			fz_throw(ctx, FZ_ERROR_FORMAT, "document has no pages");

		pages = pdf_add_new_dict(ctx, doc, 3);
		kids = pdf_new_array(ctx, doc, n);
		for (i = 0; i < n; i++)
		{
			pdf_obj *page = pdf_lookup_page_obj(ctx, doc, i);
			/* MuPDF takes any leaf of the tree for a page. When an update reused page
			 * object numbers for other objects, these are annotations or streams, and
			 * no rebuilt tree gets the pages back: fail and let the caller decide. */
			if (!pdf_is_indirect(ctx, page) || !pdf_is_dict(ctx, page) ||
				!pdf_name_eq(ctx, pdf_dict_get(ctx, page, PDF_NAME(Type)), PDF_NAME(Page)))
				fz_throw(ctx, FZ_ERROR_FORMAT, "page %d is not a page object", i + 1);
			pdf_flatten_inheritable_page_items(ctx, page);
			pdf_array_push(ctx, kids, page);
		}
		/* After the lookups: they may still walk the old tree. */
		for (i = 0; i < n; i++)
			pdf_dict_put(ctx, pdf_array_get(ctx, kids, i), PDF_NAME(Parent), pages);
		pdf_dict_put(ctx, pages, PDF_NAME(Type), PDF_NAME(Pages));
		pdf_dict_put(ctx, pages, PDF_NAME(Kids), kids);
		pdf_dict_put_int(ctx, pages, PDF_NAME(Count), n);
		pdf_dict_put(ctx, pdf_dict_get(ctx, pdf_trailer(ctx, doc), PDF_NAME(Root)), PDF_NAME(Pages), pages);

		save(ctx, doc, out_path);
	}
	fz_always(ctx)
	{
		pdf_drop_obj(ctx, kids);
		pdf_drop_obj(ctx, pages);
		pdf_drop_document(ctx, doc);
	}
	fz_catch(ctx)
		fz_rethrow(ctx);
}

/* ---- render ----------------------------------------------------------------- */

/* fz_save_pixmap_as_pam, byte for byte, for the RGBA pixmaps render makes. MuPDF's writer
 * divides once per pixel to un-premultiply and writes 720 bytes at a time, which took about
 * as long as drawing the page; this looks the factor up, skips opaque pixels (the factor is
 * 1 there) and writes whole rows. It un-premultiplies the samples in place. */
static void save_rgba_pam(fz_context *ctx, fz_pixmap *pix, const char *path)
{
	fz_output *out;
	unsigned char *row;
	int inva[256], x, y, a;

	if (pix->n != 4 || !pix->alpha || pix->s || !fz_colorspace_is_rgb(ctx, pix->colorspace))
	{
		fz_save_pixmap_as_pam(ctx, pix, path);
		return;
	}
	for (a = 0; a < 256; a++)
		inva[a] = a ? 256 * 255 / a : 0;
	for (y = 0, row = pix->samples; y < pix->h; y++, row += pix->stride)
	{
		unsigned char *p = row;
		for (x = 0; x < pix->w; x++, p += 4)
		{
			a = p[3];
			if (a != 255)
			{
				int k = inva[a];
				p[0] = (p[0] * k + 128) >> 8;
				p[1] = (p[1] * k + 128) >> 8;
				p[2] = (p[2] * k + 128) >> 8;
			}
		}
	}

	out = fz_new_output_with_path(ctx, path, 0);
	fz_try(ctx)
	{
		fz_write_printf(ctx, out, "P7\nWIDTH %d\nHEIGHT %d\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n", pix->w, pix->h);
		if (pix->stride == (ptrdiff_t)pix->w * 4)
			fz_write_data(ctx, out, pix->samples, (size_t)pix->h * pix->stride);
		else
			for (y = 0, row = pix->samples; y < pix->h; y++, row += pix->stride)
				fz_write_data(ctx, out, row, (size_t)pix->w * 4);
		fz_close_output(ctx, out);
	}
	fz_always(ctx)
		fz_drop_output(ctx, out);
	fz_catch(ctx)
		fz_rethrow(ctx);
}

/* The whole page at each scale, RGBA with a transparent background, in page space
 * (rotation applied, crop box at the origin, y down). info.json gets the bounds and the
 * transform from PDF space, so the caller can crop annotation thumbnails out of it. */
static void cmd_render(fz_context *ctx, int argc, char **argv)
{
	const char *in_path = argv[0];
	int page_index = atoi(argv[1]);
	const char *info_path = argv[2];
	pdf_document *doc = open_document(ctx, in_path);
	fz_pool *pool = fz_new_pool(ctx);
	pdf_page *page = NULL;
	fz_var(page);
	fz_try(ctx)
	{
		fz_json *info = fz_json_new_object(ctx, pool);
		fz_rect mediabox;
		fz_matrix ctm;
		int i;

		page = pdf_load_page(ctx, doc, page_index);
		for (i = 3; i + 1 < argc; i += 2)
		{
			float scale = (float)atof(argv[i]);
			fz_pixmap *pix = fz_new_pixmap_from_page(ctx, (fz_page *)page, fz_scale(scale, scale), fz_device_rgb(ctx), 1);
			fz_try(ctx)
				save_rgba_pam(ctx, pix, argv[i + 1]);
			fz_always(ctx)
				fz_drop_pixmap(ctx, pix);
			fz_catch(ctx)
				fz_rethrow(ctx);
		}
		pdf_page_transform(ctx, page, &mediabox, &ctm);
		fz_json_object_set(ctx, pool, info, "bounds", json_rect(ctx, pool, fz_bound_page(ctx, (fz_page *)page)));
		fz_json_object_set(ctx, pool, info, "transform", json_matrix(ctx, pool, ctm));
		write_json_file(ctx, info_path, info);
	}
	fz_always(ctx)
	{
		fz_drop_page(ctx, (fz_page *)page);
		fz_drop_pool(ctx, pool);
		pdf_drop_document(ctx, doc);
	}
	fz_catch(ctx)
		fz_rethrow(ctx);
}

/* ---- main --------------------------------------------------------------------- */

int main(int argc, char **argv)
{
	fz_context *ctx;
	int code = 0;

	if (argc < 2)
	{
		usage();
		return 2;
	}
	if (!strcmp(argv[1], "version"))
	{
		printf("pp-mupdf %s (MuPDF %s)\n", PP_MUPDF_VERSION, FZ_VERSION);
		return 0;
	}

	ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
	if (!ctx)
	{
		fprintf(stderr, "pp-mupdf: cannot create MuPDF context\n");
		return 1;
	}

	fz_try(ctx)
	{
		const char *cmd = argv[1];
		/* Only render needs colour management. Setting up lcms on the first colour of a
		 * page took most of a read (about 20 ms per process), and text extraction only
		 * reports codepoints and positions, which colours do not change. */
		if (strcmp(cmd, "render"))
			fz_disable_icc(ctx);
		if (!strcmp(cmd, "info") && argc == 4)
			cmd_info(ctx, argv[2], argv[3]);
		else if (!strcmp(cmd, "check") && argc == 3)
			cmd_check(ctx, argv[2], 0);
		else if (!strcmp(cmd, "check") && argc == 4 && !strcmp(argv[3], "--no-repair"))
			cmd_check(ctx, argv[2], 1);
		else if (!strcmp(cmd, "read") && argc == 4)
			cmd_read(ctx, argv[2], argv[3], 1);
		else if (!strcmp(cmd, "read") && argc == 5 && !strcmp(argv[4], "--no-chars"))
			cmd_read(ctx, argv[2], argv[3], 0);
		else if (!strcmp(cmd, "write") && argc == 5)
			cmd_write(ctx, argv[2], argv[3], argv[4]);
		else if (!strcmp(cmd, "repair") && argc == 4)
			cmd_repair(ctx, argv[2], argv[3]);
		else if (!strcmp(cmd, "render") && argc >= 7 && (argc - 5) % 2 == 0)
			cmd_render(ctx, argc - 2, argv + 2);
		else
		{
			usage();
			code = 2;
		}
	}
	fz_catch(ctx)
	{
		/* MuPDF's pending "... repeated N times" warning first, so the error is the last line. */
		fz_flush_warnings(ctx);
		fprintf(stderr, "pp-mupdf %s: %s\n", argv[1], fz_caught_message(ctx));
		fz_ignore_error(ctx);
		code = 1;
	}

	fz_drop_context(ctx);
	return code;
}
