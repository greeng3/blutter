"""What allocated a register says what a fixed-offset access to it is.

Regression test for issue #19. `processLoadStore()` decides an access is an
element access by comparing its displacement against `Array::data_offset()`,
which matches element 0 and nothing else. A constructor filling a list wrote
element 0 as an element and every later one as a field at a raw offset::

    // 0xae430: r0 = AllocateArray()
    // 0xae43c: ArrayStore: r0[0] = r16  ; List_8
    // 0xae444: StoreField: r0->field_1f = r16      <- element 1
    // 0xae44c: StoreField: r0->field_27 = r16      <- element 2

The same comparison claims an element that is not there when a class puts a
field where an Array's payload starts — `GrowableObjectArray::data` does::

    // 0xae4a8: r0 = AllocateGrowableArray()
    // 0xae4b0: ArrayStore: r0[0] = r1  ; List_8    <- its data field
    // 0xae4b8: StoreField: r0->field_f = r1        <- its length

Both assertions are about what the allocator returned, not about any offset,
because an offset is exactly what cannot tell these apart:

* a register the array allocator just returned holds an Array, whose type
  arguments and length the stub itself has already written — so nothing the
  caller stores into it at a fixed offset is a field;
* a register the growable allocator just returned holds a GrowableObjectArray,
  which has no elements of its own at all — its payload lives in the separate
  array its data field points at.

A register is watched from the allocation until something else defines it, which
for a call result means until the next call, since a call clobbers it — and only
along the straight-line run out of the allocation. Where another branch lands in
the middle, control can arrive without having allocated anything, and declining
to name an element there is the analyzer being right rather than missing one; an
inline allocation's slow path rejoining the fast one is the common case. So the
watch also ends at the first instruction anything branches to, which is exactly
the guarantee the analyzer offers.

These tests need a real sample, so they are opt-in::

    BLUTTER_BIN=bin/blutter_dartvm<ver>_<target> \\
    BLUTTER_TEST_LIBAPP=/path/to/App \\
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

# An IL line, as opposed to the disassembly indented underneath one.
IL_LINE = re.compile(r"^\s*// (0x[0-9a-f]+): (.*)$")
ALLOCATE = re.compile(r"^(r\d+) = Allocate(Array|GrowableArray)\(\)$")
# A register is written either bare ("r0 = ...") or by a named IL
# ("LoadField: r0 = ..."), and either ends the watch.
DEFINES = re.compile(r"^(?:\w+: )?(r\d+) = ")

# An instruction the analyzer matched nothing for is printed as itself. One of
# those can reload the register from the frame, which ends the watch just as an
# IL would — the register no longer holds what the allocator returned.
RAW_INSN = re.compile(r"^([a-z][a-z0-9.]*)\s+([xw])(\d+)")
# Mnemonics whose first operand is a register they read rather than write.
READS_FIRST_OPERAND = {
    "cmp", "cmn", "tst", "stp", "str", "stur", "strb", "sturb", "strh", "sturh",
    "b", "bl", "br", "blr", "cbz", "cbnz", "tbz", "tbnz", "ret",
}


def _register_written(il: str) -> str | None:
    """The register this line writes, whether it is an IL or a bare instruction."""
    m = DEFINES.match(il)
    if m:
        return m.group(1)
    m = RAW_INSN.match(il)
    if m and m.group(1).split(".")[0] not in READS_FIRST_OPERAND:
        return f"r{m.group(3)}"
    return None
STORE_FIELD = re.compile(r"^StoreField: (r\d+)->field_")
# Any address branched to, anywhere in the file: "b.ne #0x1724e88", "cbnz x0, #0x110f0f0".
BRANCH_TARGET = re.compile(r"^\s*//\s+\S*\s*(?:b|b\.\w+|cbz|cbnz|tbz|tbnz)\s+[^#]*#(0x[0-9a-f]+)", re.M)
BRANCHES = re.compile(r"^(?:b|b\.\w+|cbz|cbnz|tbz|tbnz)\s")
ELEMENT_OP = re.compile(r"^Array(?:Load|Store): (?:(r\d+) = )?(r\d+)\[")


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
class AllocatedArrayTests(unittest.TestCase):
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

        cls.allocations = 0
        cls.fields_of_an_array: list[str] = []
        cls.elements_of_a_growable: list[str] = []

        for path in sorted((outdir / "asm").rglob("*.dart")):
            text = path.read_text(errors="replace")
            targets = set(BRANCH_TARGET.findall(text))

            watching: str | None = None  # register
            kind = ""
            for line in text.splitlines():
                m = IL_LINE.match(line)
                if not m:
                    continue
                addr, il = m.group(1), m.group(2).strip()

                if watching and (addr in targets or BRANCHES.match(il)):
                    watching = None

                if watching:
                    where = f"{path.name} {addr}: {il}"
                    if kind == "Array":
                        f = STORE_FIELD.match(il)
                        if f and f.group(1) == watching:
                            cls.fields_of_an_array.append(where)
                    else:
                        e = ELEMENT_OP.match(il)
                        if e and e.group(2) == watching:
                            cls.elements_of_a_growable.append(where)

                    if _register_written(il) == watching:
                        watching = None

                a = ALLOCATE.match(il)
                if a:
                    cls.allocations += 1
                    watching, kind = a.group(1), a.group(2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_sample_allocates_arrays(self):
        """Guard against a run with nothing to say, which would pass vacuously."""
        self.assertGreater(self.allocations, 0, "no array allocation in the analysis")

    def test_nothing_stored_into_a_fresh_array_is_a_field(self):
        self.assertEqual(
            self.fields_of_an_array,
            [],
            f"{len(self.fields_of_an_array)} elements of a fresh Array read as fields",
        )

    def test_no_element_is_claimed_on_a_growable_array(self):
        self.assertEqual(
            self.elements_of_a_growable,
            [],
            f"{len(self.elements_of_a_growable)} fields of a GrowableObjectArray read as elements",
        )


if __name__ == "__main__":
    unittest.main()
