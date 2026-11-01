# macOS: an exited, unreaped child still looks alive

**Problem.** The first CI run's macOS job failed `test_the_whole_protocol_against_a_real_process`: `ops stop` wrote
the stop file, the fake app exited at once, yet `stop_app` waited the full 30 s, fell back to a kill and then reported
"Still running after the forced kill". Linux and Windows passed.

**Cause.** `processes.pid_alive` probes with `os.kill(pid, 0)`, which succeeds for a zombie (exited, not yet reaped by
its parent). The zombie check read `/proc/<pid>/stat`, and macOS has no `/proc`, so the `OSError` branch answered
"alive". In the test the child's parent is pytest, which only reaps it after `stop_app` returns; in production the same
happens whenever the app's parent (the supervisor, a shell) has not waited on it yet.

**Fix.** Without `/proc`, `pid_alive` asks `ps -o stat= -p <pid>`: a state starting with `Z` is a zombie, and no row
means the process is gone (`processes._ps_alive`). Tests cover a real unreaped child on POSIX and the ps parsing on
every OS.

**How to avoid.** On POSIX, "alive" means kill(pid, 0) succeeds *and* the process is not a zombie, and each OS needs
its own way to read the state. Run process code on all three OSes (the CI `portable` matrix) before trusting it.
