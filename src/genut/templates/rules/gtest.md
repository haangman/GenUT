# gtest 규칙

## 반드시
- EXPECT_* 를 기본으로 쓰고, 이후 단계가 의미 없을 때만 ASSERT_* 를 쓴다.
- C 함수는 GenUT가 extern "C" 블록으로 감싼 include를 통해 호출한다. include를 직접 추가하지 않는다.

## 절대 금지
- 전역 상태를 바꾼 채로 끝내는 케이스.
