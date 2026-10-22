# 평가 (`genut eval`)

실제 LLM으로 코퍼스의 성공률을 재는 수동 평가입니다(설계 11.4, 13.2). 자동 테스트에는 들어가지 않습니다. API 키와 비용이 듭니다.

**데이터 정책(설계 9.1)**: 외부 API(Claude)로는 이 샘플 코퍼스와 평가 코퍼스만 처리합니다. 실제 프로덕트 코드는 사내 Gemma 4(`openai_compat`)로만 평가하세요.

## 준비 (한 번)

```bash
genut init
genut product add sample --root tests/fixtures/sample_project --backend gtest
```

`$GENUT_HOME/products/sample/product.toml`에 링크 타깃을 적습니다. `rb_push`가 `slot.c`의 `rb_alloc_slot`을 부르므로 `rbslot`도 넣습니다.

```toml
[gtest]
link_targets = ["rb", "rbslot", "strutil"]
```

cmake, C/C++ 컴파일러, gtest가 PATH에 있어야 합니다(`genut doctor --product sample`로 확인).

## Claude 기준치

```bash
export ANTHROPIC_API_KEY=...
genut eval --product sample --corpus evals/sample_corpus.toml --label claude-baseline --save-baseline
```

결과는 `$GENUT_HOME/products/sample/evals/runs/<시각>-<라벨>/`(results.json, report.md, report.html)에, 기준치는 `$GENUT_HOME/evals/baselines/<모델>.json`에 남습니다. 항목마다 생성 실행 기록은 `products/sample/runs/`에 평소처럼 남으므로 `genut view`로 볼 수 있습니다. 평가는 측정만 하고 프로덕트 트리에 테스트를 설치하지 않습니다.

## 바꾼 뒤 비교

규칙 md, 역할 프롬프트(`$GENUT_HOME/prompts/<role>.md`), 예산, 모델을 바꾼 뒤 같은 코퍼스로 다시 돌리면 같은 모델의 기준치와 자동으로 비교합니다. 다른 결과와 비교하려면 `--compare <results.json>`을 줍니다.

```bash
genut eval --product sample --corpus evals/sample_corpus.toml --label rules-v2
genut eval --product sample --corpus evals/sample_corpus.toml --set llm.provider=openai_compat --label gemma4 \
  --compare ~/.genut/evals/baselines/claude-opus-5-5.json
```

지표: complete 비율, 일부라도 건진 비율(complete + partial), 케이스 빌드율·통과율, 평균 라인 커버리지, 평균 빌드 횟수·수정 라운드·토큰·소요 시간, 실패 분류 분포. 리포트에는 규칙 md 해시와 역할 프롬프트 해시가 남아 무엇이 바뀌었는지 추적할 수 있습니다.
