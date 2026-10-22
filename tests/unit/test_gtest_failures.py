"""gtest 단정 실패 요약: 실제 gtest 1.18이 낸 JSON·stdout(tests/fixtures/logs/gtest_failures.*)으로 확인한다.

픽스처는 gtest_failures.cc의 일부러 실패하는 케이스를 빌드·실행해 얻었다 (--gtest_output=json, stdout).
"""

import pytest

from conftest import FIXTURES
from genut.agents.fixer import failure_text
from genut.models import FailureInfo
from genut.state import TestCase
from genut.verify.logs.gtest_json import describe_failure, parse_gtest_json, parse_gtest_stdout, split_failures
from genut.verify.review import copied_actual

LOGS = FIXTURES / "logs"
JSON = parse_gtest_json(LOGS / "gtest_failures.json")
STDOUT, _ = parse_gtest_stdout((LOGS / "gtest_failures.stdout").read_text(encoding="utf-8"))

# 케이스 → (요약, 기대값, 실제값, 관찰값, 줄)
EXPECTED = {
    "eq_both_values": ("같지 않음: push(6) [-28] ≠ 0", "0", "-28", ["-28"], 11),
    "eq_literal_first": ("같지 않음: 0 ≠ push(6) [-28]", "0", "-28", ["-28"], 13),  # 인자 순서와 상관없이 호출 쪽이 실제값
    "eq_two_calls": ("같지 않음: push(6) [-28] ≠ push(1) [0]", None, None, ["-28", "0"], 14),  # 어느 쪽인지 모르면 비운다
    "streq": ('같지 않음: name(1) ["partial"] ≠ "empty"', '"empty"', '"partial"', ['"partial"'], 15),
    "ne": ("비교 실패: push(1) != 0 아님 (값 0, 0)", None, None, ["0", "0"], 17),
    "ge": ("비교 실패: push(6) >= 0 아님 (값 -28, 0)", None, None, ["-28", "0"], 19),
    "is_true": ("참·거짓 불일치: push(6) == 0 → false (기대 true)", "true", "false", ["false"], 20),
    "near": ("오차 초과: |1.5 - 1.0| = 0.5 > 1e-3 (값 1.5, 1)", None, None, ["1.5", "1"], 22),
    "float_eq": ("같지 않음: 1.5f [1.5] ≠ 1.25f [1.25]", None, None, ["1.5", "1.25"], 23),
    "that": ("조건 불일치: push(6) → -28 (기대: is equal to 0)", "is equal to 0", "-28", ["-28"], 24),  # (of type int)는 뺀다
    "fail_msg": ("FAIL(): rb가 초기화되지 않음", None, None, [], 25),
    "with_message": ("같지 않음: push(6) [-28] ≠ 0 — 용량 4에 6바이트", "0", "-28", ["-28"], 26),
    "two_failures": ("같지 않음: push(6) [-28] ≠ 0 외 1건", "0", "-28", ["-28"], 27),
    "exception": ('예외: C++ exception with description "boom" thrown in the test body.', None, None, [], 0),
    "ptr_null": ("같지 않음: p [0x7ff6be78f07d] ≠ nullptr [(nullptr)]", None, None, ["0x7ff6be78f07d", "(nullptr)"], 29),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_summary_per_assertion_kind(name):
    summary, expected, actual, observed, line = EXPECTED[name]
    r = JSON[name]
    assert (r.status, r.message, r.expected, r.actual, r.observed, r.line) == ("fail", summary, expected, actual, observed, line)
    assert "Failure" not in r.message and r.details  # 위치 줄이 아니라 단정 내용을 요약하고, 원문은 상세에 남긴다


def test_json_and_stdout_agree():
    """크래시로 JSON이 없을 때 쓰는 stdout 경로도 같은 요약을 낸다. JSON의 한국어(바이트별 \\u00XX 이스케이프)도 바로 읽는다."""
    assert set(JSON) == set(STDOUT) and len(JSON) == 19
    for name, r in JSON.items():
        s = STDOUT[name]
        assert (s.message, s.expected, s.actual, s.observed, s.line) == (r.message, r.expected, r.actual, r.observed, r.line), name


def test_split_and_unknown_format():
    two = (LOGS / "gtest_failures.stdout").read_text(encoding="utf-8")
    block = two[two.index("fmt_test.cc:27: Failure"): two.index("[  FAILED  ] Fmt.two_failures")]
    parts = split_failures(block)
    assert len(parts) == 2 and parts[1].splitlines()[1:3] == ["Expected equality of these values:", "  push(7)"]
    odd = describe_failure("t.cc:3: Failure\nSomething new from gtest 2.0\n  Which is: 7\n  Actual: 8 (of type int)")
    assert (odd.summary, odd.observed) == ("Something new from gtest 2.0", ["7", "8"])  # 모르는 형식은 첫 줄 + 값처럼 보이는 줄


def test_expected_actual_reach_fixer_and_copy_check():
    r = JSON["eq_both_values"]
    fi = FailureInfo(category="assertion_failed", stage="run", summary=r.message, details=r.details, expected=r.expected, actual=r.actual,
                     observed=r.observed)
    text = failure_text(fi)
    assert "요약: 같지 않음: push(6) [-28] ≠ 0" in text and "기대값: 0 · 실제값: -28" in text
    before = "EXPECT_EQ(push(6), 0);"
    copied = TestCase(name="t", target_function="push", body="EXPECT_EQ(push(6), -28);", previous_body=before, last_failure=fi)
    assert copied_actual(copied) == ["-28"]  # 실제값을 기대값 자리로 옮기면 잡힌다
    kept = TestCase(name="t", target_function="push", body="EXPECT_EQ(push(1), 0);", previous_body=before, last_failure=fi)
    assert copied_actual(kept) == []  # 기대값 쪽 값은 베끼기가 아니다
