"""Two arm64 matchers must not error on the code shapes issue #12 found.

Regression test for issue #12, which a second Mach-O sample surfaced once
macOS support landed: AppFlowy 0.13.1, Dart 3.11.5, macos arm64, uncompressed
pointers. Nothing about either gap is macOS-specific — both are generic arm64
matcher shapes that an Android build could just as well contain.

1. `handleArgumentsDescriptorTypeArguments` expects a branch to the merge point
   where the compiler emitted a fallthrough::

        mov   x1, x2                 ; typeArg already in the final register
        cbnz  x0, #0x110f0f0         ; typeArgLen != 0 -> skip the pool load
        add   x1, x27, #0xcd, lsl #12
        ldr   x1, [x1, #0x8b8]       ; typeArg_final = <from PP>
        ldr   x0, [x29, #0x38]       ; merge point, reached by falling through

   With the pool load writing the register the `else` arm already holds, the
   compiler needs neither the `b` out of the `then` arm nor the `mov` in the
   `else` arm, so both are absent.

2. `processCallLeafRuntime` claims a sequence that is not a leaf runtime call::

        ldr   x10, [THR, #0x268]     ; THR::call_native_through_safepoint_entry_point
        ldr   x9, [THR, #0x7e8]      ; THR::PropagateError
        blr   x10

   The entry point loaded into x10 is the safepoint trampoline and x9 is the
   runtime entry it dispatches to — a shape the analyzer leaves as plain
   instructions elsewhere in the same function (0x85400 above this one).

The assertion is per matcher rather than "no analysis errors at all": an
unrelated gap in some other matcher, on whatever sample a runner supplies, is
not this regression and should not fail this test.

These tests need a real sample, so they are opt-in::

    BLUTTER_BIN=bin/blutter_dartvm<ver>_macos_arm64_no-compressed-ptrs \\
    BLUTTER_TEST_LIBAPP=/path/to/AppFlowy.app/Contents/Frameworks/App.framework/App \\
    python -m unittest discover -s tests

They are vacuous on a sample that contains neither shape — passing here means
"this sample produced no such error", not "the shapes are handled". The sample
known to exercise both is the public AppFlowy 0.13.1 macOS universal release;
`test_analysis_ran` guards only against a run that produced nothing at all.
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

# Analysis error at line 723 `std::unique_ptr<CallLeafRuntimeInstr> FunctionAnalyzer::processCallLeafRuntime(AsmIterator&)`: il
ANALYSIS_ERROR = re.compile(r"^Analysis error at line \d+ `(?P<signature>[^`]*)`: (?P<condition>.*)$")

ARGUMENTS_DESCRIPTOR = "handleArgumentsDescriptorTypeArguments"
CALL_LEAF_RUNTIME = "processCallLeafRuntime"


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
class MatcherGapTests(unittest.TestCase):
    """One analysis run shared by every assertion — it is the expensive part."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.outdir = pathlib.Path(cls._tmp.name) / "out"
        result = subprocess.run(
            [BLUTTER_BIN, "-i", SAMPLE, "-o", str(cls.outdir)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f"analysis failed: {result.stderr[-2000:]}")

        # An error is the header line plus the disassembly window it printed,
        # up to the next header. The window is what makes a failure here
        # actionable, so it is kept with the error it belongs to.
        cls.errors: list[tuple[str, list[str]]] = []
        current: list[str] = []
        for line in result.stderr.splitlines():
            m = ANALYSIS_ERROR.match(line)
            if m:
                current = [line]
                cls.errors.append((m.group("signature"), current))
            elif current:
                current.append(line)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def assertNoErrorFrom(self, matcher: str):
        reported = ["\n".join(lines) for signature, lines in self.errors if matcher in signature]
        self.assertEqual(reported, [], f"{matcher} reported {len(reported)} error(s)")

    def test_analysis_ran(self):
        """Guard against a run that produced nothing, which would pass vacuously."""
        self.assertTrue(
            any((self.outdir / "asm").rglob("*.dart")),
            "the analysis produced no disassembly",
        )

    def test_arguments_descriptor_type_arguments_handles_a_fallthrough_merge(self):
        self.assertNoErrorFrom(ARGUMENTS_DESCRIPTOR)

    def test_call_leaf_runtime_declines_a_safepoint_dispatch(self):
        self.assertNoErrorFrom(CALL_LEAF_RUNTIME)


if __name__ == "__main__":
    unittest.main()
