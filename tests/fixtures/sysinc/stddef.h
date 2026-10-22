/* 테스트용 최소 시스템 헤더 (libclang 내장 헤더 대신) */
#ifndef _GENUT_FAKE_STDDEF_H
#define _GENUT_FAKE_STDDEF_H
typedef __SIZE_TYPE__ size_t;
#define NULL ((void *)0)
#endif
