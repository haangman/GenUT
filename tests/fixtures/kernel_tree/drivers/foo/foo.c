#include <linux/errno.h>
#include "foo.h"

int foo_clamp(int v, int lo, int hi)
{
	if (lo > hi)
		return -EINVAL;
	if (v < lo)
		return lo;
	if (v > hi)
		return hi;
	return v;
}
