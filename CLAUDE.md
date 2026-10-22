# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

GenUT는 C/C++ 소스 코드를 분석하여 단위 테스트를 자동 생성하는 프로젝트입니다.

- 유저 스페이스 코드 대상: **GoogleTest (gtest)** 테스트 생성
- 리눅스 커널 코드 대상: **KUnit** 테스트 생성

libclang 정적 분석으로 함수 구조를 뽑고, 역할별 LLM 에이전트가 테스트를 쓰고 고치며, 코드가 직접 빌드·실행해 통과한 케이스만 남깁니다. 구현의 기준은 `docs/design.md`이고, 모듈 docstring의 `(설계 7.4)` 같은 표기는 이 문서의 절 번호입니다. 사용자가 만드는 KUnit 실행 스크립트의 계약은 `docs/kunit-script.md`에 있습니다.

## 필수 작업 규칙

코드 수정이 발생하면 아래 절차를 **반드시** 순서대로 수행합니다.

1. **테스트 추가**: 수정한 코드에 대응하는 테스트를 반드시 추가하거나 갱신합니다.
   - 생성기 자체의 변경은 생성기 테스트로 검증합니다.
   - 생성되는 테스트 템플릿(gtest/KUnit)의 변경은 실제 생성 결과를 빌드·실행하는 테스트로 검증합니다.
   - 문서(`docs/`, `*.md`)만 바뀐 커밋은 테스트 추가 의무에서 제외합니다. 커밋 규칙은 동일하게 적용합니다.
2. **테스트 수행**: 전체 테스트 스위트를 실행하고 모두 통과하는지 확인합니다. 실패하는 테스트가 있으면 커밋하지 않습니다.
3. **git commit**: 테스트가 통과하면 반드시 커밋합니다.
   - 커밋 메시지는 **반드시 영문**으로 작성합니다.
   - 제목은 명령형 현재 시제로 작성합니다. (예: `Add KUnit generator for static functions`)
   - 필요하면 본문에 변경 이유와 테스트 방법을 적습니다.

## CLAUDE.md 업데이트

프로젝트 구조, 빌드/테스트 명령, 규칙 등이 바뀌어 이 문서가 실제와 달라지면 **CLAUDE.md를 업데이트할 수 있습니다**. 새로운 빌드 명령, 디렉터리 구조, 테스트 실행 방법 등이 정해지면 이 파일에 반영하세요. 설계 13.1에 따라 구현 단계를 하나 끝낼 때마다 아래 "진행 상황"을 함께 갱신합니다.

## 빌드 및 테스트 명령

Python 3.11+ 패키지이며 가상환경은 `uv`로 만든다.

```bash
uv venv .venv                      # 처음 한 번
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m pytest -q  # 전체 테스트 (Linux/macOS: .venv/bin/python)
.venv/Scripts/python -m pytest -q tests/unit/test_llm.py                                    # 파일 하나
.venv/Scripts/python -m pytest -q tests/unit/test_llm.py::test_builder_overflow_on_required  # 테스트 하나
.venv/Scripts/python -m pytest -q -m libclang                                               # 마커로 고르기
.venv/Scripts/genut --help         # CLI
```

- libclang이 필요한 테스트(`@pytest.mark.libclang`)는 libclang을 불러오지 못하면 `tests/conftest.py`의 `pytest_collection_modifyitems`가 건너뛴다. pip `libclang` 패키지만 있으면 되고 시스템 컴파일러는 필요 없다(가짜 시스템 헤더 `tests/fixtures/sysinc`를 쓴다).
- 통합 테스트(`tests/integration/`, `@pytest.mark.integration`)는 샘플 프로젝트를 실제 cmake·C++ 컴파일러·gtest로 빌드·실행한다. 셋 중 하나라도 없으면(gtest는 `find_package(GTest)`로 확인) 같은 훅이 건너뛴다. 건너뛴 채로 끝나면 생성 템플릿을 검증하지 못한 것이므로 `-rs`로 건너뛴 이유를 확인한다.
- 이 Windows 개발 PC의 툴체인은 MSYS2 UCRT64(gcc·cmake·ninja·gtest·gdb, `C:\msys64\ucrt64\bin`)다. 전체 테스트는 그 경로를 PATH 앞에 두고 돌린다: `PATH="/c/msys64/ucrt64/bin:$PATH" .venv/Scripts/python -m pytest -q -rs` (Bash). MinGW라 ASan/UBSan은 없어 새니타이저는 `tests/fixtures/logs/`의 로그 픽스처로만 검증한다.
- 린터·포매터는 설정되어 있지 않다.

