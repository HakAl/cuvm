"""ssh access to one guest. All commands use the output root's known_hosts file."""
import pathlib
import subprocess
import time


class Guest:
    def __init__(self, ip, user, known_hosts):
        self.ip, self.user, self.known_hosts = ip, user, pathlib.Path(known_hosts)

    def opts(self, timeout=5):
        return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % timeout,
                "-o", "UserKnownHostsFile=%s" % self.known_hosts, "-o", "StrictHostKeyChecking=accept-new"]

    def run(self, cmd, input=None, timeout=120):
        return subprocess.run(["ssh", *self.opts(), "%s@%s" % (self.user, self.ip), cmd],
                              input=input, capture_output=True, text=True, timeout=timeout)

    def ok(self, cmd, timeout=30):
        try:
            return self.run(cmd, timeout=timeout).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def background(self, cmd):
        """For commands that drop the ssh session (network changes)."""
        return subprocess.Popen(["ssh", *self.opts(), "%s@%s" % (self.user, self.ip), cmd],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait(self, check, timeout, every=1.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(every)
        return False

    def wait_ssh(self, timeout=180):
        return self.wait(lambda: self.ok("true", timeout=8), timeout, every=2)

    def host_key_fp(self):
        r = subprocess.run(["ssh-keygen", "-lF", self.ip, "-f", str(self.known_hosts)], capture_output=True, text=True)
        fps = [l.split()[2] for l in r.stdout.splitlines() if "ED25519" in l]
        return fps[-1] if fps else None

    def fix_network(self, gateway, timeout=30):
        """Router and DNS = gateway, so macOS Local Network privacy lets claude reach the proxy
        (docs/findings.md). Returns the observed state."""
        self.background("sudo networksetup -setmanual Ethernet %s 255.255.255.0 %s && "
                        "sudo networksetup -setdnsservers Ethernet %s" % (self.ip, gateway, gateway))
        time.sleep(3)
        check = ("route -n get default 2>/dev/null | grep -q 'gateway: %s' && "
                 "scutil --dns | grep -q 'nameserver.*: %s'" % (gateway, gateway))
        ok = self.wait(lambda: self.ok(check, timeout=5), timeout)
        state = self.run("networksetup -getinfo Ethernet | head -4; route -n get default | grep -E 'gateway|interface'; "
                         "scutil --dns | grep -m2 nameserver", timeout=30).stdout
        return ok, state

    def pull(self, remote, local):
        local = pathlib.Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["scp", "-q", *self.opts(), "%s@%s:%s" % (self.user, self.ip, remote), str(local)],
                           capture_output=True, text=True, timeout=300)
        return r.returncode == 0
