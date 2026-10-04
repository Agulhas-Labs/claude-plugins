import glob
import os
import re
import unittest

HOOKS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")

# Claude Code compiles a JSX element to a call of `h` and a fragment to a use of `Fragment`, so any binding
# of either name in a .tsx/.jsx file (a variable, parameter, function or import) turns every element in the
# file into a call of that. The engine refuses the module; this pins it before a release does.
BINDING = re.compile(
    r"\b(?:const|let|var|function|class)\s+(?:h|Fragment)\b"
    r"|[(,]\s*(?:h|Fragment)\s*(?::[^,)=]+)?\s*[,)=]"
    r"|\b(?:h|Fragment)\s*=>"
    r"|\bimport\b[^;\n]*\b(?:h|Fragment)\b"
    r"|\{[^}]*\b(?:h|Fragment)\b[^}]*\}\s*=",
)


class ModNameTests(unittest.TestCase):
    def test_no_mod_source_binds_h_or_fragment(self):
        found = []
        for path in sorted(glob.glob(os.path.join(HOOKS, "*.tsx")) + glob.glob(os.path.join(HOOKS, "*.jsx"))):
            with open(path, encoding="utf-8") as source:
                for number, line in enumerate(source, 1):
                    if BINDING.search(line):
                        found.append(f"{os.path.basename(path)}:{number}: {line.strip()}")
        self.assertEqual(found, [])

    def test_the_pattern_catches_the_known_shapes(self):
        for bad in ("const h = 1", "let h", "function h(", "(h) => 1", "(a, h)", "x.map(h => h)", "import { h } from 'x'", "const { h } = x", "const Fragment = 2"):
            self.assertTrue(BINDING.search(bad), bad)
        for good in ("const hours = 1", "const m = 2", "const html = 3", "function hash()", "(host, path)", "const { Box, Text } = $.ui.resolve(e)"):
            self.assertFalse(BINDING.search(good), good)


if __name__ == "__main__":
    unittest.main()
