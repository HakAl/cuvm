"""cuvm command line.

  python3 -m cuvm run CONFIG [--out DIR] [--images DIR] [--repeat N] [--keep-on-fail]
  python3 -m cuvm run --interactive [CONFIG] [--out DIR] [--images DIR] [--keep-on-fail]
  python3 -m cuvm image record VM [--images DIR] [--settings FILE] [--source REF] [--guest-user NAME]
                               [--browser-info FILE]
  python3 -m cuvm image proxy VM [--out DIR] [--allow FILE]
"""
import argparse
import pathlib
import sys

from cuvm import DATA, image

DEFAULT_INTERACTIVE = pathlib.Path(__file__).resolve().parent.parent / "experiments/interactive/config.json"


def positive(s):
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cuvm", description="Run computer-use experiments in disposable Tart VMs.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one experiment config end to end")
    r.add_argument("config", nargs="?", help="experiment config (optional with --interactive)")
    r.add_argument("--interactive", action="store_true",
                   help="attach this terminal to the guest Claude session; exiting Claude ends the run")
    r.add_argument("--out", default="runs", help="output root for run records and results (default ./runs)")
    r.add_argument("--images", default="images", help="directory of image records (default ./images)")
    r.add_argument("--repeat", type=positive, default=1)
    r.add_argument("--keep-on-fail", action="store_true",
                   help="on FAIL, stop but do not delete the VM (never when the login could not be removed)")
    i = sub.add_parser("image", help="image records")
    isub = i.add_subparsers(dest="icmd", required=True)
    rec = isub.add_parser("record", help="hash a stopped golden VM and write <images>/<vm>.json")
    rec.add_argument("vm")
    rec.add_argument("--images", default="images")
    rec.add_argument("--settings", default=str(DATA / "guest-settings.json"),
                     help="the guest ~/.claude/settings.json installed in the image")
    rec.add_argument("--source", help="where the image came from (default: Tart's Source field)")
    rec.add_argument("--guest-user", default="admin")
    rec.add_argument("--browser-info", help="JSON of the image's browser facts (docs/image.md, Derived browser image)")
    bp = isub.add_parser("proxy", help="run the image-build proxy for a running golden VM (foreground)")
    bp.add_argument("vm")
    bp.add_argument("--out", default="runs")
    bp.add_argument("--allow", default=str(DATA / "allow-imagebuild.txt"), help="allowlist (default: image-build list)")
    a = ap.parse_args(argv)
    if a.cmd == "image" and a.icmd == "proxy":
        image.build_proxy(a.vm, a.out, a.allow)
        return 0
    if a.cmd == "image":
        print(image.record(a.vm, a.images, a.settings, a.source, a.guest_user, browser_info=a.browser_info))
        return 0
    if a.interactive:
        if a.repeat != 1:
            ap.error("--interactive runs one session; --repeat must be 1")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            ap.error("--interactive needs a terminal on stdin and stdout")
        a.config = a.config or str(DEFAULT_INTERACTIVE)
    elif not a.config:
        ap.error("config is required unless --interactive")
    from cuvm import run
    results = []
    for _ in range(a.repeat):
        r = run.Run(a.config, out=a.out, images=a.images, keep_on_fail=a.keep_on_fail, interactive=a.interactive)
        results.append(r.execute())
        if r.r.get("end_reason") == "operator_interrupt":
            break  # Ctrl-C ends the series, not just this run
    print("PASS %d / %d" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
