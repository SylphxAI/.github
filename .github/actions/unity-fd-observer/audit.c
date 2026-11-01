#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

/* glibc's audit namespace keeps this observer's libc/errno separate from the
 * editor. Every observed call still reaches its original resolved function. */
typedef int32_t (*close_on_exec_fn)(intptr_t);
typedef void (*assert_fail_fn)(const char *, const char *, unsigned int, const char *);
static close_on_exec_fn original_close_on_exec;
static assert_fail_fn original_assert_fail;
static __thread intptr_t pipe_fd = -1;
static __thread int inside_pipe_call;

static void emit(const char *event, intptr_t fd, int known)
{
    struct rlimit limit;
    if (getrlimit(RLIMIT_NOFILE, &limit) != 0)
        return;
    long open_max = sysconf(_SC_OPEN_MAX);
    fprintf(stderr,
            "unity-fd-observer event=%s pid=%ld fd=%" PRIdPTR " fd_known=%d "
            "soft=%" PRIuMAX " hard=%" PRIuMAX " sysconf=%ld\n",
            event, (long)getpid(), fd, known, (uintmax_t)limit.rlim_cur,
            (uintmax_t)limit.rlim_max, open_max);
    fflush(stderr);
}

__attribute__((constructor)) static void start(void)
{
    emit("process-start", -1, 0);
}

static int32_t observed_close_on_exec(intptr_t fd)
{
    pipe_fd = fd;
    inside_pipe_call = 1;
    emit("named-pipe-close-on-exec", fd, 1);
    int32_t result = original_close_on_exec(fd);
    inside_pipe_call = 0;
    return result;
}

__attribute__((noreturn)) static void observed_assert_fail(
    const char *assertion, const char *file, unsigned int line, const char *function)
{
    emit("assertion", pipe_fd, inside_pipe_call);
    original_assert_fail(assertion, file, line, function);
    __builtin_unreachable();
}

unsigned int la_version(unsigned int version)
{
    return version >= LAV_CURRENT ? LAV_CURRENT : 0;
}

unsigned int la_objopen(struct link_map *map, Lmid_t namespace_id, uintptr_t *cookie)
{
    (void)map;
    (void)namespace_id;
    (void)cookie;
    return LA_FLG_BINDTO | LA_FLG_BINDFROM;
}

uintptr_t la_symbind64(Elf64_Sym *symbol, unsigned int index, uintptr_t *reference,
                      uintptr_t *definition, unsigned int *flags, const char *name)
{
    (void)index;
    (void)reference;
    (void)definition;
    (void)flags;
    if (strcmp(name, "SystemNative_FcntlSetCloseOnExec") == 0) {
        close_on_exec_fn resolved = (close_on_exec_fn)symbol->st_value;
        /* Do not redirect a second implementation to the first library. */
        if (original_close_on_exec != NULL && original_close_on_exec != resolved)
            return symbol->st_value;
        original_close_on_exec = resolved;
        return (uintptr_t)observed_close_on_exec;
    }
    if (strcmp(name, "__assert_fail") == 0) {
        assert_fail_fn resolved = (assert_fail_fn)symbol->st_value;
        if (original_assert_fail != NULL && original_assert_fail != resolved)
            return symbol->st_value;
        original_assert_fail = resolved;
        return (uintptr_t)observed_assert_fail;
    }
    return symbol->st_value;
}
