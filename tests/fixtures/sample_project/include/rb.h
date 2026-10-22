#ifndef RB_H
#define RB_H

#include <stddef.h>

struct rb {
	unsigned char *buf;
	size_t head;
	size_t tail;
	size_t cap;
	unsigned int magic;
};

struct rb_stats {
	unsigned long push_count;
	unsigned long full_count;
};

enum rb_err {
	RB_OK = 0,
	RB_ENOMEM = -12,
	RB_EINVAL = -22,
	RB_ENOSPC = -28,
};

enum rb_state {
	RB_STATE_EMPTY,
	RB_STATE_PARTIAL,
	RB_STATE_FULL,
};

int rb_init(struct rb *rb, unsigned char *buf, size_t cap);
int rb_push(struct rb *rb, const void *data, size_t len);
size_t rb_used(const struct rb *rb);
const char *rb_state_name(int state);

/* src/slot.c 에 정의. 테스트가 링크하는 rb 라이브러리에는 없다 */
unsigned char *rb_alloc_slot(struct rb *rb, size_t len);

#endif
