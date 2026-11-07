
/*
 * Copyright (C) Nginx, Inc.
 */


#ifndef _NGX_SIMD_H_INCLUDED_
#define _NGX_SIMD_H_INCLUDED_


#include <ngx_config.h>
#include <ngx_core.h>


/*
 * 16-byte unsigned byte comparisons used to skip runs of ordinary bytes;
 * callers keep a scalar path for short buffers and other platforms
 */

#if (defined(__GNUC__) || defined(__clang__))

#if defined(__SSE2__)
#include <emmintrin.h>
#define NGX_SIMD_SSE2  1
#define NGX_HAVE_SIMD  1

#elif defined(__aarch64__) && defined(__ARM_NEON)
#include <arm_neon.h>
#define NGX_SIMD_NEON  1
#define NGX_HAVE_SIMD  1
#endif

#endif


#if (NGX_HAVE_SIMD)

#define NGX_SIMD_WIDTH  16


#if (NGX_SIMD_SSE2)

typedef __m128i  ngx_simd_t;

#define ngx_simd_load(p)    _mm_loadu_si128((const __m128i *) (p))
#define ngx_simd_set1(c)    _mm_set1_epi8((char) (c))
#define ngx_simd_or(a, b)   _mm_or_si128(a, b)
#define ngx_simd_eq(a, b)   _mm_cmpeq_epi8(a, b)

/* unsigned a <= b and a >= b */
#define ngx_simd_le(a, b)   _mm_cmpeq_epi8(_mm_min_epu8(a, b), a)
#define ngx_simd_ge(a, b)   _mm_cmpeq_epi8(_mm_max_epu8(a, b), a)


/* the index of the first matching byte, or NGX_SIMD_WIDTH */

static ngx_inline ngx_uint_t
ngx_simd_first(ngx_simd_t m)
{
    unsigned int  mask;

    mask = (unsigned int) _mm_movemask_epi8(m);

    return mask ? (ngx_uint_t) __builtin_ctz(mask) : NGX_SIMD_WIDTH;
}

#else /* NGX_SIMD_NEON */

typedef uint8x16_t  ngx_simd_t;

#define ngx_simd_load(p)    vld1q_u8((const uint8_t *) (p))
#define ngx_simd_set1(c)    vdupq_n_u8((uint8_t) (c))
#define ngx_simd_or(a, b)   vorrq_u8(a, b)
#define ngx_simd_eq(a, b)   vceqq_u8(a, b)
#define ngx_simd_le(a, b)   vcleq_u8(a, b)
#define ngx_simd_ge(a, b)   vcgeq_u8(a, b)


static ngx_inline ngx_uint_t
ngx_simd_first(ngx_simd_t m)
{
    uint64_t  mask;

    /* narrow 16 byte flags into 16 nibbles of a 64-bit mask */
    mask = vget_lane_u64(vreinterpret_u64_u8(
                             vshrn_n_u16(vreinterpretq_u16_u8(m), 4)), 0);

    return mask ? (ngx_uint_t) __builtin_ctzll(mask) >> 2 : NGX_SIMD_WIDTH;
}

#endif

#endif /* NGX_HAVE_SIMD */


#endif /* _NGX_SIMD_H_INCLUDED_ */
