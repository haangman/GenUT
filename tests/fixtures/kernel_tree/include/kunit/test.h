#ifndef _KUNIT_TEST_H
#define _KUNIT_TEST_H
/* 의미 검사용 최소 KUnit 선언 (실제 커널의 include/kunit/test.h를 흉내 낸다) */
struct kunit { int status; };
struct kunit_case { void (*run_case)(struct kunit *test); const char *name; };
struct kunit_suite {
	const char name[256];
	int (*init)(struct kunit *test);
	void (*exit)(struct kunit *test);
	struct kunit_case *test_cases;
};
#define KUNIT_CASE(test_name) { .run_case = test_name, .name = #test_name }
#define KUNIT_EXPECT_EQ(test, left, right) ((void)(test), (void)((left) == (right)))
#define KUNIT_ASSERT_EQ(test, left, right) KUNIT_EXPECT_EQ(test, left, right)
#define KUNIT_EXPECT_TRUE(test, cond) ((void)(test), (void)(cond))
#define kunit_test_suites(...) static struct kunit_suite *genut_suites[] = { __VA_ARGS__ }
#endif
