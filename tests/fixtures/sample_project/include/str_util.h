#ifndef STR_UTIL_H
#define STR_UTIL_H

#include <stddef.h>

size_t str_count_char(const char *s, char c);
int str_is_blank(const char *s);
int str_clamp(int v, int lo, int hi);

#endif
