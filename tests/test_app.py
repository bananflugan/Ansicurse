from pathlib import Path

import pytest

pytest.importorskip("textual")

from ansicurse.app import AnsicurseApp  # noqa: E402
from ansicurse.playbooks import EntryKind  # noqa: E402
from ansicurse.widgets.dialogs import RunDialog  # noqa: E402

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("ANSIBLE_CONFIG", raising=False)


async def test_browses_and_previews_playbooks():
    app = AnsicurseApp(EXAMPLES)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        browser = app.browser
        assert [e.name for e in browser._visible][:4] == ["..", "group_vars", "roles", "tasks"]
        playbooks = [e.name for e in browser._visible if e.kind is EntryKind.PLAYBOOK]
        assert playbooks == ["hello.yml", "site.yml", "web.yml"]

        # Walk down to hello.yml and check the detail pane follows.
        while app.current_entry is None or app.current_entry.name != "hello.yml":
            await pilot.press("down")
        assert app.detail.entry.name == "hello.yml"

        # Into a folder and back up again.
        browser.load(EXAMPLES / "tasks")
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert browser.directory == EXAMPLES
        assert browser.current.name == "tasks"


async def test_run_dialog_builds_command():
    app = AnsicurseApp(EXAMPLES)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        while app.current_entry is None or app.current_entry.name != "hello.yml":
            await pilot.press("down")
        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, RunDialog)
        await pilot.press(*"web01")
        assert app.screen.options().limit == "web01"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RunDialog)


def _become_project(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "ansible.cfg").write_text("[defaults]\ninventory = hosts\n")
    (project_dir / "hosts").write_text("localhost ansible_connection=local\n")
    (project_dir / "site.yml").write_text(
        "- hosts: all\n  become: true\n  tasks:\n    - ansible.builtin.ping:\n"
    )
    return project_dir


async def _open_run_dialog(app, pilot):
    for _ in range(10):
        if app.current_entry is not None and app.current_entry.name == "site.yml":
            break
        await pilot.press("down")
    await pilot.press("f5")
    await pilot.pause()


async def test_become_password_is_asked_once_and_kept_in_memory(tmp_path, monkeypatch):
    import os

    from textual.widgets import Checkbox, Input

    from ansicurse.widgets.dialogs import PasswordDialog

    app = AnsicurseApp(_become_project(tmp_path))
    launched = []
    monkeypatch.setattr(app, "_launch", launched.append)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await _open_run_dialog(app, pilot)
        assert isinstance(app.screen, RunDialog)
        assert app.screen.query_one("#ask_become_pass", Checkbox).value is True
        assert "--ask-become-pass" in str(app.screen.query_one("#cmd-preview").render())

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, PasswordDialog)
        assert app.screen.query_one("#password", Input).password  # masked
        await pilot.press(*"s3cret", "enter")
        await pilot.pause()
        assert len(launched) == 1
        assert launched[0]._passwords == {"become": "s3cret"}
        assert "--ask-become-pass" in launched[0].command

        # Same session: no second prompt.
        await _open_run_dialog(app, pilot)
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert not isinstance(app.screen, PasswordDialog)
        assert len(launched) == 2 and launched[1]._passwords == {"become": "s3cret"}

        # The remembered options say "ask", but never contain the password.
        options_file = os.path.join(os.environ["XDG_STATE_HOME"], "ansicurse", "options.json")
        saved = open(options_file).read()
        assert '"ask_become_pass": true' in saved and "s3cret" not in saved

        # F9 forgets it; cancelling the prompt cancels the run.
        await pilot.press("f9")
        await _open_run_dialog(app, pilot)
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, PasswordDialog)
        await pilot.press("escape")
        await pilot.pause()
        assert len(launched) == 2


async def test_rejected_password_is_forgotten(tmp_path):
    from ansicurse.runner import Job, RunOptions
    from ansicurse.widgets.jobs import JobUpdated

    project_dir = _become_project(tmp_path)
    app = AnsicurseApp(project_dir)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app._passwords[("become", str(project_dir))] = "wrong"
        job = Job(project_dir / "site.yml", RunOptions(ask_become_pass=True), project_dir,
                  passwords={"become": "wrong"})
        job.status = "failed"
        job.password_errors.add("become")
        app.post_message(JobUpdated(job))
        await pilot.pause()
        assert app._passwords == {}
