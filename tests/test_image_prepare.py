import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from cuvm import __main__ as cli
from cuvm import browser, codex, egress, image, prepare


def vm(name, running=False):
    return {"Name": name, "Source": "local", "State": "running" if running else "stopped", "Running": running}


class ImagePrepareTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        self.start(mock.patch.dict(os.environ, {"TART_HOME": str(self.root)}))
        self.inventory = {"base": vm("base")}
        self.start(mock.patch.object(prepare, "vms", return_value=self.inventory))
        self.start(mock.patch.object(prepare.platform, "system", return_value="Darwin"))
        self.start(mock.patch.object(prepare.platform, "machine", return_value="arm64"))
        self.start(mock.patch.object(prepare.platform, "mac_ver", return_value=("26.0", (), "")))
        self.start(mock.patch.object(prepare.shutil, "which", return_value="/usr/bin/tool"))
        self.start(mock.patch.object(prepare.shutil, "disk_usage", return_value=mock.Mock(free=50 * 1024**3)))
        self.start(mock.patch.object(prepare.subprocess, "check_output", return_value=str(32 * 1024**3)))
        self.base = image.tart_vm_dir("base")
        self.base.mkdir(parents=True)
        (self.base / "disk.img").write_bytes(b"test disk")
        (self.base / "config.json").write_text(json.dumps({"os": "darwin", "arch": "arm64", "memorySize": 8 * 1024**3}))
        self.fp = prepare.fingerprint("dGVzdCBrZXk=")
        self.builder = prepare.Prepare("base", "gold", self.root / "records", self.root / "runs", "base@sha256:test", self.fp)

    def start(self, patch):
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def test_cli_preview_does_not_start_or_write(self):
        args = ["image", "prepare", "--agent", "codex", "--base", "base", "--destination", "gold",
                "--images", str(self.builder.records), "--out", str(self.builder.out)]
        output = io.StringIO()
        with mock.patch.object(prepare, "tart") as tart, contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        tart.assert_not_called()
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["download_hosts"], list(egress.INSTALL_HOSTS))
        self.assertEqual(preview["packages"]["@openai/codex"], codex.VERSION)
        self.assertFalse(self.builder.out.exists())
        self.assertFalse(self.builder.records.exists())

    def test_source_and_destinations_are_checked(self):
        self.inventory["base"] = vm("base", running=True)
        with self.assertRaisesRegex(ValueError, "stopped local"):
            self.builder.preflight()
        self.inventory["base"] = vm("base")
        self.inventory["gold"] = vm("gold")
        with self.assertRaisesRegex(ValueError, "destination gold already exists"):
            self.builder.preflight()
        del self.inventory["gold"]
        self.builder.records.mkdir()
        (self.builder.records / "gold.json").symlink_to("absent")
        with self.assertRaisesRegex(ValueError, "record .* already exists"):
            self.builder.preflight()
        with self.assertRaisesRegex(ValueError, "distinct local"):
            prepare.Prepare("ghcr.io/acme/base:latest", "gold", self.root, self.root, None, None)

    def test_pair_uses_only_the_trusted_ed25519_key(self):
        ip = "192.0.2.4"
        guest = prepare.PairedGuest(ip, "admin", self.root / "known_hosts")
        line = f"{ip} ssh-ed25519 dGVzdCBrZXk=\n"
        reply = subprocess.CompletedProcess([], 0, stdout=line)
        with mock.patch.object(prepare.subprocess, "run", return_value=reply):
            with self.assertRaisesRegex(ValueError, "did not match"):
                guest.pair(prepare.fingerprint("b3RoZXI="))
            self.assertFalse(guest.known_hosts.exists())
            guest.pair(self.fp)
        self.assertEqual(guest.known_hosts.read_text(), line)
        self.assertIn("StrictHostKeyChecking=yes", guest.opts())
        self.assertIn("GlobalKnownHostsFile=/dev/null", guest.opts())

    def test_pair_retries_when_ssh_is_not_ready(self):
        ip = "192.0.2.4"
        guest = prepare.PairedGuest(ip, "admin", self.root / "known_hosts")
        line = f"{ip} ssh-ed25519 dGVzdCBrZXk=\n"
        self.builder.tart_process = mock.Mock()
        self.builder.tart_process.poll.return_value = None
        answers = [subprocess.CompletedProcess([], 1, stdout=""),
                   subprocess.CompletedProcess([], 0, stdout=line)]
        with mock.patch.object(prepare.subprocess, "run", side_effect=answers) as scan, \
                mock.patch.object(prepare.time, "sleep"):
            self.builder.wait_for_key(guest, timeout=10)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(guest.known_hosts.read_text(), line)

    def fake_tart(self, cmd, *args, **kwargs):
        if cmd == "clone":
            work = image.tart_vm_dir(args[1])
            work.mkdir()
            for name in ("config.json", "disk.img"):
                (work / name).write_bytes((self.base / name).read_bytes())
            self.inventory[args[1]] = vm(args[1])
        if cmd == "rename":
            image.tart_vm_dir(args[0]).rename(image.tart_vm_dir(args[1]))
            del self.inventory[args[0]]
            self.inventory[args[1]] = vm(args[1])
        if cmd == "delete":
            del self.inventory[args[0]]

    def build_mocks(self, stack):
        measured = {
            "codex": {"version": codex.VERSION, "bin": "/opt/homebrew/bin/codex", "binary": "/opt/bin/codex",
                      "binary_sha256": "a" * 64},
            "browser": {"node": "/opt/homebrew/bin/node", "entry": "/opt/cli.js", "entry_sha256": "b" * 64,
                        "engines": {"webkit": prepare.WEBKIT_REVISION}},
            "packages": {"tmux": "tmux 3.6"},
        }

        def provision(*args):
            for block in ("codex", "browser"):
                (self.builder.log / f"{block}.json").write_text(json.dumps(measured[block]))
            return measured

        tart = stack.enter_context(mock.patch.object(prepare, "tart", side_effect=self.fake_tart))
        stack.enter_context(mock.patch.object(image, "tart_state", side_effect=self.inventory.get))
        stack.enter_context(mock.patch.object(self.builder, "boot_and_pair", return_value=(mock.Mock(), "192.0.2.1")))
        stack.enter_context(mock.patch.object(self.builder, "remove_guest_agent"))
        stack.enter_context(mock.patch.object(self.builder, "start_proxy", return_value=8888))
        stack.enter_context(mock.patch.object(self.builder, "provision", side_effect=provision))
        stack.enter_context(mock.patch.object(self.builder, "restore_network", side_effect=lambda guest: guest))
        stack.enter_context(mock.patch.object(self.builder, "shutdown"))
        return tart

    def test_first_record_has_both_fact_blocks_and_trusted_fingerprint(self):
        hashes = image.hashes("base")
        with contextlib.ExitStack() as stack:
            tart = self.build_mocks(stack)
            path = self.builder.execute()
        record = json.loads(path.read_text())
        codex.check_image({"agent": "codex"}, record)
        browser.check_image({"browser": "webkit"}, record)
        self.assertEqual(record["ssh_host_key_fp"], self.fp)
        self.assertEqual(record["preparation"]["base_sha256"], hashes)
        self.assertEqual(image.hashes("base"), hashes)
        self.assertEqual([call.args[0] for call in tart.call_args_list], ["clone", "rename"])
        with self.assertRaisesRegex(ValueError, "destination gold already exists"):
            self.builder.preflight()

    def test_interrupt_after_rename_reports_incomplete_record(self):
        def interrupt_after_rename(command, *args, **kwargs):
            self.fake_tart(command, *args, **kwargs)
            if command == "rename":
                raise KeyboardInterrupt

        stderr = io.StringIO()
        with contextlib.ExitStack() as stack, contextlib.redirect_stderr(stderr):
            self.build_mocks(stack).side_effect = interrupt_after_rename
            with self.assertRaises(KeyboardInterrupt):
                self.builder.execute()
        self.assertEqual(self.inventory["gold"]["State"], "stopped")
        self.assertIn("Its preparation record is incomplete", stderr.getvalue())

    def test_interrupt_after_record_publication_keeps_valid_record(self):
        write_record = image.record

        def interrupt_after_publication(*args, **kwargs):
            write_record(*args, **kwargs)
            raise KeyboardInterrupt

        stderr = io.StringIO()
        with contextlib.ExitStack() as stack, contextlib.redirect_stderr(stderr):
            self.build_mocks(stack)
            stack.enter_context(mock.patch.object(image, "record", side_effect=interrupt_after_publication))
            with self.assertRaises(KeyboardInterrupt):
                self.builder.execute()
        self.assertEqual(stderr.getvalue(), "")
        record = json.loads((self.builder.records / "gold.json").read_text())
        self.assertEqual(record["preparation"]["build_id"], self.builder.work)

    def test_restore_reconnects_to_a_rediscovered_address_with_the_same_pin(self):
        for outcome in (subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 255),
                        subprocess.TimeoutExpired("ssh", 15)):
            with self.subTest(outcome=outcome):
                previous = mock.Mock(ip="192.0.2.4", user="admin", known_hosts=self.root / "known_hosts")
                previous.run.side_effect = [outcome]
                updated = mock.Mock(ip="192.0.2.5")
                updated.attach_mock(mock.Mock(return_value=True), "pair")
                updated.attach_mock(mock.Mock(return_value=True), "ok")
                with mock.patch.object(self.builder, "guest_ip", return_value=updated.ip), \
                        mock.patch.object(prepare, "PairedGuest", return_value=updated):
                    self.assertIs(self.builder.restore_network(previous), updated)
                launch = previous.run.call_args.args[0]
                self.assertIn("nohup", launch)
                self.assertIn("-setdhcp Ethernet && networksetup -setdnsservers Ethernet empty", launch)
                self.assertEqual(updated.mock_calls, [mock.call.pair(self.fp),
                                                      mock.call.ok(prepare.NETWORK_RESTORED, timeout=8)])

    def test_restore_reports_a_definite_launch_failure(self):
        self.builder.log.mkdir(parents=True)
        guest = mock.Mock()
        guest.run.return_value = subprocess.CompletedProcess([], 1, stderr="sudo failed")
        with mock.patch.object(self.builder, "guest_ip") as lookup:
            with self.assertRaisesRegex(RuntimeError, "detached network restore"):
                self.builder.restore_network(guest)
        lookup.assert_not_called()

    def test_guest_agent_verification_requires_absent_jobs_and_processes(self):
        stubs = r"""
            sudo() {
                case "$JOB_STATE" in
                    loaded) return 0 ;;
                    error) printf 'Permission denied\n' >&2; return 1 ;;
                esac
                printf 'Could not find service "%s" in domain for system\n' "${4##*/}" >&2
                return 113
            }
            pgrep() {
                case "$PROCESS_STATE" in
                    daemon) [[ tart-guest-daemon =~ $2 ]]; return $? ;;
                    error) return 2 ;;
                esac
                return 1
            }
        """
        for jobs, processes, should_pass in (
            ("absent", "absent", True),
            ("loaded", "absent", False),
            ("error", "absent", False),
            ("absent", "daemon", False),
            ("absent", "error", False),
        ):
            with self.subTest(jobs=jobs, processes=processes):
                guest = mock.Mock()
                guest.run.return_value.returncode = 0
                guest.wait.side_effect = lambda verify, timeout: verify()

                def run_check(command, timeout):
                    result = subprocess.run(["/bin/zsh", "-f", "-c", stubs + command],
                                            capture_output=True, text=True, timeout=timeout,
                                            env={**os.environ, "JOB_STATE": jobs, "PROCESS_STATE": processes})
                    return result.returncode == 0

                guest.ok.side_effect = run_check
                if should_pass:
                    self.builder.remove_guest_agent(guest)
                else:
                    with self.assertRaisesRegex(RuntimeError, "could not verify"):
                        self.builder.remove_guest_agent(guest)

    def test_interrupt_deletes_only_the_work_clone(self):
        with mock.patch.object(prepare, "tart", side_effect=self.fake_tart) as tart, \
                mock.patch.object(self.builder, "boot_and_pair", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.builder.execute()
        self.assertEqual(tart.call_args.args, ("delete", self.builder.work))
        self.assertEqual(list(self.inventory), ["base"])
        self.assertFalse((self.builder.records / "gold.json").exists())

    def test_prepared_record_refuses_suspended_vm_and_does_not_clobber(self):
        entry = self.root / "browser.json"
        entry.write_text(json.dumps({"node": "/n", "entry": "/e", "entry_sha256": "a" * 64,
                                     "engines": {"webkit": prepare.WEBKIT_REVISION}}))
        native = self.root / "codex.json"
        native.write_text(json.dumps({"bin": "/c", "version": codex.VERSION, "binary": "/c",
                                      "binary_sha256": "b" * 64}))
        path = self.root / "base.json"
        path.write_text("operator's previous record")
        prepared = {"ssh_host_key_fp": self.fp, "provenance": {}}
        args = {"codex_info": native, "browser_info": entry, "prepared": prepared}
        with mock.patch.object(image, "tart_state", return_value=dict(vm("base"), State="suspended")):
            with self.assertRaisesRegex(ValueError, "must be stopped"):
                image.record("base", self.root, **args)
        with mock.patch.object(image, "tart_state", return_value=vm("base")):
            with self.assertRaises(FileExistsError):
                image.record("base", self.root, **args)
        self.assertEqual(path.read_text(), "operator's previous record")

    def test_install_recipe_reaches_webkit_in_zsh(self):
        homebrew = self.root / "homebrew"
        modules = homebrew / "lib/node_modules"
        playwright = modules / "@playwright/mcp/node_modules/.bin/playwright"
        playwright.parent.mkdir(parents=True)
        playwright.write_text('#!/bin/sh\n[ "$*" = "install webkit" ] || exit 1\nprintf "webkit installed\\n"\n')
        playwright.chmod(0o700)
        stubs = """
            sudo() {
                case "$2" in
                    tee) cat >/dev/null ;;
                    sshd)
                        if [ "$3" = -T ]; then
                            printf 'passwordauthentication no\nkbdinteractiveauthentication no\n'
                        fi ;;
                esac
            }
            node() { return 0; }
            tmux() { return 0; }
            codex() { printf 'codex-cli %s\n' "$CODEX_FIXTURE"; }
            npm() {
                case "$1" in
                    view) printf 'sha512-test\n' ;;
                    root) printf '%s\n' "$MODULES_FIXTURE" ;;
                    install) return 0 ;;
                    *) return 1 ;;
                esac
            }
        """
        recipe = (prepare.PKG / "scripts/prepare_codex.sh").read_text().split('CODEX_INTEGRITY="$codex_integrity"', 1)[0]
        recipe = recipe.replace("/opt/homebrew", str(homebrew)).replace("/usr/sbin/sshd", "sshd")
        run = subprocess.run(
            ["/bin/zsh", "-f", "-s", "--", "http://192.0.2.1:8888", codex.VERSION,
             prepare.MCP_VERSION, prepare.WEBKIT_REVISION],
            input=stubs + recipe, capture_output=True, text=True, timeout=10,
            env={**os.environ, "CODEX_FIXTURE": codex.VERSION, "MODULES_FIXTURE": str(modules)},
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("webkit installed", run.stderr)


class InstallProxyTest(unittest.TestCase):
    def setUp(self):
        listener = mock.Mock()
        listener.getsockname.return_value = ("127.0.0.1", 4321)
        patch = mock.patch.object(egress.socket, "create_server", return_value=listener)
        patch.start()
        self.addCleanup(patch.stop)

    def test_install_switch_never_reads_stdin_keychain_or_creates_ca(self):
        with tempfile.TemporaryDirectory() as out, contextlib.redirect_stdout(io.StringIO()) as stdout, \
                mock.patch("sys.stdin", mock.Mock(readline=mock.Mock(side_effect=AssertionError))), \
                mock.patch.object(egress, "keychain_token", side_effect=AssertionError), \
                mock.patch.object(egress, "google_keychain_token", side_effect=AssertionError, create=True), \
                mock.patch.object(egress, "make_ca", side_effect=AssertionError), \
                mock.patch.object(egress.Egress, "serve_forever", lambda instance: instance.close()):
            egress.main(["--listen", "127.0.0.1", "--guest", "127.0.0.1", "--install-only", "--log", out + "/log"])
        self.assertEqual(list(json.loads(stdout.getvalue())), ["port"])

    def test_install_proxy_refuses_service_hosts_wrong_port_and_other_clients(self):
        with tempfile.TemporaryDirectory() as out:
            dial = mock.Mock(side_effect=AssertionError)
            proxy = egress.Egress("127.0.0.1", "127.0.0.1", egress.INSTALL_HOSTS, (), None, None, None,
                                  pathlib.Path(out) / "log", dial=dial, install_only=True)
            self.addCleanup(proxy.close)
            for target, ip, reply in (
                ("chatgpt.com:443", "127.0.0.1", b"403"),
                ("api.anthropic.com:443", "127.0.0.1", b"403"),
                ("registry.npmjs.org:8443", "127.0.0.1", b"403"),
                ("registry.npmjs.org:443", "192.0.2.3", b""),
            ):
                with self.subTest(target=target, ip=ip):
                    client = mock.Mock()
                    client.recv.return_value = f"CONNECT {target} HTTP/1.1\r\n\r\n".encode()
                    proxy.client(client, (ip, 12345))
                    if reply:
                        self.assertIn(reply, client.sendall.call_args.args[0])
                    else:
                        client.sendall.assert_not_called()
            dial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