## 진행 상황

설계 13.1의 14단계 중 1~10단계(스켈레톤, LLM 계층, 분석기, 검증 기반, 규칙·에이전트, gtest 백엔드, 수정 루프 + 관찰 도구, 커버리지 확장, KUnit 백엔드, Reviewer + 평가), 12단계(C++ 인스턴스 멤버 함수), 13단계(지식 노트)가 구현되었다. 11단계(스텁 어댑터)는 사용자가 셋업 때 주는 스텁 도구 md(`gtest-stub.md`, `kunit-stub.md`)가 있어야 시작할 수 있어 건너뛰었고, 다음은 14단계(Gemma 4 튜닝)다. `genut gen`이 gtest와 KUnit 모두에서 분석 → 스텁 → 사전검증 → 스모크 → 확장 → 수정 → 커버리지 → 리뷰 → 최종 확인을 끝까지 돈다. 지금 스텁은 `NoopStubAdapter`뿐이다. 10단계 검증 항목인 실제 Claude 기준치 측정(`evals/README.md`)은 API 키와 비용이 들어 수동으로 한다. 설계와 다른 점: KUnit 최종 확인은 이전 실행의 함수별 파일을 집합 파일에 포함만 하고 다시 검증하지 않는다(설계 7.8은 함께 검증하고 회귀하면 빼라고 함). 실제 커널·QEMU로는 돌려 보지 않았다(가짜 스크립트로 계약만 검증). CLI는 `init`·`doctor`·`product`·`gen`(`--dry-run`, `--stop-after`)·`show`·`view`·`finalize`·`replay`·`eval`이 있고, `knowledge`(`list`·`promote`·`demote`·`edit`)·`diff`가 있다.

## 아키텍처

**순서는 코드가, 판단은 LLM이.** LLM이 아닌 오케스트레이터(Python 상태 머신)가 ① 분석 → ② 스텁 → ③ 사전검증 → ④ 스모크 → ⑤ 확장 → ⑥ 수정 → ⑦ 커버리지 → ⑧ 리뷰 → 최종 확인을 진행하고(설계 3장), 에이전트(Analyzer, Stub Planner, Test Writer, Fixer, Reviewer, Note Keeper)는 pydantic 스키마로 정해진 좁은 질문에만 답한다. 에이전트끼리 대화하지 않고 공유 상태(블랙보드)를 거쳐 넘긴다.

