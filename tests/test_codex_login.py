"""Synthetic tests for reusing a developer's host Codex login."""
import base64
import contextlib
import ctypes
import hashlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cuvm import egress


ACCOUNT = "synthetic-cuvm-workspace"
AUTH_CLAIM = "https://api.openai.com/auth"
NOW = 1_800_000_000
SWAP_HOST = "chatgpt.com"
DUMMY = "cuvm-test-guest-token-not-a-login"
REFRESH = "synthetic-refresh-that-must-never-leave-egress"


def jwt(claims):
    payload = json.dumps(claims).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{encoded}." + "s" * 80


def auth(exp=NOW + 3600, account=ACCOUNT, mode="chatgpt", fedramp=False):
    token = jwt({"exp": exp, "sub": "test-only"})
    identity = jwt({AUTH_CLAIM: {"chatgpt_account_id": account, "chatgpt_account_is_fedramp": fedramp}})
    return {"auth_mode": mode, "last_refresh": "2027-01-15T00:00:00Z",
            "tokens": {"access_token": token, "id_token": identity, "account_id": account, "refresh_token": REFRESH}}


class NativeCodexLoginTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = pathlib.Path(temp.name).resolve()
        self.start(mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True))
        self.start(mock.patch.object(egress, "codex_check_managed_auth"))
        self.start(mock.patch.object(egress.time, "time", return_value=NOW))

    def start(self, patch):
        obj = patch.start()
        self.addCleanup(patch.stop)
        return obj

    def config(self, text):
        (self.home / "config.toml").write_text(text)

    def save(self, credential):
        path = self.home / "auth.json"
        path.write_text(json.dumps(credential))
        path.chmod(0o600)

    def test_default_file_reuses_the_current_session(self):
        first = auth()
        self.save(first)
        with mock.patch.object(egress, "codex_keychain_auth") as keychain:
            self.assertEqual(egress.codex_native_login(), (first["tokens"]["access_token"], ACCOUNT))
            second = auth(exp=NOW + 7200)
            self.save(second)
            self.assertEqual(egress.codex_native_login(), (second["tokens"]["access_token"], ACCOUNT))
        keychain.assert_not_called()

    def test_keyring_uses_the_default_user_keychain_and_exact_native_entry(self):
        self.config('cli_auth_credentials_store = "keyring"')
        stored = auth()
        digest = hashlib.sha256(os.fsencode(self.home)).hexdigest()[:16]
        alias = self.home / "symlink"
        alias.symlink_to(self.home)
        with mock.patch.object(egress, "codex_read_keychain_item", return_value=json.dumps(stored)) as read, \
                mock.patch.object(egress.threading, "Timer") as timer, \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(alias)}):
            self.assertEqual(egress.codex_native_login(), (stored["tokens"]["access_token"], ACCOUNT))
        read.assert_called_once_with(b"Codex Auth", f"cli|{digest}".encode())
        timer.assert_called_once_with(30, egress.codex_keychain_timeout)
        timer.return_value.cancel.assert_called_once()

    def test_auto_falls_back_only_when_the_exact_item_is_missing(self):
        self.config('cli_auth_credentials_store = "auto"')
        stored = auth()
        self.save(stored)
        with mock.patch.object(egress, "codex_read_keychain_item", return_value=None):
            self.assertEqual(egress.codex_native_login(), (stored["tokens"]["access_token"], ACCOUNT))
        with mock.patch.object(egress, "codex_read_keychain_item", side_effect=OSError(REFRESH)):
            with self.assertRaisesRegex(egress.CodexLoginError, "Keychain") as error:
                egress.codex_native_login()
        self.assertNotIn(REFRESH, str(error.exception))

    def test_rejects_expired_and_non_chatgpt_credentials(self):
        for settings in ({"exp": NOW}, {"exp": True}, {"exp": None}, {"mode": "apikey"}, {"account": REFRESH},
                         {"account": "wrong\nheader"}, {"fedramp": True}):
            with self.subTest(settings=settings):
                self.save(auth(**settings))
                with self.assertRaises(egress.CodexLoginError) as error:
                    egress.codex_native_login()
                self.assertNotIn(REFRESH, str(error.exception))

    def test_invalid_json_is_not_disclosed(self):
        (self.home / "auth.json").write_text(REFRESH)
        with self.assertRaises(egress.CodexLoginError) as error:
            egress.codex_native_login()
        self.assertNotIn(REFRESH, str(error.exception))

    def test_duplicate_auth_mode_is_rejected(self):
        serialized = json.dumps(auth())
        (self.home / "auth.json").write_text('{"auth_mode":"apikey",' + serialized[1:])
        with self.assertRaisesRegex(egress.CodexLoginError, "unreadable"):
            egress.codex_native_login()

    def test_native_malformed_session_envelopes_are_rejected(self):
        no_timestamp = auth()
        del no_timestamp["last_refresh"]
        no_refresh_field = auth()
        del no_refresh_field["tokens"]["refresh_token"]
        for credential in (no_timestamp, no_refresh_field, dict(auth(), last_refresh="2026-01-01"),
                           dict(auth(mode=None), OPENAI_API_KEY=""),
                           dict(auth(mode=None), personal_access_token="")):
            with self.subTest(credential=credential):
                self.save(credential)
                with self.assertRaises(egress.CodexLoginError):
                    egress.codex_native_login()

    def test_workspace_switch_with_retained_id_token_is_refused_in_both_directions(self):
        for prior_fedramp, new_fedramp in ((False, True), (True, False)):
            with self.subTest(prior_fedramp=prior_fedramp):
                stale = auth(fedramp=prior_fedramp)
                other = "synthetic-cuvm-other-workspace"
                stale["tokens"]["account_id"] = other
                stale["tokens"]["access_token"] = jwt({"exp": NOW + 3600,
                    AUTH_CLAIM: {"chatgpt_account_id": other, "chatgpt_account_is_fedramp": new_fedramp}})
                self.save(stale)
                with self.assertRaisesRegex(egress.CodexLoginError, "workspace"):
                    egress.codex_native_login()

    def test_fedramp_access_claim_cannot_be_overridden_by_the_id_token(self):
        stored = auth()
        stored["tokens"]["access_token"] = jwt({"exp": NOW + 3600,
            AUTH_CLAIM: {"chatgpt_account_id": ACCOUNT, "chatgpt_account_is_fedramp": True}})
        self.save(stored)
        with self.assertRaisesRegex(egress.CodexLoginError, "workspace"):
            egress.codex_native_login()

    def test_persisted_custom_oauth_issuer_is_refused(self):
        stored = auth()
        provenance = {"issuer": "https://auth.openai.com", "client_id": "synthetic-client"}
        stored["tokens"]["oauth_provenance"] = provenance
        self.save(stored)
        self.assertEqual(egress.codex_native_login(), (stored["tokens"]["access_token"], ACCOUNT))
        provenance["issuer"] = "https://other.example"
        self.save(stored)
        with self.assertRaisesRegex(egress.CodexLoginError, "custom authentication"):
            egress.codex_native_login()

    def test_missing_auth_reports_how_to_sign_in(self):
        with self.assertRaisesRegex(egress.CodexLoginError, "codex login"):
            egress.codex_native_login()

    def test_dangling_config_does_not_select_the_default_file_store(self):
        self.save(auth())
        (self.home / "config.toml").symlink_to("missing-config")
        with self.assertRaisesRegex(egress.CodexLoginError, "configuration is unreadable"):
            egress.codex_native_login()

    def test_unsupported_storage_is_rejected_before_opening_auth(self):
        for config in ('cli_auth_credentials_store = "ephemeral"',
                       'cli_auth_credentials_store = "auto"\n[features]\nsecret_auth_storage = true'):
            with self.subTest(config=config):
                self.config(config)
                with mock.patch.object(egress, "codex_keychain_auth") as read:
                    with self.assertRaises(egress.CodexLoginError):
                        egress.codex_native_login()
                read.assert_not_called()

    def test_auto_refuses_stale_plaintext_when_encrypted_store_exists(self):
        self.config('cli_auth_credentials_store = "auto"')
        self.save(auth())
        (self.home / "codex_auth.age").touch()
        with mock.patch.object(egress, "codex_keychain_auth") as keychain:
            with self.assertRaisesRegex(egress.CodexLoginError, "storage"):
                egress.codex_native_login()
        keychain.assert_not_called()

    def test_custom_config_is_rejected_without_a_store_read(self):
        self.save(auth())
        for config in ('not a config', 'forced_login_method = "api"', 'forced_chatgpt_workspace_id = "elsewhere"',
                       'chatgpt_base_url = "https://other.example/"', 'model_provider = "custom"', 'profile = "test"',
                       '[model_providers.openai]\nbase_url = "https://other.example/"'):
            with self.subTest(config=config):
                self.config(config)
                with mock.patch.object(egress, "codex_keychain_auth") as keychain:
                    with self.assertRaises(egress.CodexLoginError):
                        egress.codex_native_login()
                keychain.assert_not_called()

    def test_proxy_publishes_only_the_account_id(self):
        stored = auth()
        supplemental = "sk-synthetic-unused-browser-login-api-key"
        stored["OPENAI_API_KEY"] = supplemental
        self.save(stored)
        allow = self.home / "allow.txt"
        allow.write_text(SWAP_HOST + "\n")
        args = ["--listen", "127.0.0.1", "--guest", "127.0.0.1", "--allow", str(allow),
                "--dir", str(self.home / "ca"), "--log", str(self.home / "log"),
                "--token-format", "jwt", "--swap-host", SWAP_HOST, "--codex-native-auth"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch("sys.stdin", io.StringIO(DUMMY)), \
                mock.patch.object(egress, "keychain_token", side_effect=AssertionError), \
                mock.patch.object(egress, "Egress") as server:
            server.return_value.port = 1234
            server.return_value.ca_pem = self.home / "ca.pem"
            egress.main(args)
        line = json.loads(output.getvalue())
        self.assertEqual(set(line), {"port", "ca", "published"})
        self.assertEqual(line["published"], ACCOUNT)
        self.assertNotIn(stored["tokens"]["access_token"], output.getvalue())
        self.assertNotIn(REFRESH, output.getvalue())
        self.assertNotIn(supplemental, output.getvalue())
        self.assertEqual(server.call_args.args[3], [SWAP_HOST])
        self.assertEqual(server.call_args.args[5], stored["tokens"]["access_token"])

    def test_native_proxy_rejects_incompatible_options_before_reading_credentials(self):
        common = ["--listen", "127.0.0.1", "--guest", "127.0.0.1", "--log", "unused", "--codex-native-auth"]
        credentials = ["--allow", "unused", "--dir", "unused", "--token-format", "jwt"]
        for args in (["--install-only"], credentials + ["--swap-host", "other.example"],
                     credentials + ["--swap-host", SWAP_HOST, "--keychain-item", "old"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(egress, "codex_native_login") as read:
                with self.assertRaises(SystemExit) as error:
                    egress.main(common + args)
                self.assertEqual(error.exception.code, 2)
                read.assert_not_called()


class NativeKeychainTest(unittest.TestCase):
    def test_native_lookup_uses_the_user_domain_and_reports_exact_absence(self):
        data = '{"fixture":"Unicode é"}'.encode()
        fixture = ctypes.create_string_buffer(data)
        for status in (0, -25300, -25308):
            with self.subTest(status=status):
                security = mock.Mock()
                foundation = mock.Mock()

                def open_default(domain, out):
                    self.assertEqual(domain, 0)
                    ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = 1
                    return 0

                def find(keychain, size, service, account_size, account, length, content, item):
                    self.assertEqual((keychain.value, size, service, account_size, account, item),
                                     (1, 10, b"Codex Auth", 5, b"cli|1", None))
                    if status == 0:
                        ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(data)
                        ctypes.cast(content, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(fixture)
                    return status

                security.SecKeychainCopyDomainDefault.side_effect = open_default
                security.SecKeychainFindGenericPassword.side_effect = find
                with mock.patch.object(egress, "core_foundation", return_value=foundation), \
                        mock.patch.object(ctypes, "CDLL", return_value=security):
                    if status == -25308:
                        with self.assertRaisesRegex(egress.CodexLoginError, "Keychain"):
                            egress.codex_read_keychain_item(b"Codex Auth", b"cli|1")
                    else:
                        result = egress.codex_read_keychain_item(b"Codex Auth", b"cli|1")
                        self.assertEqual(result, data.decode() if status == 0 else None)
                self.assertEqual(security.SecKeychainItemFreeContent.call_count, int(status == 0))
                foundation.CFRelease.assert_called_once()

    def test_watchdog_emits_only_the_fixed_diagnostic(self):
        with contextlib.redirect_stderr(io.StringIO()) as diagnostics, mock.patch.object(os, "_exit") as exit:
            egress.codex_keychain_timeout()
        exit.assert_called_once_with(1)
        self.assertEqual(diagnostics.getvalue(), "egress: " + egress.CODEX_LOGIN_MESSAGES["keychain"] + "\n")

    def test_managed_policy_is_refused(self):
        for files, forced in ((True, False), (False, True), (False, OSError(REFRESH))):
            with self.subTest(files=files, forced=forced):
                with mock.patch.object(egress, "exists_for_login", return_value=files), \
                        mock.patch.object(egress, "codex_forced_preferences", side_effect=[forced]):
                    with self.assertRaisesRegex(egress.CodexLoginError, "managed policy") as error:
                        egress.codex_check_managed_auth()
                self.assertNotIn(REFRESH, str(error.exception))

    def test_macos_checks_exact_forced_preference_keys_and_releases_strings(self):
        foundation = mock.Mock()
        foundation.CFStringCreateWithCString.side_effect = [10, 20, 30]
        foundation.CFPreferencesAppValueIsForced.side_effect = [False, True]
        with mock.patch.object(egress.sys, "platform", "darwin"), \
                mock.patch.object(egress, "core_foundation", return_value=foundation):
            self.assertTrue(egress.codex_forced_preferences())
        self.assertEqual([call.args[1] for call in foundation.CFStringCreateWithCString.call_args_list],
                         [b"com.openai.codex", b"config_toml_base64", b"requirements_toml_base64"])
        self.assertEqual(foundation.CFPreferencesAppValueIsForced.call_args_list,
                         [mock.call(20, 10), mock.call(30, 10)])
        self.assertEqual(foundation.CFRelease.call_args_list, [mock.call(10), mock.call(20), mock.call(30)])


if __name__ == "__main__":
    unittest.main()
