from pathlib import Path

import pytest

from ansicurse.project import project_info, resolve_workdir
from ansicurse.runner import Job, RunOptions, build_args, display_command, looks_like_production

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_build_args():
    options = RunOptions(
        inventory="inv.ini",
        limit="web01",
        tags="a,b",
        skip_tags="c",
        extra_vars="x=1 y=2",
        check=True,
        diff=True,
        become=True,
        verbosity=2,
    )
    assert build_args(options) == [
        "-i", "inv.ini", "--limit", "web01", "--tags", "a,b", "--skip-tags", "c",
        "-e", "x=1 y=2", "--check", "--diff", "--become", "-vv",
    ]
    assert build_args(RunOptions()) == []


def test_display_command_is_relative_and_quoted(tmp_path):
    playbook = tmp_path / "plays" / "site.yml"
    command = display_command(playbook, RunOptions(extra_vars="a=1 b=2"), tmp_path)
    assert command == "ansible-playbook -e 'a=1 b=2' plays/site.yml"


def test_production_detection():
    assert looks_like_production(RunOptions(inventory="inventories/prod/hosts"))
    assert looks_like_production(RunOptions(limit="Production"))
    assert not looks_like_production(RunOptions(inventory="staging"))


def test_options_round_trip_ignores_unknown_keys():
    options = RunOptions(limit="db", check=True)
    assert RunOptions.from_dict({**options.to_dict(), "removed_field": 1}) == options


def test_host_states_take_the_worst_result():
    job = Job(Path("/x/site.yml"), RunOptions(), Path("/x"))
    job.handle_event({"event": "runner_on_ok", "event_data": {"host": "a", "res": {"changed": False}}})
    job.handle_event({"event": "runner_on_ok", "event_data": {"host": "a", "res": {"changed": True}}})
    job.handle_event({"event": "runner_on_ok", "event_data": {"host": "a", "res": {}}})
    job.handle_event({"event": "runner_on_failed", "event_data": {"host": "b", "ignore_errors": True}})
    job.handle_event({"event": "runner_on_unreachable", "event_data": {"host": "c"}})
    out = job.handle_event({"event": "verbose", "stdout": "hello"})
    assert job.hosts == {"a": "changed", "b": "ok", "c": "unreachable"}
    assert out == "hello"


def test_stats_event_fills_in_hosts():
    job = Job(Path("/x/site.yml"), RunOptions(), Path("/x"))
    job.handle_event(
        {"event": "playbook_on_stats", "event_data": {"ok": {"a": 2, "b": 1}, "failures": {"b": 1}}}
    )
    assert job.hosts == {"a": "ok", "b": "failed"}


def test_stats_event_clears_rescued_failures():
    job = Job(Path("/x/site.yml"), RunOptions(), Path("/x"))
    job.handle_event({"event": "runner_on_failed", "event_data": {"host": "a"}})
    assert job.hosts == {"a": "failed"}
    job.handle_event(
        {
            "event": "playbook_on_stats",
            "event_data": {"ok": {"a": 5}, "changed": {"a": 1}, "failures": {}, "rescued": {"a": 1}},
        }
    )
    assert job.hosts == {"a": "changed"}


def test_workdir_is_ansible_cfg_dir():
    assert resolve_workdir(EXAMPLES / "tasks", EXAMPLES.parent) == EXAMPLES


def test_workdir_falls_back_to_launch_dir(tmp_path):
    (tmp_path / "sub").mkdir()
    assert resolve_workdir(tmp_path / "sub", tmp_path) == tmp_path


def test_project_info_reads_inventory_from_cfg(monkeypatch):
    monkeypatch.delenv("ANSIBLE_CONFIG", raising=False)
    info = project_info(EXAMPLES, EXAMPLES)
    assert info.config == EXAMPLES / "ansible.cfg"
    assert info.config_inventory == "inventory.ini"
    assert info.inventory is None


def test_project_info_finds_inventory_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ANSIBLE_CONFIG", raising=False)
    (tmp_path / "hosts.ini").write_text("localhost\n")
    info = project_info(tmp_path, tmp_path)
    assert info.config is None
    assert info.inventory == "hosts.ini"


