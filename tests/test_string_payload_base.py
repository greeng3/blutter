"""No array access may be given a negative element index.

Regression test for issue #8. `processLoadStore()` turns a fixed-offset element
access into an index by subtracting where the payload starts, and it knew only
two payload bases: an Array's and a TypedData's. Anything else came out as an
index below zero, which no real element access has::

    // 0x1724e78: ArrayLoad: r5 = r3[-7]  ; TypedUnsigned_1
    //     0x1724e78: ldrb            w5, [x3, #0x10]

Immich 3.1.0 (Dart 3.12.2, android arm64) had 11 of these, in two kinds:

* a character read out of a String, whose data starts before a TypedData
  payload does — `path/src/style/windows.dart` comparing a drive letter's
  colon is one, guarded by a `cmp w2, #0xbc` class id check for OneByteString;

* a byte read through the `data_` pointer of a TypedDataBase, already loaded
  into a register by the preceding instruction. The base register holds the
  payload itself there, so the displacement *is* the index.

The invariant asserted is the one that needs no knowledge of any offset: an
index is a count of elements from the start of the payload, so it is never
negative. It holds for every array kind, which is the point — the defect was
per-kind knowledge applied to a kind it did not know about.

These tests need a real sample, so they are opt-in::

    BLUTTER_BIN=bin/blutter_dartvm<ver>_android_arm64 \\
    BLUTTER_TEST_LIBAPP=/path/to/libapp.so \\
    python -m unittest discover -s tests

Standard library only.
"""

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

BLUTTER_BIN = os.environ.get("BLUTTER_BIN")
SAMPLE = os.environ.get("BLUTTER_TEST_LIBAPP")

# // 0x1724e78: ArrayLoad: r5 = r3[-7]  ; TypedUnsigned_1
ARRAY_OP_INDEX = re.compile(r"Array(?:Load|Store): [^;]*\[(-?\d+)\][^;]*;\s(\w+)_\d+")


def _requirements_met() -> bool:
    return bool(
        BLUTTER_BIN
        and pathlib.Path(BLUTTER_BIN).is_file()
        and SAMPLE
        and pathlib.Path(SAMPLE).is_file()
    )


@unittest.skipUnless(
    _requirements_met(), "set BLUTTER_BIN and BLUTTER_TEST_LIBAPP to a sample"
)
class PayloadBaseTests(unittest.TestCase):
    """One analysis run shared by every assertion — it is the expensive part."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        outdir = pathlib.Path(cls._tmp.name) / "out"
        result = subprocess.run(
            [BLUTTER_BIN, "-i", SAMPLE, "-o", str(outdir)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f"analysis failed: {result.stderr[-2000:]}")

        cls.indices: list[tuple[int, str, str]] = []
        for path in (outdir / "asm").rglob("*.dart"):
            for line in path.read_text(errors="replace").splitlines():
                m = ARRAY_OP_INDEX.search(line)
                if m:
                    cls.indices.append((int(m.group(1)), m.group(2), line.strip()))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_array_accesses_were_found(self):
        """Guard against a run whose output holds none, which would pass vacuously."""
        self.assertTrue(self.indices, "no array access in the whole analysis")

    def test_no_array_access_has_a_negative_index(self):
        negative = [line for index, _, line in self.indices if index < 0]
        self.assertEqual(
            negative,
            [],
            f"{len(negative)} element accesses indexed from the wrong payload base",
        )


if __name__ == "__main__":
    unittest.main()
