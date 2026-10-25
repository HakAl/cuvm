"""Image records: a golden image identified by hash, kept in <images>/<vm>.json."""
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import time

from cuvm import DATA, proxy


def tart_vm_dir(vm):
    return pathlib.Path(os.environ.get("TART_HOME", pathlib.Path.home() / ".tart")) / "vms" / vm


def sha256(path):
    """Streamed: golden disk images are tens of GB."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 24):
            h.update(chunk)
    return h.hexdigest()


def hashes(vm):
    d = tart_vm_dir(vm)
    return {"disk": sha256(d / "disk.img"), "config": sha256(d / "config.json")}


def tart_state(vm):
    vms = json.loads(subprocess.run(["tart", "list", "--format", "json"], capture_output=True, text=True,
                                    check=True).stdout)
    return next((v for v in vms if v["Name"] == vm), None)


def record(vm, images_dir, settings=DATA / "guest-settings.json", source=None, guest_user="admin"):
    """Hash a stopped golden image and write its record. Returns the record path."""
    st = tart_state(vm)
    if st is None:
        raise SystemExit("no Tart VM named %s" % vm)
    if st["Running"]:
        raise SystemExit("%s is running; stop it first (a running disk changes while it is hashed)" % vm)
    rec = {
        "vm": vm,
        "image_sha256": hashes(vm),
        "source": source or st.get("Source"),
        "guest_user": guest_user,
        "settings_sha256": sha256(settings),
        "ssh_host_key_fp": None,  # pinned by the first run, then enforced
        "recorded_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = pathlib.Path(images_dir) / (vm + ".json")
    old = json.loads(path.read_text()) if path.exists() else {}
    if old.get("image_sha256") == rec["image_sha256"]:
        # Same bytes, same host key: re-recording must not reopen trust on first use.
        rec["ssh_host_key_fp"] = old.get("ssh_host_key_fp")
        rec["source"] = source or old.get("source") or rec["source"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=1) + "\n")
    return path


def load(images_dir, vm):
    path = pathlib.Path(images_dir) / (vm + ".json")
    if not path.exists():
        raise SystemExit("no image record %s; run: python3 -m cuvm image record %s" % (path, vm))
    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_SH)  # pin_fp may be writing it
        return path, json.load(f)


def pin_fp(path, fp):
    """Record the host key on first use; concurrent first runs agree or the later one fails.
    Returns (pinned fingerprint, "pinned" | "matched" | "mismatch")."""
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        rec = json.load(f)
        if rec.get("ssh_host_key_fp") is None:  # write once, so later runs never truncate it
            rec["ssh_host_key_fp"] = fp
            f.seek(0)
            f.truncate()
            f.write(json.dumps(rec, indent=1) + "\n")
            return fp, "pinned"
        return rec["ssh_host_key_fp"], "matched" if rec["ssh_host_key_fp"] == fp else "mismatch"


def build_proxy(vm, out, allow=DATA / "allow-imagebuild.txt"):
    """Foreground tinyproxy on the running VM's gateway, port 8888 (the image's /etc/zshenv port),
    with the image-build allowlist. Stops on Ctrl-C. Log: <out>/image-build-<vm>/tinyproxy.log."""
    ip = subprocess.run(["tart", "ip", vm], capture_output=True, text=True).stdout.strip()
    if not ip:
        raise SystemExit("%s has no IP; start it with: tart run %s --net-host --no-clipboard --no-audio "
                         "--no-usb-accessories" % (vm, vm))
    gw = ip.rsplit(".", 1)[0] + ".1"
    d = pathlib.Path(out).resolve() / ("image-build-" + vm)
    d.mkdir(parents=True, exist_ok=True)
    conf = d / "tinyproxy.conf"
    conf.write_text(proxy.render(gw, ip, 8888, str(allow), str(d / "tinyproxy.log")))
    p = proxy.start(conf, d / "tinyproxy.stdout")
    time.sleep(1)
    if p.poll() is not None:
        raise SystemExit("tinyproxy exited; see %s" % (d / "tinyproxy.stdout"))
    print("proxy for %s (guest %s) on %s:8888, log %s. Ctrl-C to stop." % (vm, ip, gw, d / "tinyproxy.log"), flush=True)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.terminate()
        p.wait(10)
