"""Tests for the v0.2 surface: jury engine, stats, standalone CLI (no live models)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Never touch the real ~/.local/state from tests.
os.environ["CROSSCHECK_STATE_DIR"] = tempfile.mkdtemp(prefix="cc-test-state-")

from crosscheck import cli, config, diff, engine, gate, providers, stats  # noqa: E402
from crosscheck.providers import ProviderError, Reviewer  # noqa: E402
from crosscheck.review import Finding  # noqa: E402


def F(sev="warn", cat="bug", file="a.py", line=10, summary="off by one in loop", detail="", who=()):
    return Finding(sev, cat, file, line, summary, detail, list(who))


class _Env:
    """Temporarily set/unset environment variables."""

    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def fake_providers(outputs, fail=()):
    """Patch providers.resolve/run: each juror name returns outputs[name]."""
    saved = providers.resolve, providers.run

    def resolve(cfg):
        name = cfg.provider if cfg.provider != "auto" else "codex"
        if name in fail:
            raise ProviderError("%s not installed" % name)
        return Reviewer(name=name, argv=[name], sandboxed=True)

    def run(rev, prompt, timeout):
        out = outputs[rev.name]
        if isinstance(out, Exception):
            raise out
        return out

    providers.resolve, providers.run = resolve, run
    try:
        yield
    finally:
        providers.resolve, providers.run = saved


def _findings_json(*items):
    return json.dumps({"findings": [dict(severity=s, category=c, file=f, line=l, summary=m, detail="")
                                    for s, c, f, l, m in items]})


class TestJuryMerge(unittest.TestCase):
    def test_same_issue_from_two_jurors_merges(self):
        merged = engine.merge([
            ("codex", [F(line=10, summary="off by one in loop bound")]),
            ("gemini", [F(line=11, summary="loop bound is off by one", sev="blocker")]),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].reviewers, ["codex", "gemini"])
        self.assertEqual(merged[0].severity, "blocker")  # highest severity wins

    def test_different_files_or_far_lines_stay_separate(self):
        merged = engine.merge([
            ("codex", [F(file="a.py", line=10)]),
            ("gemini", [F(file="b.py", line=10), F(file="a.py", line=80, cat="perf",
                                                    summary="slow query")]),
        ])
        self.assertEqual(len(merged), 3)

    def test_juror_never_merges_with_itself(self):
        merged = engine.merge([("codex", [F(line=10), F(line=11)])])
        self.assertEqual(len(merged), 2)

    def test_no_line_needs_similar_text(self):
        merged = engine.merge([
            ("codex", [F(line=None, summary="sql injection in search query")]),
            ("gemini", [F(line=None, summary="unrelated naming nit", cat="style")]),
        ])
        self.assertEqual(len(merged), 2)

    def test_agreed_findings_sort_first(self):
        merged = engine.merge([
            ("codex", [F(file="z.py", line=1, sev="blocker", summary="alpha")]),
            ("gemini", [F(file="a.py", line=5, summary="beta gamma"),
                        F(file="a.py", line=5, summary="x")]),
            ("claude", [F(file="a.py", line=6, summary="beta gamma delta")]),
        ])
        self.assertGreater(len(merged[0].reviewers), 1)

    def test_path_prefixes_normalized(self):
        merged = engine.merge([("codex", [F(file="./a.py")]), ("gemini", [F(file="b/a.py")])])
        self.assertEqual(len(merged), 1)


class TestVerdictQuorum(unittest.TestCase):
    def test_quorum_filters_single_juror_findings(self):
        v = engine.Verdict(reviewers=["codex", "gemini"], findings=[
            F(who=["codex", "gemini"]), F(file="b.py", who=["codex"])])
        self.assertEqual(len(v.blocking("warn", 1)), 2)
        self.assertEqual(len(v.blocking("warn", 2)), 1)

    def test_quorum_capped_by_jurors_that_answered(self):
        v = engine.Verdict(reviewers=["codex"], findings=[F(who=["codex"])])
        self.assertEqual(len(v.blocking("warn", 3)), 1)

    def test_threshold_applies(self):
        v = engine.Verdict(reviewers=["codex"], findings=[F(sev="nit", who=["codex"])])
        self.assertEqual(v.blocking("warn"), [])
        self.assertEqual(len(v.blocking("nit")), 1)


class TestRunReview(unittest.TestCase):
    DIFF = "diff --git a/a.py b/a.py\n@@\n+x = 1\n"

    def test_jury_runs_all_and_merges(self):
        out = _findings_json(("warn", "bug", "a.py", 1, "x is wrong value here"))
        cfg = config.Config(jury=["codex", "gemini"])
        with fake_providers({"codex": out, "gemini": out}):
            v = engine.run_review(self.DIFF, cfg, lambda s: None)
        self.assertEqual(sorted(v.reviewers), ["codex", "gemini"])
        self.assertEqual(len(v.findings), 1)
        self.assertEqual(len(v.findings[0].reviewers), 2)

    def test_partial_failure_keeps_the_rest(self):
        out = _findings_json(("warn", "bug", "a.py", 1, "bad"))
        cfg = config.Config(jury=["codex", "gemini"])
        with fake_providers({"codex": out}, fail=("gemini",)):
            v = engine.run_review(self.DIFF, cfg, lambda s: None)
        self.assertEqual(v.reviewers, ["codex"])
        self.assertTrue(any("gemini" in f for f in v.failures))

    def test_unparseable_juror_is_a_failure_not_a_pass(self):
        out = _findings_json(("warn", "bug", "a.py", 1, "bad"))
        cfg = config.Config(jury=["codex", "gemini"])
        with fake_providers({"codex": out, "gemini": "no json here"}):
            v = engine.run_review(self.DIFF, cfg, lambda s: None)
        self.assertEqual(v.reviewers, ["codex"])
        self.assertEqual(len(v.failures), 1)

    def test_all_fail_raises(self):
        cfg = config.Config(jury=["codex", "gemini"])
        with fake_providers({"codex": ProviderError("boom")}, fail=("gemini",)):
            with self.assertRaises(ProviderError):
                engine.run_review(self.DIFF, cfg, lambda s: None)

    def test_duplicate_jurors_run_once(self):
        calls = []
        cfg = config.Config(jury=["auto", "codex"])
        with fake_providers({"codex": _findings_json()}):
            orig = providers.run
            providers.run = lambda r, p, t: calls.append(r.name) or orig(r, p, t)
            engine.run_review(self.DIFF, cfg, lambda s: None)
        self.assertEqual(calls, ["codex"])

    def test_secrets_redacted_both_ways_per_juror(self):
        secret = "sk-abcdefghijklmnop1234567890"
        seen = []
        out = _findings_json(("blocker", "security", "a.py", 1, "leak " + secret))
        cfg = config.Config(jury=["codex", "gemini"])
        with fake_providers({"codex": out, "gemini": out}):
            orig = providers.run
            providers.run = lambda r, p, t: seen.append(p) or orig(r, p, t)
            v = engine.run_review("diff --git a/a b/a\n+k = '%s'\n" % secret, cfg, lambda s: None)
        self.assertTrue(all(secret not in p for p in seen))
        self.assertNotIn(secret, json.dumps([f.to_dict() for f in v.findings]))


class TestJuryConfigTrust(unittest.TestCase):
    def test_env_sets_jury_and_quorum(self):
        with _Env(CROSSCHECK_JURY="Codex, gemini,codex", CROSSCHECK_QUORUM="2"):
            cfg = config.load(tempfile.mkdtemp())
        self.assertEqual(cfg.jury, ["codex", "gemini"])
        self.assertEqual(cfg.quorum, 2)

    def test_project_file_cannot_set_jury_quorum_or_stats(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".crosscheck.json"), "w") as fh:
            json.dump({"jury": ["codex", "gemini", "claude"], "quorum": 4, "stats": False}, fh)
        with _Env(CROSSCHECK_JURY=None, CROSSCHECK_QUORUM=None, CROSSCHECK_STATS=None):
            cfg = config.load(d)
        self.assertEqual(cfg.jury, [])
        self.assertEqual(cfg.quorum, 1)
        self.assertTrue(cfg.stats)

    def test_jury_size_capped(self):
        self.assertEqual(len(config.parse_jury("a,b,c,d,e,f")), config.MAX_JURY)


class TestDiffSources(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        run = lambda *a: subprocess.run(["git", "-C", self.repo] + list(a), check=True,
                                        capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        with open(os.path.join(self.repo, "a.py"), "w") as fh:
            fh.write("x = 1\n")
        run("add", ".")
        run("commit", "-qm", "init")
        run("checkout", "-qb", "feature")
        with open(os.path.join(self.repo, "a.py"), "w") as fh:
            fh.write("x = 2\n")
        run("commit", "-qam", "change")
        with open(os.path.join(self.repo, "b.py"), "w") as fh:
            fh.write("y = 1\n")
        run("add", "b.py")
        self.run_git = run

    def test_staged_only_sees_index(self):
        out = diff.compute_staged(self.repo, config.Config())
        self.assertIn("b.py", out)
        self.assertNotIn("a.py", out)

    def test_range_sees_branch_commits(self):
        default = subprocess.run(["git", "-C", self.repo, "rev-list", "--max-parents=0", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
        out = diff.compute_range(self.repo, default, config.Config())
        self.assertIn("a.py", out)
        self.assertNotIn("b.py", out)  # staged, not committed

    def test_range_rejects_option_like_refs(self):
        for bad in ("-oops", "--output=/tmp/x", "a b", ""):
            with self.assertRaises(ValueError):
                diff.compute_range(self.repo, bad, config.Config())

    def test_range_unknown_ref_is_an_error(self):
        with self.assertRaises(ValueError):
            diff.compute_range(self.repo, "no-such-branch", config.Config())

    def test_prepare_filters_and_summarize_counts(self):
        raw = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n+a\n+b\n-c\n"
               "diff --git a/yarn.lock b/yarn.lock\n+zzz\n")
        out = diff.prepare(raw, config.Config())
        self.assertNotIn("yarn.lock", out)
        self.assertEqual(diff.summarize(out), (1, 2, 1))


class TestStats(unittest.TestCase):
    def setUp(self):
        self._env = _Env(CROSSCHECK_STATE_DIR=tempfile.mkdtemp())
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__()

    def test_record_and_load_counts_only(self):
        caught = [F(sev="blocker", cat="security", who=["codex", "gemini"],
                    summary="SECRET-TEXT-SHOULD-NOT-PERSIST")]
        stats.record(caught + [F(sev="nit", who=["codex"])], caught, ["codex", "gemini"], True)
        stats.record([], [], ["codex"], False)
        data = stats.load()
        self.assertEqual((data["reviews"], data["blocked"], data["caught"], data["agreed"]), (2, 1, 1, 1))
        self.assertEqual(data["by_category"], {"security": 1})
        self.assertEqual(data["by_reviewer"], {"codex": 1, "gemini": 1})
        with open(stats.path()) as fh:
            self.assertNotIn("SECRET-TEXT", fh.read())
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.dirname(stats.path())).st_mode & 0o777, 0o700)

    def test_corrupt_or_hostile_file_degrades(self):
        os.makedirs(os.path.dirname(stats.path()), exist_ok=True)
        with open(stats.path(), "w") as fh:
            fh.write('{"caught": "lots", "reviews": -5, "by_category": {"x": "y", "bug": 3}}')
        data = stats.load()
        self.assertEqual(data["caught"], 0)
        self.assertEqual(data["reviews"], 0)
        self.assertEqual(data["by_category"], {"bug": 3})
        with open(stats.path(), "w") as fh:
            fh.write("not json")
        self.assertEqual(stats.load()["caught"], 0)

    def test_badge_and_svg(self):
        data = stats.empty()
        data["caught"] = 37
        data["by_category"] = {"<script>": 1}
        self.assertIn("37%20issues%20caught", stats.badge_markdown(data))
        svg = stats.svg_card(data)
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("<script>", svg)

    def test_record_never_raises(self):
        with _Env(CROSSCHECK_STATE_DIR="/proc/definitely/not/writable"):
            stats.record([F()], [F()], ["codex"], True)


def _cli(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    saved_in = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(stdin.encode()))
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
    finally:
        sys.stdin = saved_in
    return rc, out.getvalue(), err.getvalue()


class TestCLI(unittest.TestCase):
    DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n+x = 1\n"

    def setUp(self):
        self._env = _Env(CROSSCHECK_STATE_DIR=tempfile.mkdtemp(), NO_COLOR="1",
                         CROSSCHECK_JURY=None, CROSSCHECK_PROVIDER=None)
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__()

    def test_blocking_findings_exit_1_and_count_in_stats(self):
        out = _findings_json(("blocker", "bug", "a.py", 1, "x must not be 1"))
        with fake_providers({"codex": out}):
            rc, stdout, _ = _cli(["-"], self.DIFF)
        self.assertEqual(rc, 1)
        self.assertIn("x must not be 1", stdout)
        self.assertEqual(stats.load()["caught"], 1)

    def test_clean_pass_exit_0(self):
        with fake_providers({"codex": '{"findings": [], "verdict": "pass"}'}):
            rc, stdout, _ = _cli(["review", "-"], self.DIFF)
        self.assertEqual(rc, 0)
        self.assertIn("passed", stdout)

    def test_json_output_with_jury(self):
        out = _findings_json(("warn", "bug", "a.py", 1, "x is the wrong value"))
        with fake_providers({"codex": out, "gemini": out}):
            rc, stdout, _ = _cli(["-", "--json", "--jury", "codex,gemini", "--no-stats"], self.DIFF)
        data = json.loads(stdout)
        self.assertEqual(rc, 1)
        self.assertEqual(data["verdict"], "changes-requested")
        self.assertEqual(sorted(data["findings"][0]["reviewers"]), ["codex", "gemini"])
        self.assertTrue(data["findings"][0]["blocking"])
        self.assertEqual(stats.load()["reviews"], 0)  # --no-stats

    def test_quorum_flag(self):
        out_a = _findings_json(("warn", "bug", "a.py", 1, "only codex sees this"))
        with fake_providers({"codex": out_a, "gemini": '{"findings": []}'}):
            rc, _, _ = _cli(["-", "--jury", "codex,gemini", "--quorum", "2"], self.DIFF)
        self.assertEqual(rc, 0)

    def test_reviewer_error_fail_open_vs_strict(self):
        with fake_providers({}, fail=("codex",)):
            self.assertEqual(_cli(["-"], self.DIFF)[0], 0)
            self.assertEqual(_cli(["-", "--strict"], self.DIFF)[0], 2)

    def test_empty_diff_is_a_pass(self):
        rc, _, err = _cli(["-"], "")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to review", err)

    def test_stray_positional_is_usage_error(self):
        self.assertEqual(_cli(["review", "main"])[0], 2)

    def test_stats_views(self):
        self.assertEqual(_cli(["stats"])[0], 0)
        rc, out, _ = _cli(["stats", "--badge"])
        self.assertIn("img.shields.io", out)
        svg = os.path.join(tempfile.mkdtemp(), "card.svg")
        self.assertEqual(_cli(["stats", "--svg", svg])[0], 0)
        self.assertTrue(os.path.exists(svg))

    def test_demo_runs_offline_and_records_nothing(self):
        with fake_providers({}, fail=("codex", "gemini")):
            rc, out, _ = _cli(["demo", "--fast"])
        self.assertEqual(rc, 0)
        self.assertIn("2/2 agree", out)
        self.assertEqual(stats.load()["reviews"], 0)

    def test_version(self):
        with self.assertRaises(SystemExit):
            _cli(["--version"])


class TestInstallHook(unittest.TestCase):
    def test_install_refuses_foreign_hook_then_force(self):
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", repo], check=True)
        hook = os.path.join(repo, ".git", "hooks", "pre-commit")
        saved = os.getcwd()
        os.chdir(repo)
        try:
            with open(hook, "w") as fh:
                fh.write("#!/bin/sh\necho mine\n")
            self.assertEqual(_cli(["install-hook"])[0], 2)
            self.assertEqual(_cli(["install-hook", "--force"])[0], 0)
            # Re-installing over our own hook needs no --force.
            self.assertEqual(_cli(["install-hook"])[0], 0)
        finally:
            os.chdir(saved)
        with open(hook) as fh:
            body = fh.read()
        self.assertIn("review --staged", body)
        self.assertTrue(os.access(hook, os.X_OK))


class TestGateJury(unittest.TestCase):
    def test_block_reason_names_agreeing_jurors(self):
        out = _findings_json(("blocker", "bug", "x.py", 1, "x is the wrong value"))
        saved_compute, saved_stdout = diff.compute, sys.stdout
        try:
            diff.compute = lambda cwd, cfg: "diff --git a/x.py b/x.py\n+x=1\n"
            sys.stdout = io.StringIO()
            cfg = config.Config(jury=["codex", "gemini"])
            with _Env(XDG_RUNTIME_DIR=tempfile.mkdtemp()), \
                    fake_providers({"codex": out, "gemini": out}), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = gate._run_gate({}, "/tmp", "jury-session", cfg)
            payload = json.loads(sys.stdout.getvalue())
        finally:
            diff.compute, sys.stdout = saved_compute, saved_stdout
        self.assertEqual(rc, 0)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("jury", payload["reason"])
        self.assertIn("flagged by codex, gemini", payload["reason"])


if __name__ == "__main__":
    unittest.main()
