#include <stddef.h>
#include "str_util.h"

size_t str_count_char(const char *s, char c)
{
	size_t n = 0;

	if (s == NULL)
		return 0;
	for (; *s; s++) {
		if (*s == c)
			n++;
	}
	return n;
}

int str_is_blank(const char *s)
{
	if (s == NULL)
		return 1;
	while (*s) {
		if (*s != ' ' && *s != '\t' && *s != '\n')
			return 0;
		s++;
	}
	return 1;
}

int str_clamp(int v, int lo, int hi)
{
	if (lo > hi)
		return lo;
	if (v < lo)
		return lo;
	if (v > hi)
		return hi;
	return v;
}
