"""Finding the file the person meant.

These commands normally run inside the etl container, where the workdir is
/app and the project's ./data directory is mounted at /data. So the
obvious thing to type - `data/inbox/budget.csv`, which is what the
Makefile's own help shows - points at nothing from /app.

The Makefile translates the path, and this is the safety net underneath
it: the same spelling works when plumb is invoked directly, and when
nothing can be found the error says why instead of a bare "no such file".
"""
from __future__ import annotations

from pathlib import Path

from plumb.cli import _input_file


def test_a_path_that_exists_is_used_as_given(tmp_path, capsys):
    target = tmp_path / "budget.csv"
    target.write_text("category,effective_from,amount\n")
    assert _input_file(str(target)) == target
    # Nothing to explain, so nothing is said.
    assert capsys.readouterr().out == ""


def test_a_relative_path_resolves_against_the_working_directory(
        tmp_path, monkeypatch):
    target = tmp_path / "budget.csv"
    target.write_text("x\n")
    monkeypatch.chdir(tmp_path)
    assert _input_file("budget.csv") == Path("budget.csv")


def test_a_host_relative_path_falls_back_to_the_mount(tmp_path, monkeypatch,
                                                      capsys):
    # The actual bug: from /app, "data/inbox/x.csv" is nothing, while
    # "/data/inbox/x.csv" is the mounted file. Simulated here with tmp_path
    # standing in for the mount root.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "budget.csv").write_text("x\n")

    elsewhere = tmp_path / "workdir"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    # tmp_path is absolute, so dropping its leading slash gives exactly the
    # shape of a host-relative path that only resolves from the root.
    relative = str(tmp_path / "inbox" / "budget.csv").lstrip("/")
    found = _input_file(relative)

    assert found == Path("/") / relative
    assert found.is_file()
    # And it says so, rather than silently reading a different file than
    # the one that was asked for.
    assert "mounted at /data" in capsys.readouterr().out


def test_a_missing_file_returns_none_and_explains(tmp_path, monkeypatch,
                                                  capsys):
    monkeypatch.chdir(tmp_path)
    assert _input_file("data/inbox/typo.csv") is None
    err = capsys.readouterr().err
    assert "no such file" in err


def test_the_hint_names_the_path_to_try(tmp_path, monkeypatch, capsys):
    # Only when there is a /data mount to point at - on a developer's own
    # machine the hint would be noise.
    monkeypatch.chdir(tmp_path)
    _input_file("data/inbox/typo.csv")
    err = capsys.readouterr().err
    if Path("/data").is_dir():
        assert "/data/inbox/typo.csv" in err
