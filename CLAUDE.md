# CLAUDE.md

이 파일은 Claude Code가 이 저장소에서 작업할 때 따라야 하는 지침입니다.

## 프로젝트 개요

GenUT는 C/C++ 소스 코드를 분석하여 단위 테스트를 자동 생성하는 프로젝트입니다.

- 유저 스페이스 코드 대상: **GoogleTest (gtest)** 테스트 생성
- 리눅스 커널 코드 대상: **KUnit** 테스트 생성

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

프로젝트 구조, 빌드/테스트 명령, 규칙 등이 바뀌어 이 문서가 실제와 달라지면 **CLAUDE.md를 업데이트할 수 있습니다**. 새로운 빌드 명령, 디렉터리 구조, 테스트 실행 방법 등이 정해지면 이 파일에 반영하세요.

## 빌드 및 테스트 명령

Python 3.11+ 패키지이며 가상환경은 `uv`로 만든다.

```bash
uv venv .venv                      # 처음 한 번
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m pytest -q  # 전체 테스트 (Linux/macOS: .venv/bin/python)
.venv/Scripts/genut --help         # CLI
```

- 통합 테스트(`@pytest.mark.integration`)는 cmake·C/C++ 컴파일러·gtest가 없으면 자동으로 건너뛴다.
- libclang이 필요한 테스트(`@pytest.mark.libclang`)는 libclang을 불러오지 못하면 건너뛴다.

## 구조

- `docs/design.md`, `docs/design.html`: 설계 문서 (구현의 기준)
- `src/genut/`: 패키지. 하위 패키지는 `analysis`(정적 분석), `verify`(진단·분류·로그 축약), `llm`(provider·구조화 출력·프롬프트 조립), `agents`(역할별 에이전트), `backends`(gtest·KUnit), `viewer`(HTML 뷰어)
- `tests/unit`: 외부 도구 없이 도는 단위 테스트, `tests/fixtures`: 고정 픽스처
