// gtest 단정 실패 메시지 형식 수집용. 모든 케이스가 일부러 실패한다.
#include <gtest/gtest.h>
#include <gmock/gmock.h>
#include <stdexcept>
#include <string>

static int push(int n) { return n > 4 ? -28 : 0; }
static const char *name(int s) { return s == 0 ? "empty" : "partial"; }
#define RB_OK 0

TEST(Fmt, eq_both_values) { EXPECT_EQ(push(6), RB_OK); }
TEST(Fmt, eq_literal) { EXPECT_EQ(push(6), 0); }
TEST(Fmt, eq_literal_first) { EXPECT_EQ(0, push(6)); }
TEST(Fmt, eq_two_calls) { EXPECT_EQ(push(6), push(1)); }
TEST(Fmt, streq) { EXPECT_STREQ(name(1), "empty"); }
TEST(Fmt, string_eq) { EXPECT_EQ(std::string(name(1)), "empty"); }
TEST(Fmt, ne) { EXPECT_NE(push(1), RB_OK); }
TEST(Fmt, lt) { EXPECT_LT(push(1), -1); }
TEST(Fmt, ge) { ASSERT_GE(push(6), 0); }
TEST(Fmt, is_true) { EXPECT_TRUE(push(6) == RB_OK); }
TEST(Fmt, is_false) { EXPECT_FALSE(push(1) == RB_OK); }
TEST(Fmt, near) { EXPECT_NEAR(1.5, 1.0, 1e-3); }
TEST(Fmt, float_eq) { EXPECT_FLOAT_EQ(1.5f, 1.25f); }
TEST(Fmt, that) { EXPECT_THAT(push(6), ::testing::Eq(0)); }
TEST(Fmt, fail_msg) { FAIL() << "rb가 초기화되지 않음"; }
TEST(Fmt, with_message) { EXPECT_EQ(push(6), RB_OK) << "용량 4에 6바이트"; }
TEST(Fmt, two_failures) { EXPECT_EQ(push(6), RB_OK); EXPECT_EQ(push(7), 1); }
TEST(Fmt, exception) { throw std::runtime_error("boom"); }
TEST(Fmt, ptr_null) { const char *p = name(0); EXPECT_EQ(p, nullptr); }
