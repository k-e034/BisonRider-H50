/* カードの BAR0 窓（非キャッシュの MMIO）を、幅をそろえた読み出しで写す。
 *
 * 汲み出し（s4-boot --exit-pump）は ctypes.string_at（glibc memcpy）で読んでいて、約 6 Mbps で頭打ちだった
 * （ch27v-55）。非キャッシュの領域では memcpy の rep movsb が細かい読みに落ちることがあるので、
 * 1 回の読みを 4 / 8 / 16 / 32 バイトに固定した写しを用意し、--pump-bench で速さと正しさを比べる。
 *
 *   gcc -O2 -mavx2 -shared -fPIC -o tools/fastread.so tools/fastread.c
 *
 * rd(dst, base, off, n, w): base+off から n バイトを dst へ。読みは w バイト境界にそろえる
 * （前後にはみ出した分は読み捨てる）。base は窓の先頭（4 KB 境界）、w は 4/8/16/32。
 */
#include <immintrin.h>
#include <stdint.h>
#include <string.h>

#define CHUNK 4096

static void rd_aligned(uint8_t *dst, const volatile uint8_t *src, size_t n, int w)
{
    /* src と n は w の倍数 */
    size_t i;
    switch (w) {
    case 4:
        for (i = 0; i < n; i += 4)
            *(uint32_t *)(dst + i) = *(const volatile uint32_t *)(src + i);
        break;
    case 8:
        for (i = 0; i < n; i += 8)
            *(uint64_t *)(dst + i) = *(const volatile uint64_t *)(src + i);
        break;
    case 16:
        for (i = 0; i < n; i += 16)
            _mm_storeu_si128((__m128i *)(dst + i), _mm_load_si128((const __m128i *)(src + i)));
        break;
    case 32:
        for (i = 0; i < n; i += 32)
            _mm256_storeu_si256((__m256i *)(dst + i), _mm256_load_si256((const __m256i *)(src + i)));
        break;
    }
}

int rd(uint8_t *dst, const uint8_t *base, size_t off, size_t n, int w)
{
    uint8_t tmp[CHUNK + 64];
    if (w != 4 && w != 8 && w != 16 && w != 32)
        return -1;
    while (n) {
        /* 揃えは絶対番地で取る（整列ロードは番地が w の倍数でないと落ちる。BAR の先頭はページ境界） */
        uintptr_t p = (uintptr_t)base + off;
        uintptr_t a = p & ~(uintptr_t)(w - 1);
        size_t take = n < CHUNK ? n : CHUNK;
        uintptr_t b = (p + take + w - 1) & ~(uintptr_t)(w - 1);
        rd_aligned(tmp, (const volatile uint8_t *)a, b - a, w);
        memcpy(dst, tmp + (p - a), take);
        dst += take;
        off += take;
        n -= take;
    }
    return 0;
}
