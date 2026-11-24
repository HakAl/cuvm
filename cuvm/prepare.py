"""Prepare a Codex golden from a stopped local macOS Tart base."""
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import pathlib
import platform
import re
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time

from cuvm import DATA, PKG, browser, codex, egress, image
from cuvm.guest import Guest

MCP_VERSION = "0.0.83"
WEBKIT_REVISION = "webkit-2368"
DISK_HEADROOM = 10 * 1024**3
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
SETTINGS = DATA / "guest-settings.json"
NETWORK_RESTORED = ("networksetup -getinfo Ethernet | grep -qx 'DHCP Configuration' && "
                    "networksetup -getdnsservers Ethernet | grep -qx 'There aren.t any DNS Servers set on Ethernet.'")


def tart(*args, timeout=90, check=True):
    return subprocess.run(["tart", *args], check=check, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "TART_NO_AUTO_PRUNE": ""})


def vms():
    return {vm["Name"]: vm for vm in json.loads(tart("list", "--format", "json").stdout)}


def check_destination(name, records, inventory):
    if name in inventory or os.path.lexists(image.tart_vm_dir(name)):
        raise ValueError(f"Tart destination {name} already exists")
    path = records / f"{name}.json"
    if os.path.lexists(path):
        raise ValueError(f"image record {path} already exists")


def stopped(inventory, name):
    vm = inventory.get(name, {})
    return vm.get("Source") == "local" and vm.get("State") == "stopped" and vm.get("Running") is False


def identity(name):
    try:
        state = image.tart_vm_dir(name).stat()
    except FileNotFoundError:
        return None
    return state.st_dev, state.st_ino


