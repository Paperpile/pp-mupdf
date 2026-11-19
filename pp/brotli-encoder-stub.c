/* pp-mupdf never writes Brotli-compressed streams, but MuPDF's compression code references
 * the Brotli encoder, which would link about 0.8 MB of encoder tables. Defining the encoder
 * entry points MuPDF calls keeps the linker from pulling the encoder out of
 * libmupdf-third.a. Creating an encoder fails, so MuPDF reports an error if anything ever
 * asks for Brotli output. Decoding Brotli streams is unaffected. */

#include <brotli/encode.h>

BrotliEncoderState *BrotliEncoderCreateInstance(brotli_alloc_func alloc_func,
	brotli_free_func free_func, void *opaque)
{
	(void)alloc_func; (void)free_func; (void)opaque;
	return NULL;
}

void BrotliEncoderDestroyInstance(BrotliEncoderState *state)
{
	(void)state;
}

BROTLI_BOOL BrotliEncoderSetParameter(BrotliEncoderState *state,
	BrotliEncoderParameter param, uint32_t value)
{
	(void)state; (void)param; (void)value;
	return BROTLI_FALSE;
}

BROTLI_BOOL BrotliEncoderCompressStream(BrotliEncoderState *state,
	BrotliEncoderOperation op, size_t *available_in, const uint8_t **next_in,
	size_t *available_out, uint8_t **next_out, size_t *total_out)
{
	(void)state; (void)op; (void)available_in; (void)next_in;
	(void)available_out; (void)next_out; (void)total_out;
	return BROTLI_FALSE;
}

BROTLI_BOOL BrotliEncoderIsFinished(BrotliEncoderState *state)
{
	(void)state;
	return BROTLI_TRUE;
}

size_t BrotliEncoderMaxCompressedSize(size_t input_size)
{
	(void)input_size;
	return 0;
}
