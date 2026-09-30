"""The destructive-action prompt the root utility scripts share.

Lives in src/ for the same reason env_bootstrap.py does: it is only used by the
repo-root scripts, but it has to be importable by the test suite, and having
one copy is the whole point.

The rule it exists to enforce: an abbreviation may never resolve to the
destructive branch. delete_synced.py and dedup_synced.py both accepted "d" and
ran the delete, because "d" is not "cancel" and is not "dry-run", so it fell
through to the else. Someone shortening "dry-run" -- the safe option, and the
one whose name starts with d -- deleted every synced asset for a user instead.
"""

CANCEL = "cancel"
DRY_RUN = "dry-run"
DELETE = "delete"

# Only the two safe actions get abbreviations, and "d" is deliberately not one
# of them: it is the first letter of both "dry-run" and "delete", so there is no
# reading of it that is safe to guess at.
_ALIASES = {
    "c": CANCEL,
    "n": CANCEL,
    "dry": DRY_RUN,
    "dryrun": DRY_RUN,
}

_AMBIGUOUS = {"d", "de", "dr"}


def interpret(answer: str) -> str | None:
    """Map a typed answer to cancel/dry-run/delete, or None if it is not one.

    None means "ask again". The caller prints the retry message, so this stays
    a pure function the tests can enumerate.
    """
    answer = answer.strip().lower()
    if answer in _AMBIGUOUS:
        return None
    if answer in _ALIASES:
        return _ALIASES[answer]
    if answer in (CANCEL, DRY_RUN, DELETE):
        return answer
    return None


def ask(question: str, *, input_fn=input, print_fn=print) -> str:
    """Prompt until the answer is unambiguous. Returns cancel/dry-run/delete.

    input_fn and print_fn are injected so the tests can drive this without a
    terminal.
    """
    while True:
        action = interpret(input_fn(f"{question} [dry-run / delete / cancel]: "))
        if action is not None:
            return action
        print_fn(
            "Please enter 'dry-run', 'delete', or 'cancel' in full. "
            "'d' is not accepted: it starts both 'dry-run' and 'delete'."
        )
