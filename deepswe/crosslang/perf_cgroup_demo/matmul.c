#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static void fail(const char *what)
{
    fprintf(stderr, "%s: %s\n", what, strerror(errno));
    exit(1);
}

static void path_in(char *out, size_t cap, const char *dir, const char *name)
{
    int n = snprintf(out, cap, "%s/%s", dir, name);
    if (n < 0 || (size_t)n >= cap) {
        fputs("control path is too long\n", stderr);
        exit(1);
    }
}

static void write_text(const char *path, const char *text)
{
    FILE *f = fopen(path, "w");
    if (!f) fail(path);
    if (fputs(text, f) == EOF || fclose(f) != 0) fail(path);
}

static void wait_for_fifo(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) fail(path);
    if (fgetc(f) == EOF) {
        fputs("control FIFO closed without a signal\n", stderr);
        exit(1);
    }
    if (fclose(f) != 0) fail(path);
}

static double seconds_between(struct timespec start, struct timespec end)
{
    return (double)(end.tv_sec - start.tv_sec)
         + (double)(end.tv_nsec - start.tv_nsec) / 1000000000.0;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s MATRIX_SIZE\n", argv[0]);
        return 2;
    }
    char *endptr = NULL;
    errno = 0;
    unsigned long parsed = strtoul(argv[1], &endptr, 10);
    if (errno || !endptr || *endptr || parsed < 16 || parsed > 1024) {
        fputs("MATRIX_SIZE must be an integer from 16 to 1024\n", stderr);
        return 2;
    }
    size_t n = (size_t)parsed;
    size_t cells = n * n;
    uint32_t *a = malloc(cells * sizeof(*a));
    uint32_t *b = malloc(cells * sizeof(*b));
    uint64_t *c = malloc(cells * sizeof(*c));
    if (!a || !b || !c) fail("malloc");

    for (size_t i = 0; i < n; ++i) {
        for (size_t j = 0; j < n; ++j) {
            a[i * n + j] = (uint32_t)((i * 17 + j * 13 + 1) % 251);
            b[i * n + j] = (uint32_t)((i * 11 + j * 19 + 3) % 251);
        }
    }

    const char *control = getenv("DEMO_CONTROL_DIR");
    char path[4096];
    if (control && *control) {
        path_in(path, sizeof(path), control, "ready");
        write_text(path, "ready\n");
        path_in(path, sizeof(path), control, "go.fifo");
        wait_for_fifo(path);
    }

    struct timespec start, stop;
    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) fail("clock_gettime");
    for (size_t i = 0; i < n; ++i) {
        for (size_t j = 0; j < n; ++j) {
            uint64_t sum = 0;
            for (size_t k = 0; k < n; ++k) {
                sum += (uint64_t)a[i * n + k] * b[k * n + j];
            }
            c[i * n + j] = sum;
        }
    }
    if (clock_gettime(CLOCK_MONOTONIC, &stop) != 0) fail("clock_gettime");

    uint64_t checksum = 0;
    for (size_t i = 0; i < cells; ++i) checksum += c[i];
    double elapsed = seconds_between(start, stop);
    char result[256];
    snprintf(result, sizeof(result),
             "{\"size\":%zu,\"checksum\":%" PRIu64 ",\"elapsed_s\":%.9f}\n",
             n, checksum, elapsed);
    fputs(result, stdout);
    fflush(stdout);

    if (control && *control) {
        path_in(path, sizeof(path), control, "result.json");
        write_text(path, result);
        path_in(path, sizeof(path), control, "finish.fifo");
        wait_for_fifo(path);
    }
    free(a);
    free(b);
    free(c);
    return 0;
}
