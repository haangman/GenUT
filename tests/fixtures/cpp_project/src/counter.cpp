#include "counter.h"

namespace demo {

int add_clamped(int a, int b, int limit)
{
	int s = a + b;
	if (s > limit)
		return limit;
	return s;
}

Counter::Counter() : limit_(10) {}

Counter::Counter(int limit) : limit_(limit) {}

int Counter::add(int n)
{
	if (n < 0)
		return -1;
	if (value_ + n > limit_)
		return -1;
	value_ += n;
	return value_;
}

int Counter::value() const
{
	return value_;
}

void Counter::reset()
{
	value_ = 0;
}

bool Counter::valid_limit(int limit)
{
	return limit > 0;
}

int Counter::check(int n) const
{
	return n < limit_ ? n : limit_;
}

int Shape::twice() const
{
	return 2 * area();
}

int box_get(const Box<int> &b)
{
	return b.get();
}

} // namespace demo
