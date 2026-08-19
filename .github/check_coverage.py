#!/usr/bin/env python3
# check_coverage.py -- report-only: every non-desktop release/arch a
# builder publishes must be covered by the anyvm test workflows, and every
# DEFAULT_BUILDER_VERSIONS pin must point at an existing builder release
# tag. Intentional gaps are declared in .github/coverage.allow
# ("<os> <tag|*> <workflow>  # reason").
#
# The release list comes from the builder's OWN release asset at the
# version anyvm pins --
#   https://github.com/anyvm-org/<os>-builder/releases/download/v<ver>/releases.json
# -- generated from conf/ by base-builder's gendata.py and uploaded by that
# builder's build.yml release-index job. Never a branch (raw.github-
# usercontent/main) and never releases/latest: the index and the images it
# describes must be the matched pair from one pinned release.
#
#   python3 .github/check_coverage.py [--tree /path/to/anyvm-org]
#
# --tree reads each builder's working-copy releases.json instead, for
# checking a change before anything is pushed or released.
#
# Exit codes: 0 covered, 1 coverage findings, 2 fetch errors. A pinned
# release with no releases.json asset (one cut before this mechanism
# existed) prints SKIP and does not fail; it self-heals on that builder's
# next release.

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(REPO_ROOT, ".github", "workflows")
ALLOW_PATH = os.path.join(REPO_ROOT, ".github", "coverage.allow")
ANYVM_PATH = os.path.join(REPO_ROOT, "anyvm.py")

SHARED_WORKFLOWS = ["test.yml", "testmacos.yml", "testwindows.yml"]
MATRIX_KEYS = {"os", "release", "arch"}


class FetchError(Exception):
    pass


def fetch(url, token=None):
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url)
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:
            last = e
        time.sleep(5)
    raise FetchError("%s: %s" % (url, last))


DEFAULT_BUILDER_VERSIONS_BLOCK_RE = re.compile(
    r"DEFAULT_BUILDER_VERSIONS\s*=\s*\{(.*?)\}", re.S)
VERSIONS_RE = re.compile(r'"([a-z0-9_-]+)"\s*:\s*"([^"]+)"')


