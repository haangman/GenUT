# KUnit 규칙

## 반드시
- 메모리 할당은 kunit_kzalloc(test, ...)만 사용한다. kmalloc/kfree 직접 호출 금지.
- static 함수 접근은 스텁 도구가 제공하는 방식을 따른다.

## 절대 금지
- readl/writel/ioremap 등 MMIO 직접 호출. 반드시 스텁 도구로 대체.
- msleep/udelay 등 시간 의존 코드.
- 전역 변수를 바꾼 채로 끝내는 케이스.

## 반드시 스텁
- `readl` `writel` `ioremap`

## 리뷰에서 거부할 패턴
- `\bkmalloc\(`
- `\bkfree\(`