- **블랙보드** (`state.py`): `RunState`(소스 파일 하나) → `FunctionState` → `TestCase`. 진행 단위는 케이스이며 통과한 케이스는 어떤 종료 경로에서도 잃지 않는다(설계 7.10). `LIVE`/`TERMINAL` 상태 집합이 다음 빌드 포함 여부를 정한다. 여러 케이스가 함께 쓰는 include·헬퍼는 `SharedArea`로 버전을 매기고 되돌릴 수 있다. `state.json`은 빌드마다 원자적으로 저장한다.
- **예산** (`budget.py`): 빌드·시간은 실행 단위, 토큰·수정 횟수는 함수 단위. `final_build_reserve`만큼 빌드를 최종 확인용으로 남긴다.
- **설정 겹쳐 올리기** (`config.py`): 내장 기본값 < `BACKEND_DEFAULTS`(KUnit은 빌드가 느려 예산이 작음) < `$GENUT_HOME/config.toml` < `products/<이름>/product.toml` < CLI `--set a.b=v`. 모든 모델이 `extra="forbid"`라 모르는 키는 오류다. 설정 키를 추가하면 `config.py`, `templates/config.toml`, 설계 12장을 함께 맞춘다.
- **저장소 분리** (`paths.py`, `products.py`, `context.py`): 공통 데이터는 `$GENUT_HOME`(기본 `~/.genut`), 프로덕트 데이터는 `$GENUT_HOME/products/<이름>/`. 프로덕트는 `products.toml`에 등록된 소스 트리 루트로 현재 디렉터리에서 자동 탐지한다. 프로덕트 소스 트리에는 최종 테스트 파일 말고 아무것도 쓰지 않고, 다른 프로덕트의 지식은 절대 로드하지 않는다(설계 11.5). 프로덕트·설정이 필요한 CLI 명령은 `make_context()`로 홈·프로덕트·설정을 한 번에 얻는다.
- **실행 기록** (`trace.py`): 생성 실행 한 번마다 `runs/<시각>-<라벨>/`을 만들고 `trace.jsonl`과 프롬프트·응답 전문을 모두 남긴다. 뷰어·replay·`diff`(`rundiff.py`: 바뀐 입력과 바뀐 결과)는 이 기록만 읽는다. 실행 참조는 ID 앞부분·`latest`·`latest~N`·경로이고, 같은 초에 만든 실행의 순서는 `.seq` 순번으로 정한다(이름은 라벨 알파벳순이라).
- **LLM 계층** (`llm/`): 에이전트 → `structured.ask_structured`(제약 디코딩 / JSON 추출·복구 재요청 / 펜스 코드 블록, `length`면 출력 한도 두 배로 1회 재요청) → `ChatProvider` 프로토콜(`anthropic`, `openai_compat`, `fake`). 설계 기준 모델은 운영용 사내 Gemma 4(32k 컨텍스트 중형 모델)이므로 지시는 짧게, 선택은 열거형으로, 수정은 부분 패치가 아닌 케이스 전체 재작성으로 받는다. 모든 프롬프트는 `prompt_builder.PromptBuilder`로 조립한다. 조각(`Fragment`)마다 REQUIRED/SHRINKABLE/OPTIONAL 우선순위가 있고, 넘치면 낮은 것부터 빼거나 줄이며, 필수 조각만으로 넘치면 `ContextOverflow`를 낸다. Anthropic provider는 temperature 같은 샘플링 인자를 보내지 않는다(API가 받지 않음).
- **함수 키** (C++): 함수는 `FunctionInfo.key`(`Class_method`, 오버로드는 `_2`부터, 자유 함수는 이름 그대로)로 구분한다. `FunctionState.name`, 실행 기록의 `analysis/<키>.md`, 예산, 테스트 파일 이름, 테스트 이름(`test_<키>_...`, 프롬프트의 `$function_id`)이 모두 키를 쓰고, 사람에게 보이는 이름(`$function`, 리포트 머리)은 `display`(`Class::method`)다. `--func`은 `FileAnalysis.select()`로 이름·`Class::method`·완전한 이름·키를 받는다. 인스턴스 멤버 함수면 gtest 뼈대가 fixture에 기본 생성한 `obj`(`fixture_object`)를 두고, 다룰 수 없는 경우(private·protected, 템플릿, 추상 클래스)는 `cpp_blocker`가 `untestable`로 끝낸다.
- **정적 분석** (`analysis/`): `compile_db`(상대 경로 절대화, DB에 없는 새 테스트 파일은 이웃 항목 플래그 차용, 커널 `.o.cmd` 폴백, 컴파일러의 기본 include 검색 경로 전체(`-E -v`)를 libclang에 넘김. CMake는 이 경로를 compile DB에 적지 않고 pip libclang은 모른다. Windows MinGW면 `--target`도 맞춘다) → `flag_sanitizer`(gcc 전용 플래그 제거, 모르는 플래그는 다시 파싱하며 학습) → `parser`/`extractor`(libclang → `FunctionInfo`) → `linkscope`(호출 심볼마다 링크 시 정의 유무 판정 = 스텁 필요 여부. gtest와 커널 built-in/모듈/`EXPORT_SYMBOL`을 구분) → `summarizer`(분석 보고서를 프롬프트 조각으로).
- **검증** (`verify/`): 원시 로그는 LLM에 주지 않는다. `diagnostics`가 gcc/clang/ld/modpost 출력을 구조화·중복 제거하고 케이스(공유 영역이면 `@shared`)에 귀속시킨다. `classify`가 실패 종류와 권장 전략을 정하며, 전략 사다리 `fix_case → add_stub → drop_case → narrow_scope → give_up`에서 Fixer는 권장 전략과 그 오른쪽(더 포기하는 쪽)만 고를 수 있다(`allowed_strategies`). `staged`는 빌드 전 libclang 의미 검사, `logs/`는 KTAP·gtest 결과 파싱, 크래시 브리프, 토큰 상한 안의 우선순위 축약을 맡는다.
- **오케스트레이터** (`orchestrator.py`, `finalize.py`): `Orchestrator.run()`이 단계를 차례로 돌린다. 검증 라운드(`_verify`)는 의미 검사로 먼저 걸러 낸 뒤 모든 함수를 한 번에 빌드하고 함수별로 실행한다. 빌드에는 통과·new·not_run 케이스만 넣는다. 케이스 탓인 빌드 실패는 그 케이스를 빼고 한 번 더 빌드한다. 통과하면 즉시 코드 리뷰 검사(`verify/review.py`, 7.9)를 거치며, 실제값을 베꼈는데 Fixer의 `evidence`가 반환 지점을 가리키지 않으면 소스 버그 의심이 된다. 공유 영역이 바뀐 라운드에 통과 케이스가 깨지면 되돌리고(`_rollback`), 바꾼 케이스는 `shared_change_blocked`가 된다. 포함 케이스가 모두 통과한 빌드는 녹색 스냅샷으로 남는다. `run()`은 예산 소진·`StopRun`·Ctrl+C·내부 예외가 나도 `_finish()`에서 `final_check`(예약 빌드로 최종 확인, 실패하면 스냅샷 폴백)를 거쳐 통과 케이스를 설치한다. Fixer 호출은 어느 단계에서 일어나든 `env.stage="fix"`로 기록되어 `replay --from fix`가 그 호출만 실제 LLM으로 보낸다. `genut finalize`는 run.json에 기록된 설정을 쓴다.
- **커버리지** (`coverage.py`, 오케스트레이터 `_coverage`): 백엔드의 `coverage()`가 빌드 없이 통과 케이스만 다시 실행해 대상 소스의 줄별 실행 횟수를 낸다(gtest: `.gcda`를 지우고 실행 → `gcov --json-format`, llvm-cov: profraw → `llvm-cov export -format=lcov`). `function_coverage`가 대상 함수 줄 범위로 자르고, `reach_conditions`가 미커버 줄마다 가장 가까운 분기 조건·반환값·관련 파라미터·호출(대입된 지역 변수 → 호출한 함수)을 붙인다. 라운드마다 Test Writer `coverage` 모드로 케이스를 받고, `max_coverage_rounds` 뒤에도 남은 줄은 `UnreachableAnalyzer`(프롬프트 `unreachable.md`)가 분류한다. 최종 확인 뒤 설치본 기준으로 다시 잰다.
- **지식** (`knowledge.py`, `agents/note_keeper.py`, 설계 11.5): 노트는 사람이 고칠 수 있는 md(`global.md`, 프로덕트 `product.md`의 본문과 `## 공통 후보` 절)에 한 줄씩, 메타데이터는 줄 끝 `<!-- genut ... -->`에 둔다. 실행이 끝나면 `_keep_notes`가 배울 것이 있는 실행(`has_lessons`)에만 Note Keeper를 부르고(`GenOptions.notes=False`인 eval·replay, dry-run, stop-after, 중단 때는 안 부름), `accept_notes`가 근거·분류·전략·길이·개수를 검사한다. 범위는 LLM 제안이 아니라 `ProductFacts.mentions`(프로덕트 루트 아래 선언된 심볼, product.toml 값, 루트 아래 실제 경로. KUnit은 커널 `include/`·`arch/` 제외)로 코드가 정하며, 공통 제안은 공통 후보로 저장된 뒤 같은 정규화 키(분류 + 전략 + 자리표시자 문장)가 `auto_promote_after_products`개 프로덕트에 생기면 승격된다. 다른 프로덕트 파일은 승격 판정과 `knowledge list --candidates` 집계에만 읽고 프롬프트에는 넣지 않는다. 수정 사례(`fix_patterns.yaml`)는 성공한 수정마다 코드가 남기고, Fixer는 같은 메시지 패턴 → 프로덕트 → 많이 쓰인 순으로 받는다.
- **리뷰와 평가**: ⑧ 리뷰(`_review`)는 코드 리뷰 검사를 통과한 케이스를 `Reviewer`에게 보여 규칙 위반·무의미한 단정·단정 약화·커버리지용 케이스의 확인 누락만 받는다. 지적된 케이스는 `review_reject`로 한 번만 고치고(빌드 예산이 없거나 이미 고친 적이 있으면 제거) Reviewer에게 다시 보내지 않는다. `evaluation.py`(`genut eval`)는 코퍼스 항목마다 `GenOptions(install=False)`로 생성해 지표를 모으고 `results.json`·`report.md`·`report.html`을 쓰며, 모델별 기준치와 비교한다. 평가 하나에는 provider 하나를 쓴다.
- **에이전트** (`agents/`): 모든 호출은 `Agent.call()`을 거친다. 함수당 토큰 예산 확인 → `PromptBuilder`로 한도 맞추기 → `ask_structured` → 실행 기록(`prompts/NN-<label>.md`, `llm_call` 이벤트에 잘린 조각과 프롬프트 해시) → 예산에 토큰 더하기 순이다. 예산 초과나 필수 조각만으로 넘치는 경우에는 LLM을 부르지 않는다. 실패는 모두 `AgentError(kind=...)`로 나온다. 에이전트는 프롬프트 문자열이 아니라 `Fragment` 목록을 만들고, 조립기는 목록 뒤쪽의 SHRINKABLE부터 줄이므로 목록 순서가 곧 축약 순서다. 역할 지시문은 `agents/prompts/<en|ko>/<role>.md`이고 파일 형식은 설계 11.4에 있다. Test Writer 응답은 `merge_cases`가 정리한다(통과 케이스와 같은 이름은 버림, 이름을 C 식별자로, 새 헬퍼는 공유 영역 판을 올려 추가).
- **백엔드** (`backends/`): 공통 입력·결과 형식은 `base.py`(`TestUnit` → `Rendered` → `BuildResult`/`RunResult`), 스텁 연동 지점은 `stub.py`다. gtest(`backends/gtest/`)는 사용자 CMakeLists를 고치지 않는다. 최상위 `project()` 직후 실행되는 주입 파일(`CMAKE_PROJECT_INCLUDE`)이 최상위 처리 끝에 전용 빌드 디렉터리 안 `genut-src/`의 관리 CMakeLists를 include하고, 그 파일이 테스트 파일을 glob해 파일마다 `genut_<src>_<func>` 타깃을 만든다. 그래서 작업 중에는 프로덕트 트리에 아무것도 쓰지 않고 `install()`만 쓴다. 지연 실행 안에서는 `add_subdirectory`가 금지라 include를 쓴다. `link_targets`가 비면 File API로 찾은 타깃을 테스트별로 `genut_links.cmake`에 둔다. 뼈대(`skeleton.py`)는 C 헤더를 `extern "C"`로 감싸고 fixture + `TEST_F`로 만들며, `LineMap`으로 진단을 케이스에 귀속한다. 빌드 성공 여부는 실행 파일이 새로 생겼는지로 판단하고(빌드 전에 지움), 크래시로 JSON이 없으면 마지막 `[ RUN ]` 케이스를 용의자로 gdb 백트레이스를 받는다.
- **KUnit 백엔드** (`backends/kunit/`): 빌드·실행은 사용자 스크립트를 계약(`docs/kunit-script.md`, `script.py`)대로 부른다. 사전검증은 `--mode build`, 검증 라운드는 `build()`가 `--mode all` 한 번으로 빌드·실행하고 `run()`은 그 결과를 꺼내 쓴다(QEMU 부팅이 비싸서). 격리는 `--filter` 지원 시 `--mode run`, 아니면 용의자만 담은 파일로 다시 `all`이다. 함수별 파일(`skeleton.py`)이 자기 include를 갖고, 헬퍼는 `static <함수>__<헬퍼>`로 이름을 바꾼다. 의미 검사는 함수별 파일 앞에 집합 파일의 머리(`PRELUDE`)를 붙여 하고 줄을 되돌린다. 등록된 커널 트리 경로에 작업 파일을 쓰므로 `finish()`가 설치하지 않은 파일을 되돌린다(오케스트레이터 `_finish`가 부름). compile DB가 없으면 `.<이름>.o.cmd`로 대신한다(`KernelCompileDB`).
- 여러 계층이 함께 쓰는 모델(`Diagnostic`, `CrashBrief`, `FailureInfo`, `CaseResult`)은 순환 import를 피하려고 `models.py`에 둔다.

