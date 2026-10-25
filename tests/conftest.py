import pytest

from ansicurse import project


@pytest.fixture(autouse=True)
def isolated_ansible_environment(tmp_path_factory, monkeypatch):
    """Keep the machine's own Ansible config, password files and Ansicurse data out of the tests."""
    # A separate temp dir, so tests that list their own tmp_path don't see it.
    # Ansicurse keeps its data in ~/.local/ansicurse, so this isolates that too.
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setattr(
        project, "SYSTEM_CONFIG", tmp_path_factory.mktemp("etc") / "no-ansible.cfg"
    )
    for name in (
        "ANSIBLE_CONFIG",
        "ANSIBLE_BECOME_PASSWORD_FILE",
        "ANSIBLE_VAULT_PASSWORD_FILE",
        "ANSIBLE_VAULT_IDENTITY_LIST",
    ):
        monkeypatch.delenv(name, raising=False)
