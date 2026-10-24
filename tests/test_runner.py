from pathlib import Path

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