def default_builder_versions():
    with open(ANYVM_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    m = DEFAULT_BUILDER_VERSIONS_BLOCK_RE.search(text)
    if not m:
        sys.exit("check_coverage: DEFAULT_BUILDER_VERSIONS not found")
    versions = dict(VERSIONS_RE.findall(m.group(1)))
    if len(versions) < 10:
        sys.exit("check_coverage: DEFAULT_BUILDER_VERSIONS parse "
                  "suspiciously small (%d)" % len(versions))
    return versions


def release_index_url(os_name, version):
    # The index is a release asset of the SAME builder release the images
    # come from, addressed by the version anyvm pins -- never a moving
    # branch (raw.githubusercontent/main) and never releases/latest.
    return ("https://github.com/portsbuild-vm/%s-builder/releases/download/"
            "v%s/releases.json" % (os_name, version))


def builder_releases(os_name, version, tree=None):
    if tree:
        path = os.path.join(tree, "%s-builder" % os_name,
                            ".github", "data", "releases.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(
            fetch(release_index_url(os_name, version)).decode("utf-8"))
    return [(r["release"], r["arch"]) for r in data["releases"]
            if not r["desktop"]]


def norm_arch(value):
    if not isinstance(value, str) or not value:
        return "x86_64"
    return value


def job_combos(job):
    covered = set()
    matrix = ((job.get("strategy") or {}).get("matrix")) or {}
    releases = matrix.get("release")
    if isinstance(releases, list) and releases:
        arches = matrix.get("arch")
        if not (isinstance(arches, list) and arches):
            arches = [""]
        combos = set()
        for r in releases:
            for a in arches:
                combos.add((str(r), norm_arch(a)))
        for exc in matrix.get("exclude") or []:
            if not isinstance(exc, dict) or set(exc) - MATRIX_KEYS:
                # an exclude naming another axis (sync, runs) removes
                # only some variants, not the release/arch coverage
                continue
            er, ea = exc.get("release"), exc.get("arch")
            combos = set(
                (r, a) for (r, a) in combos
                if not ((er is None or str(er) == r)
                        and (ea is None or norm_arch(ea) == a)))
        covered |= combos
    for inc in (matrix.get("include") or []):
        if isinstance(inc, dict) and "release" in inc:
            covered.add((str(inc["release"]), norm_arch(inc.get("arch"))))
    w = job.get("with") or {}
    rel = w.get("release")
    if rel is not None and "${{" not in str(rel):
        arch = w.get("arch")
        if not isinstance(arch, str) or "${{" in arch:
            arch = ""
        covered.add((str(rel), norm_arch(arch)))
    return covered


def load_workflow(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def workflow_combos(path):
    covered = set()
    for job in (load_workflow(path).get("jobs") or {}).values():
        if isinstance(job, dict):
            covered |= job_combos(job)
    return covered


def workflow_os_lists(path):
    lists = {}
    for name, job in (load_workflow(path).get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        matrix = ((job.get("strategy") or {}).get("matrix")) or {}
        oses = matrix.get("os")
        if isinstance(oses, list):
            lists[name] = [str(o) for o in oses]
    return lists


def load_allow(path=ALLOW_PATH):
    allow = set()
    if not os.path.exists(path):
        return allow
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 3:
                sys.exit("check_coverage: bad allow line: %r" % line)
            allow.add(tuple(parts))
    return allow


def allowed(allow, os_name, tag, workflow):
    return ((os_name, tag, workflow) in allow
            or (os_name, "*", workflow) in allow)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", help="local anyvm-org tree root (read "
                    "releases.json from disk instead of the release asset)")
    args = ap.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    versions = default_builder_versions()
    allow = load_allow()
    shared = {}
    for wf in SHARED_WORKFLOWS:
        shared[wf] = workflow_os_lists(os.path.join(WF_DIR, wf))
    findings, fetch_errors, skipped = [], [], []
    for os_name in sorted(versions):
        wf = os_name + ".yml"
        wf_path = os.path.join(WF_DIR, wf)
        try:
            releases = builder_releases(os_name, versions[os_name], args.tree)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # The pinned release predates the release-index asset (or the
                # tag is missing -- the tag check below reports that as a
                # finding). Skip this builder's release coverage rather than
                # failing: it self-heals when that builder cuts its next
                # release, or when the index is uploaded to the pinned tag.
                skipped.append(
                    "%s: v%s carries no releases.json asset, release "
                    "coverage not checked" % (os_name, versions[os_name]))
            else:
                fetch_errors.append("%s releases.json: HTTP %s"
                                    % (os_name, e.code))
            releases = []
        except (FetchError, OSError, KeyError, ValueError) as e:
            fetch_errors.append("%s releases.json: %s" % (os_name, e))
            releases = []
        if not os.path.exists(wf_path):
            if not allowed(allow, os_name, "*", wf):
                findings.append("%s: workflow %s missing" % (os_name, wf))
        elif releases:
            covered = workflow_combos(wf_path)
            for rel, arch in releases:
                tag = rel if arch == "x86_64" else "%s-%s" % (rel, arch)
                if ((rel, arch) not in covered
                        and not allowed(allow, os_name, tag, wf)):
                    findings.append("%s: %s missing from %s matrix"
                                    % (os_name, tag, wf))
        for wf2 in SHARED_WORKFLOWS:
            for jobname, oses in shared[wf2].items():
                if os_name not in oses and not allowed(allow, os_name, "*", wf2):
                    findings.append("%s: missing from %s job %s os list"
                                    % (os_name, wf2, jobname))
        url = ("https://api.github.com/repos/portsbuild-vm/%s-builder/"
               "releases/tags/v%s" % (os_name, versions[os_name]))
        try:
            fetch(url, token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                findings.append(
                    "%s: DEFAULT_BUILDER_VERSIONS pin v%s has no release tag"
                    % (os_name, versions[os_name]))
            else:
                fetch_errors.append("%s tag check: HTTP %s"
                                    % (os_name, e.code))
        except FetchError as e:
            fetch_errors.append(str(e))
    for f in findings:
        print("FINDING: " + f)
    for e in fetch_errors:
        print("FETCH-ERROR: " + e)
    for s in skipped:
        print("SKIP: " + s)
    if not findings and not fetch_errors:
        if skipped:
            print("coverage: no gaps found (%d builder(s) skipped, see SKIP)"
                  % len(skipped))
        else:
            print("coverage: all builder releases covered")
    if fetch_errors:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
