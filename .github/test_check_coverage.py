import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_coverage as cc


class TestJobCombos(unittest.TestCase):
    def test_matrix_release_arch(self):
        job = {"strategy": {"matrix": {
            "release": ["9.0", "10.0"],
            "arch": ["aarch64", ""],
        }}}
        self.assertEqual(cc.job_combos(job), {
            ("9.0", "aarch64"), ("9.0", "x86_64"),
            ("10.0", "aarch64"), ("10.0", "x86_64")})

    def test_exclude_removes_combo(self):
        job = {"strategy": {"matrix": {
            "release": ["1.0", "2.0"],
            "arch": [""],
            "exclude": [{"release": "1.0"}],
        }}}
        self.assertEqual(cc.job_combos(job), {("2.0", "x86_64")})

    def test_sync_axis_exclude_keeps_coverage(self):
        # an exclude that names another axis (sync) removes only one
        # variant, not the release/arch coverage itself
        job = {"strategy": {"matrix": {
            "release": ["11.0"],
            "arch": [""],
            "sync": ["rsync", "scp"],
            "exclude": [{"release": "11.0", "sync": "rsync"}],
        }}}
        self.assertEqual(cc.job_combos(job), {("11.0", "x86_64")})

    def test_include_adds_combo(self):
        job = {"strategy": {"matrix": {
            "release": ["1.0"],
            "include": [{"release": "3.0", "arch": "sparc64"}],
        }}}
        self.assertEqual(cc.job_combos(job),
                         {("1.0", "x86_64"), ("3.0", "sparc64")})

    def test_literal_with(self):
        job = {"with": {"release": "7.9", "arch": "sparc64"}}
        self.assertEqual(cc.job_combos(job), {("7.9", "sparc64")})

    def test_templated_with_ignored(self):
        job = {"with": {"release": "${{ matrix.release }}"}}
        self.assertEqual(cc.job_combos(job), set())


class TestAllow(unittest.TestCase):
    def test_allow_parse_and_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "coverage.allow")
            with open(path, "w") as f:
                f.write("# comment\n"
                        "netbsd 11.0-sparc64 netbsd.yml  # no rsync\n"
                        "haiku * testwindows.yml\n")
            allow = cc.load_allow(path)
        self.assertTrue(cc.allowed(allow, "netbsd", "11.0-sparc64", "netbsd.yml"))
        self.assertFalse(cc.allowed(allow, "netbsd", "10.0-sparc64", "netbsd.yml"))
        self.assertTrue(cc.allowed(allow, "haiku", "anything", "testwindows.yml"))


class TestVersions(unittest.TestCase):
    def test_parse_default_builder_versions(self):
        versions = cc.default_builder_versions()
        self.assertIn("freebsd", versions)
        self.assertTrue(all(v[0].isdigit() for v in versions.values()))

    def test_default_builder_versions_not_suspiciously_small(self):
        # guards against a silent partial-parse regression: the real
        # anyvm.py has well over a dozen builders pinned.
        versions = cc.default_builder_versions()
        self.assertGreaterEqual(len(versions), 10)

    def test_versions_re_captures_hyphenated_and_underscored_keys(self):
        sample = '"open-bsd": "1.2.3", "free_bsd2": "4.5.6", "haiku": "7"'
        self.assertEqual(
            cc.VERSIONS_RE.findall(sample),
            [("open-bsd", "1.2.3"), ("free_bsd2", "4.5.6"), ("haiku", "7")])


class TestReleaseIndexUrl(unittest.TestCase):
    def test_url_points_at_the_pinned_release_asset(self):
        self.assertEqual(
            cc.release_index_url("freebsd", "2.2.6"),
            "https://github.com/portsbuild-vm/freebsd-builder/releases/download/"
            "v2.2.6/releases.json")

    def test_url_never_uses_a_branch_or_latest(self):
        # the index must be a matched pair with the images of one pinned
        # release -- not a moving branch, not releases/latest.
        for os_name, version in cc.default_builder_versions().items():
            url = cc.release_index_url(os_name, version)
            self.assertNotIn("raw.githubusercontent.com", url)
            self.assertNotIn("releases/latest", url)
            self.assertIn("/releases/download/v%s/" % version, url)


class TestBuilderReleases(unittest.TestCase):
    def test_tree_mode_reads_local_index_and_drops_desktop(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "demo-builder", ".github", "data")
            os.makedirs(d)
            with open(os.path.join(d, "releases.json"), "w") as f:
                json.dump({"os": "demo", "releases": [
                    {"tag": "1.0", "release": "1.0", "arch": "x86_64",
                     "sync": "scp", "desktop": False, "build": True},
                    {"tag": "1.0-aarch64", "release": "1.0",
                     "arch": "aarch64", "sync": "scp", "desktop": False,
                     "build": False},
                    {"tag": "1.0-xfce", "release": "1.0-xfce",
                     "arch": "x86_64", "sync": "scp", "desktop": True,
                     "build": True},
                ]}, f)
            got = cc.builder_releases("demo", "9.9.9", tree=tmp)
        # build:false entries stay (their images exist, just built
        # elsewhere); desktop entries are dropped.
        self.assertEqual(sorted(got), [("1.0", "aarch64"), ("1.0", "x86_64")])


if __name__ == "__main__":
    unittest.main()
