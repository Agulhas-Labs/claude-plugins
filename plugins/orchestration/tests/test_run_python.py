"""Tests for run-python.sh. Run: python3 -m unittest discover -s plugins/orchestration/tests"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "hooks", "run-python.sh")
SH = "/bin/sh"


def write_executable(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class RunPythonTests(unittest.TestCase):
    def setUp(self):
        self.bindir = tempfile.mkdtemp(prefix="run-python-test-")
        self.addCleanup(shutil.rmtree, self.bindir)

    def run_launcher(self, *args, path=None):
        return subprocess.run(
            [SH, SCRIPT, *args],
            env={"PATH": path if path is not None else self.bindir},
            capture_output=True,
            text=True,
        )

    def test_a_working_interpreter_is_exec_d_with_the_args(self):
        write_executable(os.path.join(self.bindir, "python3"), 'echo "$@"\n')
        result = self.run_launcher("hello", "world")
        self.assertEqual(result.stdout.strip(), "hello world")
        self.assertEqual(result.returncode, 0)

    def test_no_candidate_on_path_prints_nothing_and_exits_0(self):
        # bindir is empty: no python3/python/py, and no uname, so PATH carries no interpreter at all.
        result = self.run_launcher("hello")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 0)

    def test_a_candidate_failing_the_probe_is_skipped(self):
        # Simulate the Windows branch: a fake `uname` selects it, a fake `py` fails the Python-3 probe
        # (the Microsoft Store placeholder), and `python` is the working fallback.
        write_executable(os.path.join(self.bindir, "uname"), 'echo "MINGW64_NT-10.0"\n')
        write_executable(os.path.join(self.bindir, "py"), "exit 1\n")
        write_executable(os.path.join(self.bindir, "python"), 'if [ "$1" = "-c" ]; then exit 0; fi\necho "$@"\n')
        result = self.run_launcher("hello", "world")
        self.assertEqual(result.stdout.strip(), "hello world")
        self.assertEqual(result.returncode, 0)

    def test_non_windows_broken_python3_falls_back_to_working_python(self):
        # A `python3` on PATH that exists but fails (a pyenv/asdf shim with no version selected, or
        # the macOS stub without Command Line Tools) must not make the launcher exit non-zero — it
        # should be probed, found broken, and skipped in favor of a working `python`.
        write_executable(os.path.join(self.bindir, "python3"), "exit 127\n")
        write_executable(os.path.join(self.bindir, "python"), 'if [ "$1" = "-c" ]; then exit 0; fi\necho "$@"\n')
        result = self.run_launcher("hello", "world")
        self.assertEqual(result.stdout.strip(), "hello world")
        self.assertEqual(result.returncode, 0)

    def test_non_windows_only_python_present_is_used(self):
        # No `python3` on PATH at all: the only working candidate is `python`.
        write_executable(os.path.join(self.bindir, "python"), 'if [ "$1" = "-c" ]; then exit 0; fi\necho "$@"\n')
        result = self.run_launcher("hello", "world")
        self.assertEqual(result.stdout.strip(), "hello world")
        self.assertEqual(result.returncode, 0)

    def test_windows_branch_prefers_a_working_py_over_python(self):
        write_executable(os.path.join(self.bindir, "uname"), 'echo "MINGW64_NT-10.0"\n')
        write_executable(os.path.join(self.bindir, "py"), 'if [ "$1" = "-c" ]; then exit 0; fi\necho "py $@"\n')
        write_executable(os.path.join(self.bindir, "python"), 'if [ "$1" = "-c" ]; then exit 0; fi\necho "python $@"\n')
        result = self.run_launcher("hello", "world")
        self.assertEqual(result.stdout.strip(), "py hello world")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
