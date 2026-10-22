#ifndef COUNTER_H
#define COUNTER_H

namespace demo {

int add_clamped(int a, int b, int limit);

class Counter {
public:
	Counter();
	explicit Counter(int limit);
	int add(int n);
	int value() const;
	void reset();
	static bool valid_limit(int limit);

private:
	int check(int n) const;
	int value_ = 0;
	int limit_;
};

class Shape {
public:
	virtual ~Shape() = default;
	virtual int area() const = 0;
	int twice() const;
};

template <typename T>
class Box {
public:
	T get() const { return v_; }

private:
	T v_{};
};

} // namespace demo

#endif
