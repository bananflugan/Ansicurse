<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/ansicurse-logo-dark.svg">
    <img src="docs/assets/logo/ansicurse-logo-light.svg" alt="Ansicurse" width="420">
  </picture>
</p>

# Ansicurse

A terminal UI for Ansible. Browse a folder, see which files are playbooks, read and edit them, and run them in the background with live per-host status. It runs ordinary `ansible-playbook` commands.

```
┌ ~/ansible ────────────────┬ hello.yml  playbook ─────────────────────┐
│ ↰ ..                    │ Preview  Outline  Edit                   │
│ ▸ group_vars/           │  1 - name: Say hello                     │
│ ▸ roles/                │  2   hosts: local                        │
│ ◆ hello.yml   1 play    │  …                                       │
│ ◆ site.yml    2 plays   │                                          │
├ Runs ───────────────────┴──────────────────────────────────────────┤
│ ✔ #1 run hello.yml   localhost ●                                   │
│ TASK [Greet] ok: [localhost] => { "msg": "Hello from Ansicurse" }   │
└────────────────────────────────────────────────────────────────────┘
```

## Install

Ansicurse needs Python 3.10 or newer. It's installed with [pipx](https://pipx.pypa.io), which installs Python command-line apps, each in its own isolated environment.

### 1. Install pipx

**macOS** (Homebrew):

```bash
brew install pipx
pipx ensurepath
```

**Debian / Ubuntu** (Debian 12 or newer, Ubuntu 22.04 or newer):

```bash
sudo apt update
sudo apt install pipx git
pipx ensurepath
```

**Fedora**:

```bash
sudo dnf install pipx git
pipx ensurepath
```

**RHEL / Rocky Linux / AlmaLinux 9**: the system Python is 3.9, which is too old, so install Python 3.12 next to it and install pipx with that:

```bash
sudo dnf install python3.12 python3.12-pip git
python3.12 -m pip install --user pipx
python3.12 -m pipx ensurepath
```

On version 10, the system Python is new enough: use `python3-pip` and `python3` in place of `python3.12-pip` and `python3.12`.

`pipx ensurepath` adds `~/.local/bin` to your PATH. Open a new terminal (or log out and in again) before the next step.

Older releases, such as Debian 11 or Ubuntu 20.04, ship a Python older than 3.10 and aren't supported.

### 2. Install Ansicurse

Install it straight from GitHub:

```bash
pipx install git+https://github.com/bananflugan/ansicurse.git
```

This puts an `ansicurse` command on your PATH. Ansicurse runs the `ansible-playbook` already installed on your machine, with your collections and `ansible.cfg`. If you don't have Ansible, install it from your package manager (`sudo apt install ansible`, `sudo dnf install ansible-core`, `brew install ansible`), or add it to Ansicurse's own environment:

```bash
pipx inject ansicurse ansible-core
```

To update later, run `pipx upgrade ansicurse`; to remove it, run `pipx uninstall ansicurse`.

## Run

```bash
ansicurse
```

It starts in the current folder. To start somewhere else, pass the folder: `ansicurse ~/ansible`.

## Keys

| Key | Where | Action |
| --- | --- | --- |
| ↑ ↓ | file list | Move; the preview follows |
| Enter, →, l | file list | Open a folder · open the run dialog for a playbook · edit any other file |
| ←, h, Backspace | file list | Go up one folder |
| / | file list | Filter by name (Esc clears) |
| p | file list | Show playbooks only |
| . | file list | Show hidden files |
| F5, r | | Run the selected playbook |
| F2, c | | `--syntax-check` the selected playbook |
| F3 | | Cycle Preview / Outline / Edit |
| F4, e | | Open in `$VISUAL` / `$EDITOR` |
| Ctrl+S | editor | Save |
| F6 | | Show or hide the runs pane |
| F8 | | Stop the selected run |
| F9 | | Forget the passwords typed this session |
| Ctrl+R | run dialog | Run |
| q, Ctrl+Q | | Quit |

## Become and vault passwords

The run dialog has two checkboxes, **Ask become password (-K)** and **Ask vault password (-J)**. Ansicurse ticks them for you when the playbook uses `become` or when it finds vault-encrypted files next to it. When you run, it asks for each password once, in a masked prompt, and reuses it for later runs in the same project.

- Passwords are stored in memory only. They're never written to disk, not to `options.json` and not to the run artifacts. ansible-runner types them into Ansible's own `BECOME password:` and `Vault password:` prompts.
- They're forgotten when you quit, or straight away with F9.
- If Ansible rejects a password (for example "Incorrect sudo password"), Ansicurse forgets it and asks again on the next run.
- If your `ansible.cfg` or environment already sets `become_password_file` or `vault_password_file`, Ansicurse doesn't ask and leaves the checkbox unticked. Ansible reads the file as usual.

If you prefer a password file, a plaintext file can be read by anyone or anything with access to your home folder. Ansible can run the file instead of reading it: if `become_password_file` (or `vault_password_file`) points to an executable script, Ansible uses whatever the script prints. That lets the script fetch the password from a secret store, so no plaintext copy sits on disk. For example, with [pass](https://www.passwordstore.org/) on a Linux server:

```sh
#!/bin/sh
# ~/.ansible/become-pass.sh  (chmod 700)
exec pass show ansible/become
```

```ini
# ansible.cfg
[defaults]
become_password_file = ~/.ansible/become-pass.sh
```

On macOS, the script could read from the keychain instead: `security find-generic-password -s ansible-become -w`.

## How it works

- **Finding playbooks:** a YAML file is a playbook if it's a list whose items all have `hosts:` or `import_playbook:`. Task files, vars files and vault-encrypted files are listed but not marked as runnable.
- **Where commands run:** the nearest folder above the playbook that has an `ansible.cfg`. If there is none, the folder Ansicurse was started in. The run dialog shows the working directory and the exact command.
- **Inventory:** if `ansible.cfg` sets one, Ansicurse leaves `-i` empty so Ansible uses it. Otherwise it suggests an `inventory*` or `hosts*` file it finds.
- **Running:** jobs go through [ansible-runner](https://ansible.readthedocs.io/projects/runner/), which streams structured events. They drive the per-host status (✔ ok, ● changed, ✖ failed, ⚠ unreachable). Several runs can go at once, one tab each.
- **Production safety:** if the inventory or limit contains `prod`, Ansicurse asks for confirmation before the run starts.
- **State:** run artifacts go to `~/.local/state/ansicurse/runs/` (or `$XDG_STATE_HOME`). The last options used for each playbook are saved in `options.json` and pre-filled next time. ansible-runner stores the full process environment in each run's `artifacts/run/command` file, so run folders are created readable by your user only (0700).


## Roadmap

- Run history browser: reopen old runs, re-run with the same options, re-run failed hosts only
- Output filters: only changed or failed results, or a single host
- Vault: view and edit encrypted files in the editor
- Inventory browser (`ansible-inventory --graph`) for picking a limit
- `ansible-lint` results shown inline in the editor
- Pick tags or a start task from the outline view

## License

Copyright (C) 2026 Nichlas Rehnstam

Ansicurse is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. See [LICENSE](LICENSE) for the full text.
