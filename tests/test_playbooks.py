from pathlib import Path

from ansicurse.playbooks import EntryKind, inspect_file, is_playbook_data, load_yaml, scan_directory

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_detects_plays_and_imports():
    assert is_playbook_data([{"hosts": "all", "tasks": []}])
    assert is_playbook_data([{"ansible.builtin.import_playbook": "x.yml"}])
    assert not is_playbook_data([{"name": "a task", "debug": {"msg": "hi"}}])
    assert not is_playbook_data({"hosts": "all"})
    assert not is_playbook_data([])


def test_tolerates_ansible_tags():
    data = load_yaml("- hosts: all\n  vars:\n    secret: !vault |\n      $ANSIBLE_VAULT;1.1;AES256\n      6162\n")
    assert data[0]["vars"]["secret"] == "<vault>"


def test_example_playbooks_are_classified():
    assert inspect_file(EXAMPLES / "hello.yml").kind is EntryKind.PLAYBOOK
    assert inspect_file(EXAMPLES / "site.yml").kind is EntryKind.PLAYBOOK
    assert inspect_file(EXAMPLES / "tasks" / "common.yml").kind is EntryKind.YAML
    assert inspect_file(EXAMPLES / "group_vars" / "all.yml").kind is EntryKind.YAML


def test_outline_and_tags():
    info = inspect_file(EXAMPLES / "hello.yml").playbook
    assert info is not None
    play = info.plays[0]
    assert play.label == "Say hello locally"
    assert play.detail == "hosts: local"
    tasks = play.children[0]
    assert tasks.label == "tasks"
    labels = [node.label for node in tasks.children]
    assert labels[:3] == ["Greet", "Read the kernel version", "Report a change so the run shows a yellow host"]
    block = tasks.children[3]
    assert block.kind == "block"
    assert [c.label for c in block.children] == ["This fails on purpose", "rescue"]
    assert tasks.children[4].kind == "include"
    assert info.all_tags() == ["change", "greet", "info", "rescue"]


def test_vault_file(tmp_path):
    secret = tmp_path / "secrets.yml"
    secret.write_text("$ANSIBLE_VAULT;1.1;AES256\n3132\n")
    assert inspect_file(secret).kind is EntryKind.VAULT


def test_scan_orders_entries(tmp_path):
    (tmp_path / "zdir").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "vars.yml").write_text("a: 1\n")
    (tmp_path / "play.yml").write_text("- hosts: all\n")
    (tmp_path / "README.md").write_text("hi\n")

    names = [e.name for e in scan_directory(tmp_path)]
    assert names == ["..", "zdir", "play.yml", "vars.yml", "README.md"]

    only = [e.name for e in scan_directory(tmp_path, playbooks_only=True)]
    assert only == ["..", "zdir", "play.yml"]

    assert ".hidden" in [e.name for e in scan_directory(tmp_path, show_hidden=True)]


def test_broken_yaml_is_not_a_playbook(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("- hosts: [unclosed\n")
    entry = inspect_file(bad)
    assert entry.kind is EntryKind.YAML
    assert entry.error


def test_detects_become(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("- hosts: all\n  tasks:\n    - name: t\n      ansible.builtin.ping:\n      become: yes\n")
    assert inspect_file(play).playbook.uses_become

    play.write_text("- hosts: all\n  become: false\n  tasks: []\n")
    assert not inspect_file(play).playbook.uses_become

    assert not inspect_file(EXAMPLES / "hello.yml").playbook.uses_become