## 코드 규칙

- **언어**(설계 결정 12): CLI 도움말·오류·doctor 메시지·리포트·뷰어 등 사용자에게 보이는 문자열과 코드 주석·docstring은 한국어로, 커밋 메시지만 영어로 쓴다. 규칙 md는 한국어이고, LLM 역할 지시문은 영어가 기본이다(`llm.prompt_language`).
- **인코딩**: 파일을 읽고 쓸 때는 항상 `encoding="utf-8"`을 명시한다. 개발 환경이 한국어 Windows(기본 cp949)라 생략하면 깨진다. `.gitattributes`가 텍스트 파일을 LF로 고정한다. 경로 포함 판정은 `paths.is_relative_to`를 쓴다(Windows 대소문자 무시).
- **설계 문서**: `docs/design.md`를 고치면 그림이 든 HTML 판 `docs/design.html`도 같은 커밋에서 맞춘다.
- **코드 수정 없이 바꾸는 데이터 파일**: 실패 분류 패턴은 `verify/classify.yaml`(위에서부터 처음 맞는 규칙), gcc 플래그 제거 목록은 `analysis/flag_sanitizer.yaml`, `genut init`이 복사하는 기본값은 `templates/`. 규칙 md의 `## 반드시 스텁`, `## 리뷰에서 거부할 패턴` 제목은 `rules.py`가 파싱하므로 바꾸지 않는다.
- **데이터 정책**(설계 9.1): 외부 API(Claude)로는 샘플·평가 코퍼스만 처리하고, 실제 프로덕트 코드는 사내 Gemma 4로만 처리한다.

