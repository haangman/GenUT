"""가짜 KUnit 실행 스크립트. GenUT 계약(docs/kunit-script.md)대로 불리며 커널 빌드·QEMU 대신 표지로 결과를 낸다.

    fake_kunit.py <test_file> --mode build|run|all [--filter suite.case] --log <path> [--coverage-out <dir>]

- build: 집합 파일과 거기서 #include 한 함수별 파일을 읽는다. 케이스 본문에 COMPILE_ERROR가 있으면 gcc 형식의
  오류를 로그에 쓰고 10으로 끝난다. 성공하면 빌드 스냅샷을 <test_file>.fakebuild.json에 남긴다(run은 마지막 빌드를 돌린다)
- run: 스냅샷의 스위트를 KTAP으로 쓴다. 본문 표지: FAIL ACTUAL=<값>, CRASH(oops 뒤 패닉, 뒤 케이스는 실행 안 됨),
  KASAN(use-after-free 보고 뒤 실패), TIMEOUT(try timed out), COVERS=<줄,...>, BOOT_FAIL(부팅 실패, 종료 20)
- --coverage-out: 실행된 케이스가 COVERS로 밟은 줄을 lcov로 쓴다. 실행 가능한 줄은 <소스>.lines에서 읽는다
- 환경 변수 FAKE_KUNIT_CALLS가 있으면 호출 인자를 그 파일에 한 줄씩 남긴다 (계약 테스트용)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

INC = re.compile(r'^#include\s+"([^"]+)"', re.M)
CASE_FN = re.compile(r"^static void (\w+)\(struct kunit \*test\)\n\{\n(.*?)\n\}$", re.M | re.S)
SUITE = re.compile(r'\.name = "(\w+)"')
KCASE = re.compile(r"KUNIT_CASE\((\w+)\)")
HEAD = re.compile(r"GenUT가 생성한 KUnit 테스트입니다: (\w+) \(([^)]+)\)")


def scan(test_file: Path) -> list[dict]:
    suites = []
    for inc in INC.findall(test_file.read_text(encoding="utf-8")):
        p = (test_file.parent / inc).resolve()
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        name, head = SUITE.search(text), HEAD.search(text)
        if not name or not head:
            continue
        lines = text.splitlines()
        markers = {i + 1: line for i, line in enumerate(lines)}
        suites.append({
            "file": str(p), "suite": name.group(1), "function": head.group(1), "source": head.group(2),
            "cases": KCASE.findall(text), "bodies": {m.group(1): m.group(2) for m in CASE_FN.finditer(text)},
            "error_lines": [n for n, line in markers.items() if "COMPILE_ERROR" in line],
        })
    return suites


def eq_args(body: str) -> tuple[str, str] | None:
    """본문의 첫 KUNIT_EXPECT_EQ/ASSERT_EQ(test, L, R)에서 L, R (괄호 안 쉼표는 건너뛴다)."""
    m = re.search(r"KUNIT_(?:EXPECT|ASSERT)_EQ\(\s*test\s*,", body)
    if not m:
        return None
    args, depth, cur = [], 0, ""
    for ch in body[m.end():]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                args.append(cur.strip())
                break
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        cur += ch
    return (args[0], args[1]) if len(args) == 2 else None


def ts(n: float) -> str:
    return f"[{n:12.6f}] "


def build(test_file: Path, log: list[str]) -> int:
    suites = scan(test_file)
    errors = [(s["file"], n) for s in suites for n in s["error_lines"]]
    log.append("  CC      " + str(test_file))
    for f, n in errors:
        log.append(f"{f}:{n}:9: error: 'nope' undeclared (first use in this function)")
    if errors:
        log.append("make[4]: *** [scripts/Makefile.build:243: drivers/foo/foo_kunit.o] Error 1")
        return 10
    Path(str(test_file) + ".fakebuild.json").write_text(json.dumps(suites), encoding="utf-8")
    return 0


def run(test_file: Path, log: list[str], filter_: str, cov_out: Path | None) -> int:
    snap = Path(str(test_file) + ".fakebuild.json")
    suites = json.loads(snap.read_text(encoding="utf-8")) if snap.exists() else []
    if any("BOOT_FAIL" in b for s in suites for b in s["bodies"].values()):
        log.append(ts(0.5) + "Kernel panic - not syncing: VFS: Unable to mount root fs")
        return 20
    t = 1.0
    covered: dict[str, set[int]] = {}
    want_suite, _, want_case = filter_.partition(".")
    if want_suite:
        suites = [s for s in suites if s["suite"] == want_suite]
    log += [ts(t) + "KTAP version 1", ts(t) + f"1..{len(suites)}"]
    for i, s in enumerate(suites, 1):
        cases = [c for c in s["cases"] if not want_case or c == want_case]
        log += [ts(t) + "    KTAP version 1", ts(t) + f"    # Subtest: {s['suite']}", ts(t) + f"    1..{len(cases)}"]
        ok_suite = True
        for j, c in enumerate(cases, 1):
            t += 0.01
            body = s["bodies"].get(c, "")
            m = re.search(r"COVERS=([\d,]+)", body)
            covered.setdefault(s["source"], set()).update(int(x) for x in (m.group(1).split(",") if m else []) if x)
            if "CRASH" in body:
                log += [ts(t) + "BUG: kernel NULL pointer dereference, address: 0000000000000008",
                        ts(t) + f"RIP: 0010:{s['function']}+0x2c/0x80",
                        ts(t) + "RAX: 0000000000000000 RBX: 0000000000000008 RCX: 0000000000000000",
                        ts(t) + "Call Trace:", ts(t) + " <TASK>",
                        ts(t) + f" {s['function']}+0x2c/0x80", ts(t) + f" {c}+0x5e/0x120", ts(t) + " kunit_try_run_case+0x7a/0x140",
                        ts(t) + " </TASK>", ts(t) + "Kernel panic - not syncing: Fatal exception"]
                _coverage(test_file, cov_out, covered)
                return 0  # 스크립트는 끝까지 갔다. 결과는 로그로 판단
            if "KASAN" in body:
                log += [ts(t) + "==================================================================",
                        ts(t) + f"BUG: KASAN: slab-use-after-free in {s['function']}+0x40/0x80",
                        ts(t) + "Read of size 4 at addr ffff888003e1c010 by task kunit_try_catch/52",
                        ts(t) + "Call Trace:", ts(t) + f" {s['function']}+0x40/0x80", ts(t) + f" {c}+0x31/0x90",
                        ts(t) + "==================================================================",
                        ts(t) + f"    # {c}: EXPECTATION FAILED at lib/kunit/test.c:42", ts(t) + "    KASAN failure detected",
                        ts(t) + f"    not ok {j} {c}"]
                ok_suite = False
                continue
            if "TIMEOUT" in body:
                log += [ts(t) + f"    # {c}: try timed out", ts(t) + f"    not ok {j} {c}"]
                ok_suite = False
                continue
            m = re.search(r"FAIL ACTUAL=(\S+)", body)
            if m:
                where = os.path.relpath(s["file"]).replace(os.sep, "/")  # 커널 로그처럼 트리 기준 상대 경로 (cwd = 커널 트리)
                # 커널 lib/kunit/assert.c처럼: 식을 그대로 찍고, 리터럴이 아닌 쪽만 값 줄을 둔다 (오른쪽 값은 모르므로 리터럴로 친다)
                left, right = eq_args(body) or (f"{s['function']}(...)", "0")
                log += [ts(t) + f"    # {c}: EXPECTATION FAILED at {where}:12",
                        ts(t) + f"    Expected {left} == {right}, but",
                        ts(t) + f"        {left} == {m.group(1)} (0x{int(m.group(1)) & 0xffffffff:x})",
                        ts(t) + f"    not ok {j} {c}"]
                ok_suite = False
                continue
            log.append(ts(t) + f"    ok {j} {c}")
        log.append(ts(t) + f"{'ok' if ok_suite else 'not ok'} {i} {s['suite']}")
    _coverage(test_file, cov_out, covered)
    return 0


def _coverage(test_file: Path, cov_out: Path | None, covered: dict[str, set[int]]) -> None:
    if cov_out is None:
        return
    out = []
    for source, hit in covered.items():
        src = test_file.parent / source
        lines_file = Path(str(src) + ".lines")
        if not lines_file.exists():
            continue
        out.append(f"SF:{src}")
        out += [f"DA:{n},{1 if n in hit else 0}" for n in (int(x) for x in lines_file.read_text().split())]
        out.append("end_of_record")
    cov_out.mkdir(parents=True, exist_ok=True)
    (cov_out / "coverage.info").write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test_file")
    ap.add_argument("--mode", choices=["build", "run", "all"], required=True)
    ap.add_argument("--filter", default="")
    ap.add_argument("--log", required=True)
    ap.add_argument("--coverage-out")
    a = ap.parse_args()
    if os.environ.get("FAKE_KUNIT_CALLS"):
        with open(os.environ["FAKE_KUNIT_CALLS"], "a", encoding="utf-8") as f:
            f.write(json.dumps(sys.argv[1:]) + "\n")
    test_file = Path(a.test_file)
    log: list[str] = []
    rc = 0
    if a.mode in ("build", "all"):
        rc = build(test_file, log)
    if rc == 0 and a.mode in ("run", "all"):
        rc = run(test_file, log, a.filter, Path(a.coverage_out) if a.coverage_out else None)
    Path(a.log).write_text("\n".join(log) + "\n", encoding="utf-8")
    print(f"fake_kunit: {a.mode} -> {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
