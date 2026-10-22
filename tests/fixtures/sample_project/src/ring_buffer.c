#include <stddef.h>
#include <string.h>
#include "rb.h"

#define RB_MAGIC 0x52420001u

static struct rb_stats stats;

static size_t rb_free_space(const struct rb *rb)
{
	return rb->cap - (rb->tail - rb->head);
}

int rb_init(struct rb *rb, unsigned char *buf, size_t cap)
{
	if (rb == NULL || buf == NULL || cap == 0)
		return RB_EINVAL;
	rb->buf = buf;
	rb->cap = cap;
	rb->head = 0;
	rb->tail = 0;
	rb->magic = RB_MAGIC;
	return RB_OK;
}

int rb_push(struct rb *rb, const void *data, size_t len)
{
	unsigned char *slot;

	if (rb == NULL || data == NULL)
		return RB_EINVAL;
	if (rb_free_space(rb) < len) {
		stats.full_count++;
		return RB_ENOSPC;
	}
	slot = rb_alloc_slot(rb, len);
	if (slot == NULL)
		return RB_ENOMEM;
	memcpy(slot, data, len);
	rb->tail += len;
	stats.push_count++;
	return RB_OK;
}

size_t rb_used(const struct rb *rb)
{
	return rb->tail - rb->head;
}

const char *rb_state_name(int state)
{
	switch (state) {
	case RB_STATE_EMPTY:
		return "empty";
	case RB_STATE_FULL:
		return "full";
	default:
		return "partial";
	}
}

#ifdef RB_DEBUG
void rb_dump(const struct rb *rb)
{
	(void)rb;
}
#endif
