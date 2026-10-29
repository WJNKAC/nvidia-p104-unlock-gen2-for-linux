/*
 * bwtest.c —— 不依赖 CUDA toolkit 的 H2D/D2H 带宽测试
 *
 * 原理：驱动装好之后系统里就有 libcuda.so.1（NVIDIA 驱动的一部分，不是 toolkit）。
 *       用 dlopen 动态取 driver API 的几个入口，手工声明原型，就能 cuMemAlloc + cuMemcpy。
 *       不需要 cuda.h、不需要 nvcc。
 *
 * 编译：gcc -O2 -o bwtest bwtest.c -ldl
 * 用法：./bwtest [MB] [迭代次数]
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <dlfcn.h>

typedef int            CUresult;
typedef int            CUdevice;
typedef unsigned long long CUdeviceptr;
typedef void          *CUcontext;

#define CUDA_SUCCESS 0

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

#define LOAD(name) do { \
    *(void **)(&p_##name) = dlsym(h, #name); \
    if (!p_##name) { fprintf(stderr, "缺符号: %s\n", #name); return 2; } \
} while (0)

int main(int argc, char **argv)
{
    size_t mb   = (argc > 1) ? (size_t)atoi(argv[1]) : 256;
    int    iter = (argc > 2) ? atoi(argv[2]) : 10;
    size_t bytes = mb << 20;

    void *h = dlopen("libcuda.so.1", RTLD_NOW);
    if (!h) h = dlopen("libcuda.so", RTLD_NOW);
    if (!h) { fprintf(stderr, "打不开 libcuda.so.1: %s\n", dlerror()); return 2; }

    CUresult (*p_cuInit)(unsigned int);
    CUresult (*p_cuDeviceGet)(CUdevice *, int);
    CUresult (*p_cuCtxCreate_v2)(CUcontext *, unsigned int, CUdevice);
    CUresult (*p_cuMemAlloc_v2)(CUdeviceptr *, size_t);
    CUresult (*p_cuMemcpyHtoD_v2)(CUdeviceptr, const void *, size_t);
    CUresult (*p_cuMemcpyDtoH_v2)(void *, CUdeviceptr, size_t);
    CUresult (*p_cuCtxSynchronize)(void);
    CUresult (*p_cuMemFree_v2)(CUdeviceptr);

    LOAD(cuInit); LOAD(cuDeviceGet); LOAD(cuCtxCreate_v2);
    LOAD(cuMemAlloc_v2); LOAD(cuMemcpyHtoD_v2); LOAD(cuMemcpyDtoH_v2);
    LOAD(cuCtxSynchronize); LOAD(cuMemFree_v2);

    CUresult r;
    if ((r = p_cuInit(0)) != CUDA_SUCCESS) { fprintf(stderr, "cuInit=%d\n", r); return 3; }

    CUdevice dev;
    if ((r = p_cuDeviceGet(&dev, 0)) != CUDA_SUCCESS) { fprintf(stderr, "cuDeviceGet=%d\n", r); return 3; }

    CUcontext ctx;
    if ((r = p_cuCtxCreate_v2(&ctx, 0, dev)) != CUDA_SUCCESS) {
        fprintf(stderr, "cuCtxCreate=%d\n", r); return 3;
    }

    CUdeviceptr d;
    if ((r = p_cuMemAlloc_v2(&d, bytes)) != CUDA_SUCCESS) {
        fprintf(stderr, "cuMemAlloc(%zu MB)=%d\n", mb, r); return 3;
    }

    void *host = malloc(bytes);
    if (!host) { fprintf(stderr, "host malloc 失败\n"); return 3; }
    memset(host, 0xA5, bytes);

    /* 预热：第一次拷贝包含上下文/页表建立，不能计入 */
    p_cuMemcpyHtoD_v2(d, host, bytes);
    p_cuMemcpyDtoH_v2(host, d, bytes);
    p_cuCtxSynchronize();

    double t0, dt;

    t0 = now();
    for (int i = 0; i < iter; i++) p_cuMemcpyHtoD_v2(d, host, bytes);
    p_cuCtxSynchronize();
    dt = now() - t0;
    double h2d = (double)bytes * iter / dt / 1e9;

    t0 = now();
    for (int i = 0; i < iter; i++) p_cuMemcpyDtoH_v2(host, d, bytes);
    p_cuCtxSynchronize();
    dt = now() - t0;
    double d2h = (double)bytes * iter / dt / 1e9;

    printf("buffer=%zu MB  iter=%d\n", mb, iter);
    printf("H2D %.3f GB/s\n", h2d);
    printf("D2H %.3f GB/s\n", d2h);

    p_cuMemFree_v2(d);
    free(host);
    dlclose(h);
    return 0;
}
