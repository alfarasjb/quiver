import pytest
from pydantic import ValidationError

from quiver.store import Entry, Workstream, add_entry, get_entry, get_workstream, home, save_workstream, supersede


def test_round_trip_and_supersede():
    # "---" inside values must not be mistaken for the frontmatter delimiter.
    ws = Workstream(name="abc-533", branch="jay/abc-533-foo", status="in_progress", next_step="see ---\n---\nthen merge")
    save_workstream("acme", ws)
    assert get_workstream("acme", "abc-533") == ws

    old = Entry(kind="decision", title="Ship the stack as is", body="Follow-ups after merge.\n---\nmore", decided_by="Jay")
    add_entry("acme", old)
    assert get_entry("acme", old.id) == old

    new = supersede("acme", old.id, Entry(kind="decision", title="Follow-ups stack on #42"))
    assert get_entry("acme", new.id).supersedes == old.id
    closed = get_entry("acme", old.id)
    assert closed.status == "superseded" and closed.closed_at is not None
    assert not list(home().rglob("*.tmp"))

    # Superseding a closed or missing entry fails before writing a successor.
    with pytest.raises(ValueError):
        supersede("acme", old.id, Entry(kind="decision", title="again"))
    with pytest.raises(KeyError):
        supersede("acme", "no-such-id", Entry(kind="decision", title="orphan"))
    assert len(list((home() / "acme" / "entries").iterdir())) == 2


@pytest.mark.parametrize("fields", [{"name": "x", "status": "bogus"}, {"name": "ABC 533"}])
def test_rejects_invalid_workstream(fields):
    with pytest.raises(ValidationError):
        Workstream(**fields)
