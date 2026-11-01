# Unity descriptor observation

This opt-in action observes Linux x86_64/glibc Unity editor containers selected
by the job's existing `UNITY_IMAGE`. It does not activate Unity, start another
editor, change a resource limit, or retry a build. Use a full published commit
SHA when enabling it before an existing real Unity run:

```yaml
- uses: SylphxAI/.github/.github/actions/unity-fd-observer@<full-commit-sha> # main
  with:
    enabled: 'true'
```

The action compiles a small glibc audit library with the runner's C compiler and
adds a job-local Docker wrapper to `GITHUB_PATH`. Existing `docker run` commands
whose arguments contain the exact current `UNITY_IMAGE` receive the library as
a read-only bind mount and `LD_AUDIT`. Other Docker commands pass through.
Commands with Docker global options before `run` are not instrumented. An
existing explicit `LD_AUDIT` is refused rather than replaced.

The observer emits `unity-fd-observer` lines at process startup, immediately
before `SystemNative_FcntlSetCloseOnExec` (the Mono NamedPipe path), and at
`__assert_fail`. Each line contains PID, the actual native descriptor argument
when known, nofile soft/hard limits and `sysconf(_SC_OPEN_MAX)`. An assertion
outside that native call has `fd_known=0`; its stale descriptor is not evidence.
The original resolved native functions always execute. An assertion still
terminates the process and its Docker exit status still propagates. No command
arguments, environment values, credentials or descriptor targets are logged.

This changes function binding and timing, so a successful observed run is not
proof that an intermittent failure is fixed. A recurrence with an observed
native argument and limits is the diagnostic receipt. Removing this action
step removes the observer from the next fresh runner; no runner configuration
or shared image is mutated.

Primary source read 2026-11-01: Unity-Technologies/mono `pal_io.c` at
`bb3f028d171e0bf4810603f66926ab0ac95848db` declares
`int32_t SystemNative_FcntlSetCloseOnExec(intptr_t fd)` and passes the descriptor
through `ToFileDescriptor` before `fcntl`. Unity-Technologies/mono PR2226
explains why a changed runtime limit can strand a still-valid descriptor above
`_SC_OPEN_MAX`; it does not establish that cause for an uninstrumented build.

The glibc `rtld-audit(7)` interface was read on 2026-11-01. `la_symbind64`
may return an alternate binding address, including for `dlsym`; the observer
retains the original address and calls it with the exact documented signature.
A same-image Kata probe on 2026-11-01 observed actual Mono pipe arguments 7/8
at limits 8,192, 65,536 and 1,048,576 with successful connections. Local native
controls retain both errno and SIGABRT, including a valid fd 3,000 stranded
above a lowered soft limit of 1,024. These controls validate the observer, not
the cause of an earlier editor abort.
