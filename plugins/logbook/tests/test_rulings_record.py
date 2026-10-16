"""Tests for what the rulings view records: decision groups and whose call each was, `revise`, `summary`,
and deliverable images copied into the board.

Run: python3 -m unittest discover -s plugins/logbook/tests

Each command runs end to end through `board.main(argv)`, with the clock, the environment, the current
directory and the template controlled by the test. Nothing here runs git: the project is not a
repository.
"""
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from unittest import mock

from test_board import NOW, SESSION, Workspace, at, event

import board  # noqa: E402  (test_board puts the board folder on the path)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


class Rulings(Workspace):
    def setUp(self):
        super().setUp()
        self.minutes = 0
        patches = (
            mock.patch.object(board, "DEFAULT_TEMPLATE", self.template),
            mock.patch.object(board, "clock", self.tick),
            mock.patch.dict(os.environ, {board.SESSION_VARIABLE: SESSION}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in [n for n in os.environ if n.startswith("LOGBOOK_")]:
            self.addCleanup(os.environ.__setitem__, name, os.environ.pop(name))
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.project)
        self.board = self.start()
        self.outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(self.outside)

    def tick(self):
        self.minutes += 1
        return at(self.minutes)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = board.main(list(argv))
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_main(*argv)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def refused(self, *argv):
        before = len(board.events(self.board))
        code, out, err = self.run_main(*argv)
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(err.count("\n"), 1, err)
        self.assertEqual(len(board.events(self.board)), before, "nothing is appended")
        return err

    def state(self):
        return board.read_state(self.board)

    def last_time(self):
        return board.events(self.board)[-1]["t"]

    def file(self, name, data=PNG):
        path = os.path.join(self.outside, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def outside_files(self):
        return sorted(os.listdir(self.outside))

    # --- G1: decision fields --------------------------------------------------------------------------

    def test_a_decision_takes_a_group_and_whose_call_it_was(self):
        self.assertEqual(self.ok("decision", "One file per export.", "--group", "Export format", "--yours"),
                         "D1 recorded\n")
        self.assertEqual(self.ok("decision", "Dates in ISO 8601."), "D2 recorded\n")
        first, second = self.state()["decisions"]
        self.assertEqual((first["group"], first["yours"], first["revised"]), ("Export format", True, None))
        self.assertEqual((second["group"], second["yours"], second["revised"]), (None, False, None))

    # --- G2: revise ---------------------------------------------------------------------------------

    def test_revise_changes_only_the_fields_given_and_keeps_id_and_place(self):
        self.ok("decision", "One file.", "--why", "Simpler.", "--reverse", "Split it.", "--group", "Format")
        self.ok("decision", "Dates in ISO 8601.")
        self.assertEqual(self.ok("revise", "d1", "One file per export."), "D1 revised\n")
        revised = self.last_time()
        first, second = self.state()["decisions"]
        self.assertEqual(
            {k: first[k] for k in ("id", "text", "why", "reverse", "group", "yours", "revised")},
            {"id": "D1", "text": "One file per export.", "why": "Simpler.", "reverse": "Split it.",
             "group": "Format", "yours": False, "revised": revised},
        )
        self.assertEqual(first["time"], board.events(self.board)[1]["t"], "the time it was taken stays")
        self.assertEqual((second["id"], second["text"], second["revised"]), ("D2", "Dates in ISO 8601.", None))

        self.assertEqual(self.ok("revise", "2", "--why", "Programs read it.", "--group", "Format", "--yours"),
                         "D2 revised\n")
        second = self.state()["decisions"][1]
        self.assertEqual((second["text"], second["why"], second["reverse"], second["group"], second["yours"]),
                         ("Dates in ISO 8601.", "Programs read it.", None, "Format", True))
        self.assertEqual(second["revised"], self.last_time())

        self.assertEqual(self.ok("revise", "D2", "--not-yours"), "D2 revised\n")
        second = self.state()["decisions"][1]
        self.assertEqual((second["yours"], second["why"]), (False, "Programs read it."))
        self.assertEqual([d["id"] for d in self.state()["decisions"]], ["D1", "D2"])

    def test_revising_no_decision_is_refused_and_appends_nothing(self):
        self.assertIn("no decision D1", self.refused("revise", "D1", "Text."))
        self.ok("decision", "Dates in ISO 8601.")
        self.refused("revise", "D2", "Text.")
        self.refused("revise", "Q1", "Text.")
        self.assertIsNone(self.state()["decisions"][0]["revised"])

    def test_a_revise_that_changes_nothing_is_refused(self):
        self.ok("decision", "Dates in ISO 8601.")
        self.assertIn("nothing to revise", self.refused("revise", "D1"))

    def test_yours_and_not_yours_together_are_refused(self):
        self.ok("decision", "Dates in ISO 8601.")
        before = len(board.events(self.board))
        code, _, _ = self.run_main("revise", "D1", "--yours", "--not-yours")
        self.assertEqual(code, 2)
        self.assertEqual(len(board.events(self.board)), before)

    def test_a_revise_in_the_log_naming_no_decision_changes_nothing(self):
        log = [
            event("start", 0, session=SESSION, title="t"),
            event("decision", 1, text="Dates in ISO 8601."),
            event("revise", 2, id="D9", text="Something else."),
            event("revise", 3, id="D1"),
            event("revise", 4, id="D1", yours="yes", text=""),
        ]
        decision, = board.derive(log, [], board.settings({}))["decisions"]
        self.assertEqual((decision["text"], decision["yours"], decision["revised"]), ("Dates in ISO 8601.", False, None))

    # --- G3: summary --------------------------------------------------------------------------------

    def test_a_summary_is_set_then_replaced_whole(self):
        self.assertIsNone(self.state()["summary"])
        self.assertEqual(self.ok("summary", "The export works.", "--facts", "3 of 3 checks pass."), "summary set\n")
        self.assertEqual(self.state()["summary"],
                         {"text": "The export works.", "facts": "3 of 3 checks pass.", "time": self.last_time()})
        self.ok("summary", "The export works and is merged.")
        self.assertEqual(self.state()["summary"],
                         {"text": "The export works and is merged.", "facts": None, "time": self.last_time()})

    # --- G4: images ---------------------------------------------------------------------------------

    def test_an_image_deliverable_is_copied_into_the_board(self):
        source = self.file("screen-light.png")
        self.assertEqual(self.ok("deliverable", "Light screen", "--path", source), "deliverable recorded\n")
        self.ok("deliverable", "Notes", "--path", self.file("notes.txt", b"text\n"))
        self.ok("deliverable", "Dark screen", "--path", self.file("Screen-Dark.JPEG"))
        self.ok("deliverable", "The pull request", "--url", "https://example.com/pr/1")
        light, notes, dark, link = self.state()["deliverables"]
        self.assertEqual((light["image"], light["path"]), ("images/1-screen-light.png", source))
        self.assertEqual(notes["image"], None)
        self.assertEqual(dark["image"], "images/3-Screen-Dark.JPEG")
        self.assertEqual(link["image"], None)
        for item in (light, dark):
            with open(os.path.join(self.board, item["image"]), "rb") as f:
                self.assertEqual(f.read(), PNG)
        self.assertEqual(sorted(os.listdir(os.path.join(self.board, "images"))),
                         ["1-screen-light.png", "3-Screen-Dark.JPEG"])

    def test_every_image_extension_is_copied(self):
        for number, extension in enumerate((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"), 1):
            self.ok("deliverable", extension, "--path", self.file("shot" + extension))
            self.assertEqual(self.state()["deliverables"][-1]["image"], f"images/{number}-shot{extension}")

    def test_what_is_not_a_small_image_file_records_without_one(self):
        os.mkdir(os.path.join(self.outside, "folder.png"))
        with mock.patch.object(board, "IMAGE_CAP_BYTES", len(PNG)):
            self.ok("deliverable", "At the cap", "--path", self.file("fits.png"))
            self.ok("deliverable", "Over the cap", "--path", self.file("big.png", PNG + b"x"))
        self.ok("deliverable", "Missing", "--path", os.path.join(self.outside, "missing.png"))
        self.ok("deliverable", "A folder", "--path", os.path.join(self.outside, "folder.png"))
        self.ok("deliverable", "Text", "--path", self.file("notes.txt"))
        fits, big, missing, folder, notes = self.state()["deliverables"]
        self.assertEqual(fits["image"], "images/1-fits.png")
        self.assertEqual([d["image"] for d in (big, missing, folder, notes)], [None] * 4)
        self.assertEqual(missing["path"], os.path.join(self.outside, "missing.png"))
        self.assertEqual(os.listdir(os.path.join(self.board, "images")), ["1-fits.png"])

    def test_a_file_over_ten_megabytes_records_without_an_image(self):
        big = os.path.join(self.outside, "big.png")
        with open(big, "wb") as f:
            f.truncate(10 * 1024 * 1024 + 1)
        self.ok("deliverable", "Big", "--path", big)
        self.assertIsNone(self.state()["deliverables"][0]["image"])

    def test_an_images_folder_that_is_a_link_is_not_written_through(self):
        os.symlink(self.outside, os.path.join(self.board, "images"))
        self.ok("deliverable", "Screen", "--path", self.file("screen.png"))
        self.assertIsNone(self.state()["deliverables"][0]["image"])
        self.assertEqual(self.outside_files(), ["screen.png"])

    def test_a_link_at_the_copy_name_is_replaced_not_followed(self):
        target = self.file("victim.png", b"kept\n")
        images = os.path.join(self.board, "images")
        os.mkdir(images)
        os.symlink(target, os.path.join(images, "1-screen.png"))
        self.ok("deliverable", "Screen", "--path", self.file("screen.png"))
        self.assertEqual(self.state()["deliverables"][0]["image"], "images/1-screen.png")
        self.assertTrue(board.is_regular(os.path.join(images, "1-screen.png")))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"kept\n")
        self.assertEqual(self.outside_files(), ["screen.png", "victim.png"])

    def test_an_image_in_the_log_that_leaves_the_images_folder_is_dropped(self):
        log = [event("start", 0, session=SESSION, title="t")] + [
            event("deliverable", 1, label="x", path="x.png", image=image)
            for image in ("../../etc/x.png", "/tmp/x.png", "images/../x.png", "images/a/b.png", "images/..", 5,
                          "images/1-x.png")
        ]
        images = [d["image"] for d in board.derive(log, [], board.settings({}))["deliverables"]]
        self.assertEqual(images, [None] * 6 + ["images/1-x.png"])

    def test_the_report_keeps_the_image_path_beside_it(self):
        self.ok("deliverable", "Screen", "--path", self.file("screen.png"))
        self.ok("close")
        self.assertIn("images/1-screen.png", self.read(os.path.join(self.board, board.REPORT_FILE)))
        self.assertTrue(os.path.isfile(os.path.join(self.board, "images", "1-screen.png")))

    # --- pruning takes the images with the board ---------------------------------------------------

    def test_pruning_removes_a_board_with_its_images(self):
        self.ok("deliverable", "Screen", "--path", self.file("screen.png"))
        later = NOW + timedelta(days=board.DEFAULT_RETENTION_DAYS + 1)
        self.assertEqual(board.prune(self.project, later, env={}), [self.board])
        self.assertFalse(os.path.lexists(self.board))
        self.assertEqual(self.outside_files(), ["screen.png"])

    def test_pruning_follows_no_images_link(self):
        os.symlink(self.outside, os.path.join(self.board, "images"))
        kept = self.file("kept.png")
        later = NOW + timedelta(days=board.DEFAULT_RETENTION_DAYS + 1)
        self.assertEqual(board.prune(self.project, later, env={}), [])
        self.assertTrue(os.path.isfile(kept))


class OldLogs(unittest.TestCase):
    """G5: a log written before these fields existed derives with their defaults."""

    def test_old_decisions_and_deliverables_derive_with_the_defaults(self):
        log = [
            event("start", 0, session=SESSION, title="t"),
            event("decision", 1, text="Dates in ISO 8601.", why="Programs read it.", reverse="Change it."),
            event("deliverable", 2, label="The exporter", path="src/export.py", step="1"),
            event("deliverable", 3, label="A screenshot", path="shots/screen.png"),
        ]
        state = board.derive(log, [], board.settings({}))
        decision, = state["decisions"]
        self.assertEqual(
            decision,
            {"id": "D1", "text": "Dates in ISO 8601.", "why": "Programs read it.", "reverse": "Change it.",
             "time": board.utc(at(1)), "group": None, "yours": False, "revised": None},
        )
        self.assertEqual([d["image"] for d in state["deliverables"]], [None, None])
        self.assertIsNone(state["summary"])


if __name__ == "__main__":
    unittest.main()
