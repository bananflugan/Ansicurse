import pytest

from ansicurse import project


@pytest.fixture(autouse=True)
def isolated_ansible_environment(tmp_path_factory, monkeypatch):
    """Keep the machine's own Ansible config, password files and state out of the tests."""
    # Separate temp dirs, so tests that list their own tmp_path don't see these.
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
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
