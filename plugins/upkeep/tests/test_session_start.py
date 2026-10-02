"""Tests for the upkeep hook and stamp script. Run: python3 -m unittest discover -s plugins/upkeep/tests"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOOK = os.path.join(ROOT, "hooks", "session_start.sh")
STAMP = os.path.join(ROOT, "scripts", "stamp.sh")
LINE = "Config upkeep is due (last run {}): run /upkeep when convenient.\n"


class Env:
    """A temp HOME, plugin data dir, and a fake `date` first on PATH so today is pinned."""

    def __init__(self, test, today="2026-10-02"):
        self.tmp = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.tmp, "data")
        bin_dir = os.path.join(self.tmp, "bin")
        for d in (self.home, self.data, bin_dir):
            os.makedirs(d)
        fake = os.path.join(bin_dir, "date")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\necho {today}\n")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        self.bin = bin_dir

    def stamp(self, content, directory=None):
        with open(os.path.join(directory or self.data, "last-upkeep"), "w", newline="") as f:
            f.write(content)

    def run(self, payload='{"source":"startup"}', interval=None, data=True, script=HOOK, args=()):
        env = {"PATH": self.bin + os.pathsep + os.environ["PATH"], "HOME": self.home}
        if data:
            env["CLAUDE_PLUGIN_DATA"] = self.data
        if interval is not None:
            env["UPKEEP_INTERVAL_DAYS"] = interval
        return subprocess.run(["sh", script, *args], input=payload, capture_output=True, text=True, env=env, timeout=10)


class SessionStartHook(unittest.TestCase):
    def test_due_when_older_than_interval(self):
        e = Env(self)
        e.stamp("2026-09-10\n")
        r = e.run()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, LINE.format("2026-09-10"), ""))

    def test_not_due_prints_nothing(self):
        e = Env(self)
        e.stamp("2026-09-30\n")
        r = e.run()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_boundary_is_due_on_the_interval_day(self):
        e = Env(self)
        e.stamp("2026-09-18\n")  # exactly 14 days
        self.assertEqual(e.run().stdout, LINE.format("2026-09-18"))
        e.stamp("2026-09-19\n")  # 13 days
        self.assertEqual(e.run().stdout, "")

    def test_missing_file_says_never(self):
        self.assertEqual(Env(self).run().stdout, LINE.format("never"))

    def test_interval_zero_is_silent_even_with_no_stamp(self):
        self.assertEqual(Env(self).run(interval="0").stdout, "")
        self.assertEqual(Env(self).run(interval="000").stdout, "")

    def test_interval_override(self):
        e = Env(self)
        e.stamp("2026-09-30\n")
        self.assertEqual(e.run(interval="2").stdout, LINE.format("2026-09-30"))
        self.assertEqual(e.run(interval="3").stdout, "")

    def test_bad_interval_falls_back_to_fourteen(self):
        e = Env(self)
        e.stamp("2026-09-30\n")
        for bad in ("abc", "-3", "1.5", "", "99999999999999999999"):
            self.assertEqual(e.run(interval=bad).stdout, "", bad)
        e.stamp("2026-09-01\n")
        self.assertEqual(e.run(interval="abc").stdout, LINE.format("2026-09-01"))

    def test_malformed_stamp_counts_as_never(self):
        e = Env(self)
        for bad in ("garbage\n", "", "2026-13-01\n", "2026-00-10\n", "2026-02-32\n", "2026-09-10\r\n", "20260910\n"):
            e.stamp(bad)
            r = e.run()
            self.assertEqual((r.stdout, r.stderr, r.returncode), (LINE.format("never"), "", 0), repr(bad))

    def test_future_stamp_is_not_due(self):
        e = Env(self)
        e.stamp("2027-01-01\n")
        self.assertEqual(e.run().stdout, "")

    def test_zero_padded_months_and_days_do_no_octal_arithmetic(self):
        e = Env(self, today="2026-09-09")
        e.stamp("2026-08-08\n")
        r = e.run()
        self.assertEqual((r.stdout, r.stderr), (LINE.format("2026-08-08"), ""))
        e.stamp("2026-09-08\n")
        self.assertEqual(e.run().stdout, "")

    def test_year_boundary(self):
        e = Env(self, today="2027-01-05")
        e.stamp("2026-12-30\n")
        self.assertEqual(e.run().stdout, "")
        e.stamp("2026-12-20\n")
        self.assertEqual(e.run().stdout, LINE.format("2026-12-20"))

    def test_subagent_and_compaction_are_silent(self):
        e = Env(self)
        self.assertEqual(e.run(payload='{"source":"startup","agent_id":"a1","agent_type":"x"}').stdout, "")
        self.assertEqual(e.run(payload='{"source":"compact"}').stdout, "")
        self.assertEqual(e.run(payload='{"source":"clear"}').stdout, LINE.format("never"))
        self.assertEqual(e.run(payload="").stdout, LINE.format("never"))

    def test_falls_back_to_home_when_no_plugin_data(self):
        e = Env(self)
        fallback = os.path.join(e.home, ".claude", "upkeep")
        os.makedirs(fallback)
        e.stamp("2026-09-30\n", directory=fallback)
        self.assertEqual(e.run(data=False).stdout, "")
        e.stamp("2026-08-01\n", directory=fallback)
        self.assertEqual(e.run(data=False).stdout, LINE.format("2026-08-01"))


class StampScript(unittest.TestCase):
    def test_writes_today_and_the_hook_then_goes_quiet(self):
        e = Env(self)
        target = os.path.join(e.tmp, "made", "dir")
        r = e.run(payload="", script=STAMP, args=(target,))
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(target, "last-upkeep")) as f:
            self.assertEqual(f.read(), "2026-10-02\n")
        e.data = target
        self.assertEqual(e.run().stdout, "")

    def test_without_argument_uses_the_hook_fallback_not_the_real_home(self):
        e = Env(self)
        r = e.run(payload="", script=STAMP, data=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(e.home, ".claude", "upkeep", "last-upkeep")))


if __name__ == "__main__":
    unittest.main()
