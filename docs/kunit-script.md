# KUnit 실행 스크립트 계약

GenUT의 KUnit 백엔드는 커널을 직접 빌드하거나 QEMU를 띄우지 않습니다. 사용자가 준비한 실행 스크립트 하나를 정해진 인자로 부르고, 스크립트가 남긴 로그 파일을 읽습니다. 이 문서는 그 스크립트가 지켜야 할 계약입니다(설계 12장 "KUnit 실행 스크립트 계약").

## 미리 할 일

| 항목 | 내용 |
|---|---|
| 집합 파일 등록 | `[kunit] test_file`(예: `drivers/foo/foo_kunit.c`)을 같은 디렉터리 Makefile에 `obj-$(CONFIG_FOO_KUNIT_TEST) += foo_kunit.o`처럼, Kconfig에 그 심볼을 **GenUT 실행 전에 한 번** 등록합니다. 파일 내용은 GenUT가 씁니다. 함수별 테스트 파일(`[kunit] test_dir`)은 집합 파일이 `#include`하므로 따로 등록하지 않습니다. |
| `.config` | `CONFIG_KUNIT=y`와 위 테스트 심볼을 켭니다. 권장: `CONFIG_KASAN=y`, `CONFIG_UBSAN=y`, `CONFIG_DEBUG_INFO=y`(설계 8.5). 라인 커버리지를 쓰려면 `CONFIG_GCOV_KERNEL=y`와 대상 파일의 `GCOV_PROFILE_<obj> := y`. |
| compile DB | 커널 트리에서 `make compile_commands.json`. 없으면 GenUT는 `.<이름>.o.cmd`를 읽습니다. |
| 설정 | `product.toml`의 `[kunit]`에 `kernel_src`, `test_file`, `test_dir`, `run_script`, `script_supports_filter`, `script_timeout_sec`, `vmlinux`(심볼화용)를 적습니다. `genut doctor`가 등록·`.config`를 점검합니다. |

## 호출

```
<script> <test_file> --mode build|run|all [--filter <suite>.<case>] --log <path> [--coverage-out <dir>]
```

| 인자 | 뜻 |
|---|---|
| `<test_file>` | 집합 파일의 절대 경로. 스크립트의 작업 디렉터리는 커널 트리 루트(`kernel_src`)입니다. |
| `--mode build` | 등록된 테스트 파일을 포함해 커널(또는 모듈)을 **빌드만** 합니다. |
| `--mode run` | **마지막 빌드 결과**를 QEMU(또는 UML)에서 실행만 합니다. |
| `--mode all` | build와 run을 연달아 합니다. |
| `--filter <suite>.<case>` | 그 케이스만 실행합니다. 선택 사항이며, 지원하면 `script_supports_filter = true`로 알립니다. |
| `--log <path>` | **전체 로그**를 이 파일에 씁니다. 빌드 실패면 컴파일러 출력이, 실행이면 커널 로그(KTAP, oops, KASAN 보고)가 그대로 들어 있어야 합니다. stdout에는 짧은 진행 메시지만 씁니다. |
| `--coverage-out <dir>` | 실행 뒤 게스트의 gcov 데이터(`/sys/kernel/debug/gcov`)를 lcov 형식 `<dir>/coverage.info`로 저장합니다. 주어지지 않으면 하지 않습니다. |

| 종료 코드 | 뜻 |
|---|---|
| 0 | 실행을 마쳤습니다. 테스트가 실패해도 0입니다(결과는 로그로 판단). |
| 10 | 빌드 실패. 컴파일러 출력이 로그에 있어야 GenUT가 오류를 케이스에 귀속할 수 있습니다. |
| 20 | 실행 실패(부팅 불가 등). |
| 그 외 | 스크립트 오류. GenUT는 `infra_error`로 멈춥니다. |

한 번 호출의 시간 상한은 `script_timeout_sec`입니다. 넘기면 GenUT가 스크립트를 죽이고 시간 초과로 처리합니다.

## GenUT가 부르는 방식