def fingerprint(key_blob):
    digest = hashlib.sha256(base64.b64decode(key_blob, validate=True)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class PairedGuest(Guest):
    def opts(self, timeout=5):
        return ["-F", "/dev/null", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlgorithms=ssh-ed25519",
                "-o", "UpdateHostKeys=no", "-o", "ForwardAgent=no"]

    def pair(self, expected):
        try:
            result = subprocess.run(["ssh-keyscan", "-T", "5", "-t", "ed25519", self.ip],
                                    capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return False
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[:2] == [self.ip, "ssh-ed25519"]:
                if fingerprint(parts[2]) != expected:
                    raise ValueError("guest SSH host key did not match the trusted fingerprint")
                self.known_hosts.write_text(line + "\n")
                return True
        return False


def terminate(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(10)


class Prepare:
    def __init__(self, base, destination, images_dir, out, source, host_key_fp):
        if not NAME.fullmatch(base) or not NAME.fullmatch(destination) or base == destination:
            raise ValueError("use distinct local Tart names for the base and destination")
        if host_key_fp and not FINGERPRINT.fullmatch(host_key_fp):
            raise ValueError("SSH host key fingerprint must be an ED25519 SHA256 fingerprint")
        self.base, self.destination = base, destination
        self.records, self.out = pathlib.Path(images_dir).resolve(), pathlib.Path(out).resolve()
        self.source, self.host_key_fp = source, host_key_fp
        self.work = "cuvm-prepare-" + secrets.token_hex(8)
        self.log = self.out / self.work
        self.tart_process = self.proxy_process = None
        self.cloning = False
        self.promotion_identity = None

    def preflight(self):
        if platform.system() != "Darwin" or platform.machine() != "arm64" or int(platform.mac_ver()[0].split(".")[0]) < 26:
            raise ValueError("image prepare requires Apple silicon and macOS 26 or newer")
        missing = [tool for tool in ("tart", "ssh", "ssh-keyscan", "sysctl") if shutil.which(tool) is None]
        if missing:
            raise ValueError("missing host tools: " + ", ".join(missing))
        inventory = vms()
        if not stopped(inventory, self.base):
            raise ValueError(f"base {self.base} must be an existing stopped local Tart VM")
        if sum(vm["Running"] is True for vm in inventory.values()) >= 2:
            raise ValueError("both VM slots are in use")
        check_destination(self.destination, self.records, inventory)
        directory = image.tart_vm_dir(self.base)
        disk, config = directory / "disk.img", directory / "config.json"
        if directory.is_symlink() or any(path.is_symlink() or not path.is_file() for path in (disk, config)):
            raise ValueError("base must have a standalone local disk.img and config.json")
        vm = json.loads(config.read_text())
        if not isinstance(vm, dict) or vm.get("os") != "darwin" or vm.get("arch") != "arm64":
            raise ValueError("base must be an ARM macOS image")
        free = shutil.disk_usage(directory).free
        if free < DISK_HEADROOM:
            raise ValueError("at least 10 GiB free on the Tart volume is required after the base is installed")
        host_memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
        memory = vm.get("memorySize")
        if type(memory) is not int or not 0 < memory < host_memory:
            raise ValueError("base RAM allocation must leave memory for macOS on the host")
        return {"base": self.base, "source": self.source or f"local Tart VM {self.base}",
                "destination": self.destination, "record": str(self.records / f"{self.destination}.json"),
                "guest_ram_bytes": memory, "host_free_disk_bytes": free,
                "minimum_free_disk_bytes": DISK_HEADROOM, "download_hosts": list(egress.INSTALL_HOSTS),
                "packages": {"@openai/codex": codex.VERSION, "@playwright/mcp": MCP_VERSION,
                             "webkit": WEBKIT_REVISION, "tmux": "Homebrew if missing"},
                "operator_steps": ["pair the SSH key in the Tart window if needed",
                                   "sign in to Codex on the host after preparation"]}

    def execute(self):
        self.records.mkdir(parents=True, exist_ok=True)
        lock_path = self.records / f".prepare-{self.destination}.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self.build()

    def build(self):
        plan = self.preflight()
        start = time.monotonic()
        self.log.mkdir(parents=True, mode=0o700)
        print(f"Preparation log: {self.log}", flush=True)
        source_hashes = image.hashes(self.base)
        if not stopped(vms(), self.base):
            raise ValueError("base must remain stopped")
        try:
            self.cloning = True
            tart("clone", self.base, self.work, timeout=1800)
            guest, gateway = self.boot_and_pair()
            self.remove_guest_agent(guest)
            port = self.start_proxy(guest.ip, gateway)
            print("Paired SSH. Installing and checking the pinned guest dependencies.", flush=True)
            measured = self.provision(guest, gateway, port)
            guest = self.restore_network(guest)
            self.shutdown(guest)
            terminate(self.proxy_process)
            inventory = vms()
            if not stopped(inventory, self.base) or image.hashes(self.base) != source_hashes:
                raise ValueError("base changed during preparation")
            if not stopped(vms(), self.base):
                raise ValueError("base must remain stopped")
            inventory = vms()
            if not stopped(inventory, self.work):
                raise ValueError("work clone must remain stopped for promotion")
            check_destination(self.destination, self.records, inventory)
            self.promotion_identity = identity(self.work)
            if self.promotion_identity is None:
                raise ValueError("work clone directory is missing")
            tart("rename", self.work, self.destination)
            return image.record(self.destination, self.records, source=plan["source"],
                                codex_info=self.log / "codex.json", browser_info=self.log / "browser.json",
                                prepared={"ssh_host_key_fp": self.host_key_fp,
                                          "provenance": {"build_id": self.work, "base": self.base,
                                                         "base_sha256": source_hashes, "packages": measured["packages"],
                                                         "elapsed_s": round(time.monotonic() - start, 1),
                                                         "download_hosts": plan["download_hosts"]}})
        finally:
            self.cleanup()

    def boot_and_pair(self):
        with (self.log / "tart.log").open("w") as log:
            self.tart_process = subprocess.Popen(
                ["tart", "run", self.work, "--net-host", "--no-clipboard", "--no-audio", "--no-usb-accessories"],
                stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.tart_process.poll() is not None:
                raise RuntimeError("Tart exited before getting an IP; see tart.log")
            ip = self.guest_ip()
            if ip:
                break
            time.sleep(2)
        else:
            raise RuntimeError("Tart guest did not get an IPv4 address")
        guest = PairedGuest(ip, "admin", self.log / "known_hosts")
        attended = not self.host_key_fp
        if attended:
            print(f"In the Tart window for {self.work}, enable Remote Login for admin and install your SSH public key.\n"
                  "In the guest Terminal run: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub\n"
                  "See docs/image.md for the public-key command. Keep the guest running.", flush=True)
            self.host_key_fp = input("Paste the SHA256 fingerprint from the guest window: ").strip()
            if not FINGERPRINT.fullmatch(self.host_key_fp):
                raise ValueError("invalid ED25519 SHA256 fingerprint")
        self.wait_for_key(guest, timeout=45 if attended else 180)
        if not guest.wait_ssh(30):
            raise RuntimeError("could not authenticate with a host SSH key as admin on the paired guest")
        return guest, ip.rsplit(".", 1)[0] + ".1"

    def guest_ip(self):
        result = tart("ip", self.work, timeout=10, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return str(ipaddress.IPv4Address(result.stdout.strip()))
        return None

    def wait_for_key(self, guest, timeout):
        def pair_if_running():
            if self.tart_process.poll() is not None:
                raise RuntimeError("Tart exited before SSH pairing; see tart.log")
            return guest.pair(self.host_key_fp)

        if not guest.wait(pair_if_running, timeout, every=2):
            raise RuntimeError("guest did not present an ED25519 SSH host key before the pairing timeout")

    def start_proxy(self, ip, gateway):
        with (self.log / "egress.stderr").open("w") as err:
            self.proxy_process = subprocess.Popen(
                [sys.executable, "-I", str(PKG / "egress.py"), "--listen", gateway, "--guest", ip,
                 "--install-only", "--log", str(self.log / "egress.jsonl")],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, text=True)
        with self.proxy_process.stdout as stdout:
            ready, _, _ = select.select([stdout], [], [], 30)
            line = stdout.readline() if ready else ""
        if not line:
            raise RuntimeError("install-only proxy did not start; see egress.stderr")
        return json.loads(line)["port"]

    def remove_guest_agent(self, guest):
        self.guest_step(guest, "Tart guest agent removal", """
            setopt nullglob
            for p in /Library/LaunchDaemons/*tart-guest-{agent,daemon}*.plist; do
                sudo -n launchctl bootout system "$p" 2>/dev/null || true
            done
            for p in /Library/LaunchAgents/*tart-guest-{agent,daemon}*.plist; do
                launchctl bootout gui/$(id -u) "$p" 2>/dev/null || true
            done
            sudo -n rm -f /Library/LaunchDaemons/*tart-guest-{agent,daemon}*.plist \
                /Library/LaunchAgents/*tart-guest-{agent,daemon}*.plist || exit 1
            if [ -d /opt/homebrew/Cellar/tart-guest-agent ]; then
                HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 \
                    /opt/homebrew/bin/brew uninstall --force tart-guest-agent || exit 1
            fi
            sudo -n rm -f /opt/homebrew/bin/tart-guest-agent
        """)
        absent = r"""
            export LC_ALL=C
            for domain in system gui/$(id -u); do
                for job in org.cirruslabs.tart-guest-agent org.cirruslabs.tart-guest-daemon; do
                    if state=$(sudo -n launchctl print "$domain/$job" 2>&1); then exit 1; fi
                    [[ "$state" = *"Could not find service \"$job\" in domain"* ]] || exit 2
                done
            done
            lookup=0
            pgrep -f '^(/[^ ]*/)?tart-guest-(agent|daemon)([[:space:]]|$)' >/dev/null || lookup=$?
            test "$lookup" -eq 1
        """
        if not guest.wait(lambda: guest.ok(absent, timeout=5), 15):
            raise RuntimeError("could not verify that the Tart guest services and processes are gone")

    def provision(self, guest, gateway, port):
        self.guest_step(guest, "clean base", "test ! -e ~/.codex && test ! -L ~/.codex && "
                        "test ! -e ~/.claude/.credentials.json && test ! -L ~/.claude/.credentials.json")
        network_ok, state = guest.fix_network(gateway)
        (self.log / "build-network.txt").write_text(state)
        if not network_ok:
            raise RuntimeError("could not set the build network")
        args = shlex.join([f"http://{gateway}:{port}", codex.VERSION, MCP_VERSION, WEBKIT_REVISION])
        result = guest.run(f"/bin/zsh -s -- {args}",
                           input=(PKG / "scripts/prepare_codex.sh").read_text(), timeout=1800)
        (self.log / "install.stderr").write_text(result.stderr)
        if result.returncode:
            raise RuntimeError("could not install or verify the guest dependencies; see install.stderr")
        measured = json.loads(result.stdout)
        codex.check_image({"agent": "codex"}, measured)
        browser.check_image({"browser": "webkit"}, measured)
        for block in ("codex", "browser"):
            (self.log / f"{block}.json").write_text(json.dumps(measured[block], indent=2) + "\n")

        self.guest_step(guest, "guest proxy environment", "sudo -n tee /etc/zshenv >/dev/null && "
                        "sudo -n chown root:wheel /etc/zshenv && sudo -n chmod 644 /etc/zshenv",
                        (DATA / "zshenv").read_text())
        self.guest_step(guest, "guest path", r"""
            line='export PATH="/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"'
            grep -Fqx "$line" ~/.zshenv 2>/dev/null || printf '%s\n' "$line" >> ~/.zshenv""")
        self.guest_step(guest, "guest settings",
                        "umask 077 && mkdir -p ~/exp/results ~/.claude && cat > ~/.claude/settings.json",
                        SETTINGS.read_text())
        self.guest_step(guest, "guest cleanup", """
            rm -rf ~/.codex ~/.npm ~/.zsh_history ~/.cuvm ~/Library/Caches/org.webkit.Playwright || exit 1
            test ! -e ~/.codex && test ! -L ~/.codex
        """)
        self.guest_step(guest, "settings hash",
                        f'test "$(shasum -a 256 ~/.claude/settings.json | cut -d " " -f 1)" = {image.sha256(SETTINGS)}')
        self.guest_step(guest, "no Codex login or token state", codex.e2_cmd())
        return measured

    def restore_network(self, guest):
        restore = ("nohup /bin/sh -c 'networksetup -setdhcp Ethernet && networksetup -setdnsservers Ethernet empty' "
                   "</dev/null >/tmp/cuvm-network-restore.log 2>&1 &")
        self.guest_step(guest, "detached network restore", "sudo -n /bin/sh -c " + shlex.quote(restore),
                        connection_may_drop=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ip = self.guest_ip()
            if ip and ip != guest.ip:
                candidate = PairedGuest(ip, guest.user, guest.known_hosts)
                if candidate.pair(self.host_key_fp):
                    guest = candidate
            if ip == guest.ip and guest.ok(NETWORK_RESTORED, timeout=8):
                return guest
            time.sleep(2)
        raise RuntimeError("could not verify restored DHCP and DNS on the paired guest")

    def guest_step(self, guest, stage, command, content=None, *, connection_may_drop=False):
        try:
            result = guest.run(command, input=content, timeout=15 if connection_may_drop else 60)
        except subprocess.TimeoutExpired:
            if connection_may_drop:
                return
            raise
        if result.returncode and not (connection_may_drop and result.returncode == 255):
            (self.log / "guest.stderr").write_text(f"{stage}\n{result.stderr}")
            raise RuntimeError(f"could not set or verify {stage}; see guest.stderr")

    def shutdown(self, guest):
        guest.run("sudo -n shutdown -h now", timeout=30)
        self.tart_process.wait(90)
        if not stopped(vms(), self.work):
            raise RuntimeError("prepared clone did not shut down")

    def cleanup(self):
        try:
            terminate(self.proxy_process)
        finally:
            try:
                inventory = vms() if self.cloning else {}
                if self.work in inventory:
                    if not stopped(inventory, self.work):
                        tart("stop", self.work, "--timeout", "30", check=False)
                    terminate(self.tart_process)
                    if not stopped(vms(), self.work):
                        raise RuntimeError(f"could not stop work clone {self.work}")
                    tart("delete", self.work)
            finally:
                terminate(self.tart_process)
                if self.promotion_identity is not None and self.promotion_identity == identity(self.destination):
                    self.report_incomplete_record()

    def report_incomplete_record(self):
        path = self.records / f"{self.destination}.json"
        try:
            published = json.loads(path.read_text())["preparation"]["build_id"] == self.work
        except (OSError, ValueError, TypeError, KeyError):
            published = False
        if not published:
            print(f"Image {self.destination} was promoted. Its preparation record is incomplete at {path}.",
                  file=sys.stderr)


def interrupted(_signal, _frame):
    raise KeyboardInterrupt


def main(args):
    try:
        prep = Prepare(args.base, args.destination, args.images, args.out, args.source, args.host_key_fp)
        plan = prep.preflight()
        if not args.execute:
            print(json.dumps(plan, indent=2))
            return 0
        if not args.host_key_fp and not sys.stdin.isatty():
            raise ValueError("execution needs a terminal for pairing or a trusted --host-key-fp")
        previous = signal.signal(signal.SIGTERM, interrupted)
        try:
            print(prep.execute())
        finally:
            signal.signal(signal.SIGTERM, previous)
        return 0
    except KeyboardInterrupt:
        print("image prepare interrupted", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, EOFError) as err:
        print(f"image prepare: {err}", file=sys.stderr)
        return 1
