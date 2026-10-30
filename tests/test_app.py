from pathlib import Path

import pytest

pytest.importorskip("textual")

from ansicurse.app import AnsicurseApp  # noqa: E402
from ansicurse.playbooks import EntryKind  # noqa: E402
from ansicurse.widgets.dialogs import RunDialog  # noqa: E402

EXAMPLES = Path(__file__).parent.parent / "examples"


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


@pytest.mark.parametrize("size", [(80, 24), (110, 32)])
async def test_run_dialog_buttons_stay_visible_on_short_terminals(size):
    app = AnsicurseApp(EXAMPLES)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        while app.current_entry is None or app.current_entry.name != "hello.yml":
            await pilot.press("down")
        await pilot.press("f5")
        await pilot.pause()
        dialog = app.screen.query_one("#dialog")
        assert dialog.max_scroll_y > 0  # the form itself has to scroll
        for button in ("#run", "#cancel"):
            assert dialog.region.contains_region(app.screen.query_one(button).region)


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
        saved = (Path.home() / ".local" / "ansicurse" / "options.json").read_text()
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


async def test_options_saved_before_password_support_still_ask(tmp_path):
    from textual.widgets import Checkbox, Input

    project_dir = _become_project(tmp_path)
    app = AnsicurseApp(project_dir)
    # Saved by a version without the ask_* options.
    app.saved_options[str(project_dir / "site.yml")] = {"limit": "web01", "become": True}
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await _open_run_dialog(app, pilot)
        assert app.screen.query_one("#limit", Input).value == "web01"
        assert app.screen.query_one("#ask_become_pass", Checkbox).value is True


async def test_ticking_become_asks_for_the_password(tmp_path):
    from textual.widgets import Checkbox

    project_dir = _become_project(tmp_path)
    (project_dir / "site.yml").write_text("- hosts: all\n  tasks:\n    - ansible.builtin.ping:\n")
    app = AnsicurseApp(project_dir)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await _open_run_dialog(app, pilot)
        ask = app.screen.query_one("#ask_become_pass", Checkbox)
        assert ask.value is False  # the playbook doesn't use become
        app.screen.query_one("#become", Checkbox).value = True
        await pilot.pause()
        assert ask.value is True


async def test_missing_become_password_ticks_the_ask_for_next_time(tmp_path):
    from textual.widgets import Checkbox

    from ansicurse.runner import Job, RunOptions
    from ansicurse.widgets.jobs import JobUpdated

    project_dir = _become_project(tmp_path)
    app = AnsicurseApp(project_dir)
    playbook = project_dir / "site.yml"
    app.saved_options[str(playbook)] = {"ask_become_pass": False}
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        job = Job(playbook, RunOptions(), project_dir)
        job.handle_event({"event": "runner_on_failed", "stdout": "Missing sudo password"})
        job.status = "failed"
        app.post_message(JobUpdated(job))
        await pilot.pause()
        await _open_run_dialog(app, pilot)
        assert app.screen.query_one("#ask_become_pass", Checkbox).value is True


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


async def test_run_summary_stays_small_and_lists_problem_hosts_once(tmp_path):
    from textual.widgets import RichLog

    from ansicurse.runner import Job, RunOptions
    from ansicurse.widgets.jobs import JobUpdated

    app = AnsicurseApp(EXAMPLES)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        job = Job(EXAMPLES / "hello.yml", RunOptions(), EXAMPLES)
        await app.jobs.add_job(job)
        await pilot.pause()  # RichLog holds writes back until it knows its size
        for i in range(100):
            job.hosts[f"web{i}"] = "ok"
        job.hosts.update({"web10": "failed", "web2": "failed", "db1": "unreachable"})
        job.status, job.rc = "failed", 4
        job._log_path = Path.home() / ".local" / "ansicurse" / "logs" / "20261030-120000-1-hello.log"
        app.post_message(JobUpdated(job))
        app.post_message(JobUpdated(job))  # ansible-runner can report the end twice
        await pilot.pause()

        summary = str(app.jobs.query_one(".job-summary").render())
        assert "98 ok" in summary and "2 failed" in summary and "1 unreachable" in summary
        assert "web50" not in summary
        assert app.jobs.query_one(".job-summary").region.height == 1

        lines = [strip.text for strip in app.jobs.query_one(RichLog).lines]
        assert [line for line in lines if "failed (" in line] == ["✖ failed (2): web2, web10"]
        assert [line for line in lines if "unreachable (" in line] == ["⚠ unreachable (1): db1"]
        assert lines[-1].startswith("log: ~/.local/ansicurse/logs/20261030-120000-1-hello.log")


async def test_f10_gives_the_runs_pane_the_whole_height():
    from textual.widgets import RichLog

    from ansicurse.runner import Job, RunOptions

    app = AnsicurseApp(EXAMPLES)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.jobs.add_job(Job(EXAMPLES / "hello.yml", RunOptions(), EXAMPLES))
        await pilot.pause()
        normal = app.jobs.region.height

        await pilot.press("f10")
        await pilot.pause()
        assert app.jobs.region.height > normal * 1.5
        assert not app.query_one("#main").display
        assert isinstance(app.focused, RichLog)  # arrow keys scroll the output right away

        await pilot.press("f10")
        await pilot.pause()
        assert app.jobs.region.height == normal
        assert app.focused is app.browser.query_one("#files")

        # F6 hides the pane, also when it was maximized; F10 brings it back big.
        await pilot.press("f10", "f6")
        await pilot.pause()
        assert not app.jobs.display and app.query_one("#main").display
        assert app.focused is app.browser.query_one("#files")
        await pilot.press("f10")
        await pilot.pause()
        assert app.jobs.display and not app.query_one("#main").display
