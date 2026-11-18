from enigram.domain.citations import check_citations

SHA = "abc1234567"
GOOD = f"src/a.ts:1-2@{SHA}"
BAD = f"src/gone.ts:1-2@{SHA}"


def _code(c):
    return None if c.file == "src/a.ts" else "no such file"


def check(answer, refs=(1,), notes=(7,), code=_code):
    return check_citations(answer, refs=set(refs), notes=set(notes), code_check=code)


def test_valid_citations_are_kept():
    answer = f"It checks the hash {GOOD}. The handbook agrees [1]. The team too note#7."

    assert check(answer) == (answer, [])


def test_unknown_references_are_removed():
    assert check("The handbook says so [9].") == (
        "The handbook says so.",
        [{"citation": "[9]", "reason": "no such source"}],
    )
    assert check("Both say so [1, 9].")[0] == "Both say so [1]."
    assert check("The handbook [9] says so [1].")[0] == "The handbook says so [1]."
    assert check("The list `items[9]` is indexed [1].")[0] == "The list `items[9]` is indexed [1]."


def test_invalid_code_citations_are_removed_with_their_wrapping():
    text, invalid = check(f"It reads the file `{BAD}`. And again ({BAD}). And {GOOD}.")

    assert text == f"It reads the file. And again. And {GOOD}."
    assert invalid == [{"citation": BAD, "reason": "no such file"}] * 2


def test_code_citations_are_left_alone_without_a_checker():
    answer = f"It reads the file {BAD}."

    assert check(answer, code=None) == (answer, [])


def test_unknown_notes_are_removed():
    assert check("The team says so note#99.") == (
        "The team says so.",
        [{"citation": "note#99", "reason": "note not shown"}],
    )


def test_references_section_and_code_fences_are_untouched():
    answer = "Fine [1].\n\n```ts\nconst x = [9]\n```\n\n### References\n\n- [9] Elsewhere\n"

    assert check(answer) == (answer, [])


def test_only_the_citation_and_its_own_wrapping_are_cut():
    assert check("call foo() [9] and bar().")[0] == "call foo() and bar()."
    assert check("Text `a  b` and [9]")[0] == "Text `a  b` and"
    assert check("The (see [9]) end.")[0] == "The (see) end."
    assert check("line one [9]  \nline two")[0] == "line one  \nline two"  # hard break kept
    assert check("note#5 says so; [1] agrees.")[0] == "says so; [1] agrees."
    assert check("    indented [9] text , kept")[0] == "    indented text , kept"


def test_a_fence_closes_only_with_its_own_marker():
    answer = "~~~\n[9]\n```\n[9]\n~~~\nAfter [9]."

    assert check(answer)[0] == "~~~\n[9]\n```\n[9]\n~~~\nAfter."


def test_a_bracketed_path_segment_is_not_a_reference():
    answer = f"It is the page src/[1]/page.tsx:1-2@{SHA}."

    assert check(answer, refs=(), code=lambda c: None) == (answer, [])