## 테스트 작성

- 테스트에서 실제 LLM API를 부르지 않는다. 에이전트·구조화 출력은 `FakeProvider`로 검증한다(`responder` 콜백, 태그별 `script`, 둘 다 없으면 스키마를 만족하는 최소 응답). 실제 provider는 가짜 client(Anthropic)나 `httpx.MockTransport`(OpenAI 호환)로 요청 형태만 확인한다. 에이전트 테스트는 `FakeProvider(script={"analyzer": [...], "test_writer": [...]})`처럼 역할 이름(태그)별로 응답을 주고, 실제로 보낸 프롬프트는 `provider.calls`로 확인한다.
- 오케스트레이터 시나리오는 `tests/fake_backend.py`의 `MarkerBackend`로 돌린다. 케이스 본문의 표지(`FAIL ACTUAL=<값>`, `CRASH`, `TIMEOUT`, `COMPILE_ERROR`, `LINK_ERROR`, `CHECK_ERROR`, `FLAKY`, `FAILS_IN_FINAL`, `USES_HELPER`/`BREAKS_OTHERS`, `COVERS=33,34`)로 빌드·실행·커버리지 결과가 정해진다. 커버리지는 `executable_lines`를 정한 시나리오에서만 돌려준다. 실제 툴체인 e2e는 `tests/integration/test_gen_e2e.py`(CLI `genut gen`부터 설치까지)다.
- KUnit은 `tests/fixtures/kernel_tree/`(가짜 커널 트리)와 계약을 흉내 내는 가짜 스크립트 `tests/fixtures/kunit/fake_kunit.py`로 시험한다. 툴체인이 필요 없어 단위 테스트로 돈다. 표지는 `COMPILE_ERROR`, `FAIL ACTUAL=<값>`, `CRASH`, `KASAN`, `TIMEOUT`, `COVERS=`, `BOOT_FAIL`이고, `FAKE_KUNIT_CALLS` 환경 변수로 호출 인자를 기록한다.
- `home` 픽스처는 `GENUT_HOME`을 tmp 경로로 바꾼다. 사용자의 `~/.genut`을 건드리지 않게 이 픽스처나 `--home`을 쓴다.
- 분석 테스트는 `tests/fixtures/sample_project`(링 버퍼·문자열 유틸 C 프로젝트)와 `sample_db` 픽스처 또는 `write_compile_db()`가 만드는 compile_commands.json을 쓴다. conftest가 `tests/`를 `sys.path`에 넣으므로 헬퍼는 `from conftest import SAMPLE, write_compile_db`처럼 가져온다.
- 지식 노트 테스트(`tests/unit/test_knowledge.py`)는 tmp 홈에 프로덕트 여럿을 만들어 범위 강등·승격·다른 프로덕트 미로드를 확인한다. 오케스트레이터 시나리오에서 실패·수정이 있으면 마지막 호출이 `note_keeper`가 되므로, 호출 목록을 정확히 비교하는 테스트는 이를 고려한다(스크립트에 없으면 FakeProvider가 빈 목록을 돌려준다).
- C++ 테스트는 `tests/fixtures/cpp_project`(namespace 자유 함수, 오버로드된 생성자, 인스턴스·const·static·private 멤버, 추상 클래스, 템플릿)를 쓴다. 단위 테스트는 `tests/unit/test_cpp.py`, 실제 gtest e2e는 `tests/integration/test_cpp_e2e.py`다.