def test_password_flags_and_kinds():
    options = RunOptions(ask_become_pass=True, ask_vault_pass=True)
    assert build_args(options) == ["--ask-become-pass", "--ask-vault-pass"]
    assert options.password_kinds == ["become", "vault"]
    assert RunOptions().password_kinds == []


def test_password_prompts_match_ansible():
    import re

    from ansicurse.runner import PASSWORD_PROMPTS

    become = re.compile(PASSWORD_PROMPTS["become"], re.M)
    vault = re.compile(PASSWORD_PROMPTS["vault"], re.M)
    assert become.search("BECOME password: ")
    assert become.search("SUDO password: ")
    assert become.search("BECOME password[defaults to SSH password]: ")
    assert not become.search("Vault password: ")
    assert vault.search("Vault password: ")
    assert not vault.search("BECOME password: ")


def test_passwords_stay_out_of_command_and_options():
    options = RunOptions(ask_become_pass=True)
    job = Job(Path("/x/site.yml"), options, Path("/x"), passwords={"become": "s3cret"})
    assert "s3cret" not in job.command
    assert "s3cret" not in repr(vars(options))
    assert "s3cret" not in str(options.to_dict())


def test_rejected_passwords_are_detected():
    job = Job(Path("/x/site.yml"), RunOptions(), Path("/x"), passwords={"become": "wrong"})
    job.handle_event(
        {"event": "runner_on_failed", "event_data": {"host": "a"},
         "stdout": 'fatal: [a]: FAILED! => {"msg": "Incorrect sudo password"}'}
    )
    assert job.password_errors == {"become"}

    # Without a supplied password, the same output says nothing about our password.
    other = Job(Path("/x/site.yml"), RunOptions(), Path("/x"))
    other.handle_event({"event": "verbose", "stdout": "Incorrect sudo password"})
    assert other.password_errors == set()


@pytest.mark.parametrize(
    "stdout",
    [
        'fatal: [web01]: FAILED! => {"msg": "Missing sudo password"}',  # over ssh
        "sudo: a password is required",  # local connection
    ],
)
def test_missing_become_password_is_detected(stdout):
    job = Job(Path("/x/site.yml"), RunOptions(), Path("/x"))
    job.handle_event({"event": "runner_on_failed", "event_data": {"host": "a"}, "stdout": stdout})
    assert job.missing_passwords == {"become"}

    # When we did supply one, it isn't missing.
    supplied = Job(Path("/x/site.yml"), RunOptions(), Path("/x"), passwords={"become": "pw"})
    supplied.handle_event({"event": "verbose", "stdout": stdout})
    assert supplied.missing_passwords == set()


def test_password_files_from_config_and_environment(tmp_path, monkeypatch):
    (tmp_path / "ansible.cfg").write_text(
        "[defaults]\nbecome_password_file = ~/.become_pass  # local\nvault_password_file = vault.sh\n"
    )
    info = project_info(tmp_path, tmp_path)
    assert info.become_password_file == "~/.become_pass"
    assert info.vault_password_file == "vault.sh"

    monkeypatch.setenv("ANSIBLE_BECOME_PASSWORD_FILE", "/run/secret")
    assert project_info(tmp_path, tmp_path).become_password_file == "/run/secret"


def test_config_falls_back_to_home_config(tmp_path):
    import os

    home_cfg = Path(os.environ["HOME"]) / ".ansible.cfg"
    home_cfg.write_text("[defaults]\nbecome_password_file = ~/.pw\n")
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    info = project_info(project_dir, project_dir)
    assert info.config == home_cfg
    assert info.become_password_file == "~/.pw"

    # A project ansible.cfg wins, and Ansible reads only one file.
    (project_dir / "ansible.cfg").write_text("[defaults]\ninventory = hosts\n")
    info = project_info(project_dir, project_dir)
    assert info.config == project_dir / "ansible.cfg"
    assert info.become_password_file is None


def test_uses_vault(tmp_path):
    from ansicurse.project import uses_vault

    playbook = tmp_path / "site.yml"
    playbook.write_text("- hosts: all\n")
    assert not uses_vault(tmp_path, playbook)

    (tmp_path / "group_vars").mkdir()
    (tmp_path / "group_vars" / "all").write_text("$ANSIBLE_VAULT;1.1;AES256\n6162\n")
    assert uses_vault(tmp_path, playbook)


