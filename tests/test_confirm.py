"""The destructive-action prompt.

The bug this pins: delete_synced.py and dedup_synced.py both accepted "d" and
then fell through to the delete branch, because "d" is neither "cancel" nor
"dry-run". "d" is the first letter of the safe option as readily as the
destructive one, so the only correct answer is to refuse it.
"""
import pytest

import dedup_synced as dedup_mod
import delete_synced as delete_mod
from src import confirm


@pytest.mark.parametrize("answer", ["d", "de", "dr", "D", " d "])
def test_an_abbreviation_of_delete_is_refused(answer):
    assert confirm.interpret(answer) is None


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("delete", confirm.DELETE),
        ("DELETE", confirm.DELETE),
        ("  delete  ", confirm.DELETE),
        ("dry-run", confirm.DRY_RUN),
        ("dry", confirm.DRY_RUN),
        ("dryrun", confirm.DRY_RUN),
        ("cancel", confirm.CANCEL),
        ("c", confirm.CANCEL),
        ("n", confirm.CANCEL),
    ],
)
def test_unambiguous_answers_resolve(answer, expected):
    assert confirm.interpret(answer) == expected


@pytest.mark.parametrize("answer", ["", "yes", "y", "del", "rm", "dry run"])
def test_anything_else_is_refused(answer):
    assert confirm.interpret(answer) is None


def test_only_the_full_word_reaches_the_destructive_branch():
    """The property that matters, stated once: nothing but "delete" deletes."""
    deleting = [
        a for a in ["d", "de", "del", "dr", "dry", "dry-run", "dryrun",
                    "cancel", "c", "n", "y", "yes", ""]
        if confirm.interpret(a) == confirm.DELETE
    ]
    assert deleting == []


def test_ask_reprompts_until_unambiguous():
    answers = iter(["d", "dry"])
    complaints = []

    result = confirm.ask(
        "Delete?", input_fn=lambda _: next(answers), print_fn=complaints.append
    )

    assert result == confirm.DRY_RUN
    assert len(complaints) == 1
    assert "'d' is not accepted" in complaints[0]


def test_both_scripts_use_the_shared_prompt():
    """Neither script may grow its own copy of the accept list again.

    They had identical copies, and the fix had to be applied twice, which is
    the shape every other duplicate in this repo failed in.
    """
    assert dedup_mod.confirm is confirm
    assert delete_mod.confirm is confirm
