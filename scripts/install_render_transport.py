"""Run as root on codervm: python3 scripts/install_render_transport.py worker_key.pub."""
import argparse
from pathlib import Path
import pwd
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("public_key")
args = parser.parse_args()
key = Path(args.public_key).read_text().strip()
if not key.startswith("ssh-ed25519 ") or len(key.splitlines()) != 1:
    raise ValueError("expected one Ed25519 public key")
repo = Path(__file__).resolve().parent.parent
try:
    account = pwd.getpwnam("bot-render")
except KeyError:
    subprocess.run(["useradd", "--system", "--create-home", "--shell", "/bin/sh", "bot-render"], check=True)
    account = pwd.getpwnam("bot-render")
home = Path(account.pw_dir)
ssh = home / ".ssh"
queue = Path("/var/lib/explain-bot-render/queue")
for path in (ssh, queue):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    shutil.chown(path, user=account.pw_uid, group=account.pw_gid)
authorized = ssh / "authorized_keys"
command = f'/usr/bin/python3 {repo}/scripts/three_d_broker.py'
line = f'restrict,command="{command}" {key}'
lines = authorized.read_text().splitlines() if authorized.exists() else []
if line not in lines:
    authorized.write_text("\n".join(lines + [line]) + "\n")
authorized.chmod(0o600)
shutil.chown(authorized, user=account.pw_uid, group=account.pw_gid)
ip = subprocess.check_output(["tailscale", "ip", "-4"], text=True).strip()
Path("/run/sshd").mkdir(exist_ok=True)
Path("/etc/ssh/sshd_config_render").write_text(f'''Port 2222
ListenAddress {ip}
HostKey /etc/ssh/ssh_host_ed25519_key
PidFile /run/sshd-render.pid
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers bot-render
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
PermitRootLogin no
UsePAM yes
DisableForwarding yes
PermitTTY no
ForceCommand {command}
''')
shutil.copyfile(repo / "deploy/explain-bot-render-sshd.service", "/etc/systemd/system/explain-bot-render-sshd.service")
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable", "--now", "explain-bot-render-sshd"], check=True)
subprocess.run(["systemctl", "restart", "explain-bot-render-sshd"], check=True)
print(f"THREED_WORKER_UID={account.pw_uid}\nTHREED_WORKER_GID={account.pw_gid}")