| 단계 | 호출 |
|---|---|
| 사전검증 | `--mode build`. 케이스 없는 뼈대만 담은 집합 파일로 빌드되는지 봅니다. 뼈대·`.config`·compile DB·스크립트가 이전 성공과 같으면 건너뜁니다(사전검증 캐시). |
| 검증 라운드 | `--mode all` 한 번. 이번 실행의 함수별 파일만 집합 파일에 넣고 빌드·실행합니다. 커버리지를 켜면 `--coverage-out`도 줍니다. |
| 크래시 격리 | 필터를 지원하면 `--mode run --filter <suite>.<case>`(빌드 없음). 지원하지 않으면 용의 케이스만 담은 파일로 `--mode all`. |
| 최종 확인 | `--mode all`. 통과 케이스만 담은 파일로 한 번 더 확인합니다. |

GenUT는 작업 중에 `test_file`과 `test_dir`의 함수별 파일을 고쳐 씁니다. 실행이 끝나면 설치하지 않은 파일을 원래 내용으로 되돌리고, 집합 파일은 `test_dir`에 남은 모든 함수별 파일(이전 실행에서 설치한 것 포함)로 다시 씁니다.

## 로그에서 읽는 것

- KTAP: `# Subtest: <src>_<func>_test` 블록의 `ok`/`not ok`, `# <case>: EXPECTATION FAILED at <file>:<line>`과 그 아래 값 줄, `# <case>: try timed out`.
- 크래시: `BUG: kernel NULL pointer dereference`, `BUG: KASAN: ...`, `kernel BUG at file:line!`, `WARNING: ... at file:line`, `general protection fault`, lockdep 보고, `Kernel panic`. `Call Trace:`에서 테스트 파일·대상 소스에 속한 첫 프레임을 책임 프레임으로 잡고, `vmlinux`가 있으면 `scripts/faddr2line`으로 줄 번호를 얻습니다.
- 타임스탬프(`[    1.234567]`)는 있어도 됩니다. 다른 스위트와 부팅 메시지는 GenUT가 걸러 냅니다.

## 예시 (출발점)

`tools/testing/kunit/kunit.py`를 감싼 예시입니다. 아키텍처, `.kunitconfig` 위치, 커버리지 추출은 환경마다 다르므로 고쳐 쓰세요.

```bash
#!/usr/bin/env bash
# GenUT KUnit 실행 스크립트 예시. 계약: docs/kunit-script.md
set -u
TEST_FILE=$1; shift
MODE=all FILTER="" LOG="" COV=""
while [ $# -gt 0 ]; do
  case $1 in
    --mode) MODE=$2; shift 2 ;;
    --filter) FILTER=$2; shift 2 ;;
    --log) LOG=$2; shift 2 ;;
    --coverage-out) COV=$2; shift 2 ;;
    *) echo "모르는 인자: $1" >&2; exit 2 ;;
  esac
done
BUILD_DIR=.kunit-genut
KCONFIG=$(dirname "$TEST_FILE")/.kunitconfig   # CONFIG_KUNIT=y, 테스트 심볼, CONFIG_GCOV_KERNEL=y 등
: > "$LOG"
if [ "$MODE" != run ]; then
  echo "build..."
  ./tools/testing/kunit/kunit.py build --arch=x86_64 --build_dir="$BUILD_DIR" --kunitconfig="$KCONFIG" >> "$LOG" 2>&1 || exit 10
fi
if [ "$MODE" != build ]; then
  echo "run..."
  # 테스트가 실패하면 kunit.py가 0이 아닌 값으로 끝나므로 종료 코드 대신 로그로 판단한다
  ./tools/testing/kunit/kunit.py exec --arch=x86_64 --build_dir="$BUILD_DIR" --raw_output=all ${FILTER:+"$FILTER"} >> "$LOG" 2>&1
  grep -q "KTAP version" "$LOG" || exit 20
  if [ -n "$COV" ]; then
    # 예: 게스트의 /sys/kernel/debug/gcov를 꺼내 온 디렉터리에서 lcov로 변환한다 (환경에 맞게 구현)
    lcov --capture --directory "$BUILD_DIR" --output-file "$COV/coverage.info" >> "$LOG" 2>&1 || true
  fi
fi
exit 0
```

GenUT의 자체 테스트는 이 계약을 흉내 내는 가짜 스크립트(`tests/fixtures/kunit/fake_kunit.py`)로 계약을 검증합니다.