def test_each_run_gets_a_flat_plain_text_log(monkeypatch):
    from ansicurse import runner, state

    monkeypatch.setattr(runner, "find_ansible_playbook", lambda: None)
    job = Job(Path("/x/site.yml"), RunOptions(limit="web01"), Path("/x"))
    shown = []
    job.start(on_output=lambda j, text: shown.append(text), on_status=lambda j: None)

    log = job.log_file
    assert log.parent == state.logs_dir() == Path.home() / ".local" / "ansicurse" / "logs"
    assert log.name.endswith(f"-{job.id}-site.log")
    assert log.stat().st_mode & 0o777 == 0o600
    lines = log.read_text().splitlines()
    assert lines[0] == "$ cd /x"
    assert lines[1] == "$ ansible-playbook --limit web01 site.yml"
    assert lines[2] == shown[0].strip()  # the same output the runs pane shows
    assert lines[-1].startswith("-- error, ")


def test_log_strips_colour_codes():
    job = Job(Path("/x/site.yml"), RunOptions(), Path("/x"))
    job._open_log("colours")
    job._write_log("\x1b[0;32mok: [web01]\x1b[0m\r\n")
    job._close_log()
    assert job.log_file.read_text().splitlines()[2] == "ok: [web01]"


def test_runs_older_than_30_days_are_deleted():
    import os
    import time

    from ansicurse import state

    def make_run(name, days_old):
        log = state.logs_dir() / f"{name}.log"
        run = state.runs_dir() / name
        (run / "artifacts").mkdir(parents=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("output\n")
        stamp = time.time() - days_old * 24 * 60 * 60
        for path in (log, run):
            os.utime(path, (stamp, stamp))
        return log, run

    old_log, old_run = make_run("20260901-120000-1-site", days_old=31)
    new_log, new_run = make_run("20261020-120000-2-site", days_old=29)
    notes = state.logs_dir() / "notes.txt"  # not ours to delete
    notes.write_text("keep me")
    os.utime(notes, (0, 0))
    state.save_options({"/x/site.yml": {"limit": "web01"}})

    state.delete_old_runs()

    assert not old_log.exists() and not old_run.exists()
    assert new_log.exists() and new_run.exists()
    assert notes.exists()
    assert state.load_options() == {"/x/site.yml": {"limit": "web01"}}


async def test_old_runs_are_deleted_when_ansicurse_starts():
    import os

    from ansicurse import state
    from ansicurse.app import AnsicurseApp

    old_log = state.logs_dir() / "20260101-120000-1-site.log"
    old_log.parent.mkdir(parents=True)
    old_log.write_text("output\n")
    os.utime(old_log, (0, 0))
    app = AnsicurseApp(EXAMPLES)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert not old_log.exists()


@pytest.mark.parametrize(
    "direct_url, expected",
    [
        ('{"url": "https://github.com/bananflugan/ansicurse.git", "vcs_info": '
         '{"vcs": "git", "commit_id": "d1c5fb3d0653bde89060596660d144ebf27901c0"}}',
         "ansicurse 0.1.0 (d1c5fb3)"),
        ('{"url": "https://github.com/bananflugan/ansicurse.git", "vcs_info": {"vcs": "git", '
         '"requested_revision": "develop", "commit_id": "4103f94a0c2e1d7b9f8a6e5d4c3b2a1908f7e6d5"}}',
         "ansicurse 0.1.0 (develop 4103f94)"),
        ('{"url": "https://github.com/bananflugan/ansicurse.git", "vcs_info": {"vcs": "git", '
         '"requested_revision": "4103f94", "commit_id": "4103f94a0c2e1d7b9f8a6e5d4c3b2a1908f7e6d5"}}',
         "ansicurse 0.1.0 (4103f94)"),  # installed @<commit>: nothing to add
        ('{"url": "file:///src/ansicurse", "dir_info": {"editable": true}}', "ansicurse 0.1.0"),
        (None, "ansicurse 0.1.0"),  # installed from PyPI or a wheel: no direct_url.json
    ],
)
def test_version_shows_the_installed_commit(monkeypatch, direct_url, expected):
    from ansicurse import __main__

    class FakeDistribution:
        def read_text(self, name):
            return direct_url

    monkeypatch.setattr(__main__, "distribution", lambda name: FakeDistribution())
    assert __main__.version_text() == expected
