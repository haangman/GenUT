#include "rb.h"

unsigned char *rb_alloc_slot(struct rb *rb, size_t len)
{
	if (rb->tail + len > rb->cap)
		return NULL;
	return rb->buf + rb->tail;
}
