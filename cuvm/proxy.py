"""tinyproxy per run: render config from the repo template, start, stop, parse its log."""
import re
import socket
import subprocess

from cuvm import DATA

TEMPLATE = DATA / "tinyproxy.conf.template"


def render(gateway, guest, port, allow_file, log_file):
    t = TEMPLATE.read_text()
    t = t.replace("@GATEWAY@", gateway).replace("@GUEST@", guest)
    t = t.replace("@ALLOWFILE@", allow_file).replace("@LOGFILE@", log_file)
    t = t.replace("\nPort 8888\n", "\nPort %d\n" % port)
    return t


def free_port(host):
    s = socket.socket()
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start(conf_path, out_path):
    out = open(out_path, "w")
    return subprocess.Popen(["tinyproxy", "-d", "-c", str(conf_path)], stdout=out, stderr=subprocess.STDOUT)


def parse_log(text):
    return {
        "allowed": sorted(set(re.findall(r'Established connection to host "([^"]+)"', text))),
        "denied": sorted(set(re.findall(r'Proxying refused on filtered domain "([^"]+)"', text))),
        "requested": sorted(set(re.findall(r"Request \(file descriptor \d+\): CONNECT (\S+?)(?::443)? HTTP", text))),
        "unauthorized": sorted(set(re.findall(r'Unauthorized connection from "([^"]+)"', text))),
    }
