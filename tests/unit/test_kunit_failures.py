"""KUnit 단정 실패 요약: 커널 lib/kunit/assert.c 형식대로 만든 tests/fixtures/logs/kunit_failures.log로 확인한다.

gtest와 같은 요약 모양(assertion.py)을 쓰므로 같은 종류의 실패는 프레임워크가 달라도 같은 수정 사례 패턴이 된다.
"""

import pytest

from conftest import FIXTURES
from genut.knowledge import message_pattern
from genut.verify.logs.gtest_json import parse_gtest_json
from genut.verify.logs.ktap import describe_kunit, parse_ktap

SUITE = parse_ktap((FIXTURES / "logs" / "kunit_failures.log").read_text(encoding="utf-8"))["foo_kinds_test"].results

# 케이스 → (요약, 기대값, 실제값, 관찰값, 줄)
EXPECTED = {
    "test_eq_literal": ("같지 않음: foo_clamp(-1, 0, 10) [0] ≠ 1", "1", "0", ["0"], 12),  # 리터럴 쪽은 값 줄이 없다
    "test_eq_both": ("같지 않음: foo_clamp(11, 0, 10) [10] ≠ hi [9]", "9", "10", ["10", "9"], 18),
    "test_eq_two_calls": ("같지 않음: foo_clamp(1, 5, 0) [-22] ≠ foo_clamp(0, 5, 0) [0]", None, None, ["-22", "0"], 23),
    "test_ne": ("비교 실패: foo_clamp(5, 0, 10) != 5 아님 (값 5, 5)", None, None, ["5", "5"], 28),
    "test_lt": ("비교 실패: foo_clamp(-1, 0, 10) < 0 아님 (값 0, 0)", None, None, ["0", "0"], 33),
    "test_true": ("참·거짓 불일치: foo_ready(dev) → false (기대 true)", "true", "false", ["false"], 38),
    "test_false": ("참·거짓 불일치: foo_busy(dev) → true (기대 false)", "false", "true", ["true"], 43),
    "test_not_null": ("NULL 포인터: foo_alloc(test, 8) → NULL (기대: 유효한 포인터)", None, "NULL", ["NULL"], 48),
    "test_not_err": ("오류 포인터: foo_open(dev) → -12 (기대: 유효한 포인터)", None, "-12", ["-12"], 53),
    "test_streq": ('같지 않음: foo_name(1) ["partial"] ≠ "empty"', '"empty"', '"partial"', ['"partial"'], 58),
    "test_ptr_eq": ("같지 않음: foo_find(dev, 3) [0000000000000000] ≠ dev->slots[3] [ffff888003a1c000]", "ffff888003a1c000", "0000000000000000",
                    ["0000000000000000", "ffff888003a1c000"], 63),
    "test_fail": ("KUNIT_FAIL: 레지스터 맵이 비어 있음", None, None, [], 68),
    "test_eq_msg": ("같지 않음: foo_clamp(20, 0, 10) [10] ≠ 20 — 상한 10을 넘음", "20", "10", ["10"], 73),  # _MSG 매크로의 메시지
    "test_two_failures": ("같지 않음: foo_clamp(-1, 0, 10) [0] ≠ 1 외 1건", "1", "0", ["0"], 78),
    "test_assert": ("같지 않음: foo_init(dev) [-19] ≠ 0", "0", "-19", ["-19"], 84),  # ASSERTION도 같은 형식
    "test_memeq": ("같지 않음: dev->buf ≠ want", None, None, [], 89),  # 16진 덤프는 요약에서 뺀다
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_summary_per_assertion_kind(name):
    summary, expected, actual, observed, line = EXPECTED[name]
    r = SUITE[name]
    assert (r.status, r.message, r.expected, r.actual, r.observed, r.line) == ("fail", summary, expected, actual, observed, line)
    assert "FAILED at" in r.details  # 원문은 상세에 남는다


def test_unknown_format_and_empty_block():
    odd = describe_kunit(["    # t: EXPECTATION FAILED at x.c:1", "    Expected something new in kunit 7.0", "        v == 3 (0x3)"])
    assert (odd.summary, odd.observed) == ("Expected something new in kunit 7.0", ["3"])
    assert describe_kunit(["    # t: EXPECTATION FAILED at x.c:1"]).summary == "t: EXPECTATION FAILED at x.c:1"


def test_same_failure_same_pattern_across_frameworks():
    """같은 종류의 실패는 gtest와 KUnit에서 같은 모양이라, 수정 사례 패턴도 같아진다."""
    g = parse_gtest_json(FIXTURES / "logs" / "gtest_failures.json")["is_true"].message
    k = SUITE["test_true"].message
    assert g.split(":")[0] == k.split(":")[0] == "참·거짓 불일치"
    assert message_pattern(g, {"push"}) == message_pattern("참·거짓 불일치: push(6) == 0 → false (기대 true)", {"push"})
