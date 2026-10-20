# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Tests for .github/workflows/strix.yaml.

The gate and report scripts are taken from the workflow file itself and run
under node against a fake GitHub API (tests/strix_harness.js), so the tests
exercise exactly what ships.
"""

import json
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/strix.yaml").read_text())
INPUTS = WORKFLOW[True]["workflow_call"]["inputs"]
JOBS = WORKFLOW["jobs"]
HARNESS = ROOT / "tests/strix_harness.js"
HEAD = "a" * 40
BASE = "b" * 40
LAST = "c" * 40
# The decryption command documented in README.md for maintainers.
DECRYPT = (
    "openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000"
    " -pass env:STRIX_REPORT_KEY"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def step(job, name=None):
    steps = JOBS[job]["steps"]
    return next(s for s in steps if name is None or s.get("name") == name)


def run_script(job, env, state):
    script = step(job)["with"]["script"]
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"script": script, "env": env, "state": state}),
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(proc.stdout)
    assert "error" not in result, result.get("error")
    return result


def called(result, name):
    return [c["params"] for c in result["calls"] if c["name"] == name]


def gate(env, state):
    base_env = {
        "MODE": "pr",
        "PR_NUMBER": "7",
        "FORCE": "false",
        "SKIP_PATHS": INPUTS["skip-paths"]["default"],
    }
    return run_script("gate", {**base_env, **env}, state)


def pr(state="open", fork=False):
    return {
        "number": 7,
        "state": state,
        "head": {
            "sha": HEAD,
            "repo": {"full_name": "someone/repo" if fork else "org/repo"},
        },
        "base": {"sha": BASE},
    }


# --- gate: pull requests -----------------------------------------------------


def test_gate_scans_a_code_change():
    r = gate(
        {}, {"pr": pr(), "files": ["roles/x/tasks/main.yaml", "README.md"]}
    )
    assert r["outputs"]["scan"] == "true"
    assert r["outputs"]["head_sha"] == HEAD
    assert r["outputs"]["base_sha"] == BASE


@pytest.mark.parametrize(
    "case,state",
    [
        ("fork", {"pr": pr(fork=True), "files": ["a.py"]}),
        ("closed", {"pr": pr(state="closed"), "files": ["a.py"]}),
        (
            "docs only",
            {
                "pr": pr(),
                "files": [
                    "docs/a.md",
                    "README.rst",
                    "doc/source/conf.rst",
                    "LICENSE",
                ],
            },
        ),
        (
            "already passed",
            {
                "pr": pr(),
                "files": ["a.py"],
                "statuses": [{"context": "strix", "state": "success"}],
            },
        ),
        (
            "already failed",
            {
                "pr": pr(),
                "files": ["a.py"],
                "statuses": [{"context": "strix", "state": "failure"}],
            },
        ),
    ],
)
def test_gate_skips(case, state):
    r = gate({}, state)
    assert r["outputs"]["scan"] == "false", case
    assert "head_sha" not in r["outputs"] or case.startswith("already")


def test_gate_fork_check_needs_no_file_listing():
    r = gate({}, {"pr": pr(fork=True), "files": ["a.py"]})
    assert called(r, "pulls.listFiles") == []


def test_gate_without_pr_number_skips():
    r = gate({"PR_NUMBER": ""}, {})
    assert r["outputs"]["scan"] == "false"


@pytest.mark.parametrize(
    "statuses",
    [
        [{"context": "strix", "state": "error"}],
        [{"context": "strix", "state": "pending"}],
        [{"context": "other", "state": "success"}],
    ],
)
def test_gate_rescans_after_error_or_unrelated_status(statuses):
    r = gate({}, {"pr": pr(), "files": ["a.py"], "statuses": statuses})
    assert r["outputs"]["scan"] == "true"


def test_gate_force_rescans_a_scanned_commit():
    r = gate(
        {"FORCE": "true"},
        {
            "pr": pr(),
            "files": ["a.py"],
            "statuses": [{"context": "strix", "state": "success"}],
        },
    )
    assert r["outputs"]["scan"] == "true"


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "Dockerfile",
        ".github/workflows/x.yml",
        "docs-tool/main.py",
    ],
)
def test_gate_does_not_skip_security_relevant_files(path):
    r = gate({}, {"pr": pr(), "files": [path]})
    assert r["outputs"]["scan"] == "true", path


def test_gate_reports_visibility():
    assert (
        gate({}, {"pr": pr(), "files": ["a.py"], "private": True})["outputs"][
            "private"
        ]
        == "true"
    )
    assert (
        gate({}, {"pr": pr(), "files": ["a.py"]})["outputs"]["private"]
        == "false"
    )


# --- gate: nightly -----------------------------------------------------------


def tracking_issue(sha=LAST):
    return {
        "number": 5,
        "body": "<!-- strix-nightly-tracking -->\n"
        f"<!-- strix-last-scanned: {sha} -->",
    }


def nightly(state):
    return gate(
        {"MODE": "nightly", "PR_NUMBER": ""}, {"branchSha": HEAD, **state}
    )


def test_nightly_first_run_records_a_baseline_without_scanning():
    r = nightly({"issues": []})
    assert r["outputs"]["scan"] == "false"
    assert r["outputs"]["advance"] == "true"
    assert r["outputs"]["head_sha"] == HEAD
    assert r["outputs"]["issue_number"] == ""


def test_nightly_no_new_commits():
    r = nightly({"issues": [tracking_issue(HEAD)]})
    assert r["outputs"]["scan"] == "false"
    assert "advance" not in r["outputs"]


def test_nightly_scans_new_commits_from_the_last_scanned_one():
    r = nightly(
        {
            "issues": [tracking_issue()],
            "compare": {
                "status": "ahead",
                "ahead_by": 3,
                "files": [{"filename": "a.py"}],
            },
        }
    )
    assert r["outputs"]["scan"] == "true"
    assert r["outputs"]["base_sha"] == LAST
    assert r["outputs"]["issue_number"] == "5"


def test_nightly_ignores_unrelated_issues():
    other = {"number": 9, "body": "<!-- strix-last-scanned: " + LAST + " -->"}
    r = nightly({"issues": [other]})
    assert r["outputs"]["advance"] == "true"
    assert r["outputs"]["issue_number"] == ""


@pytest.mark.parametrize(
    "compare",
    [
        {
            "status": "ahead",
            "ahead_by": 1,
            "files": [{"filename": "docs/a.md"}],
        },
        {"status": "diverged", "ahead_by": 1, "files": [{"filename": "a.py"}]},
        "error",
    ],
)
def test_nightly_advances_without_scanning(compare):
    r = nightly({"issues": [tracking_issue()], "compare": compare})
    assert r["outputs"]["scan"] == "false"
    assert r["outputs"]["advance"] == "true"


# --- report ------------------------------------------------------------------


def report(env, state=None):
    base_env = {
        "MODE": "pr",
        "PR_NUMBER": "7",
        "FAIL_ON": "high",
        "SCANNED": "true",
        "SCAN_RESULT": "success",
        "RC": "0",
        "SUMMARY": "",
        "HEAD_SHA": HEAD,
        "BASE_SHA": BASE,
        "PRIVATE": "false",
        "ISSUE_NUMBER": "",
    }
    return run_script("report", {**base_env, **env}, state or {})


def summary(counts, findings=None):
    s = {"counts": counts, "total": sum(counts.values()), "report": True}
    if findings is not None:
        s["findings"] = findings
    return json.dumps(s)


@pytest.mark.parametrize(
    "rc,state",
    [("0", "success"), ("2", "failure"), ("1", "error"), ("", "error")],
)
def test_report_sets_the_commit_status(rc, state):
    r = report({"RC": rc})
    (status,) = called(r, "repos.createCommitStatus")
    assert status["state"] == state
    assert status["context"] == "strix"
    assert status["sha"] == HEAD
    assert len(status["description"]) <= 140


def test_report_public_repo_publishes_counts_only():
    r = report(
        {
            "RC": "2",
            "SUMMARY": summary(
                {"high": 1, "low": 2},
                findings=[{"severity": "high", "title": "SQLi in x"}],
            ),
        }
    )
    (comment,) = called(r, "issues.createComment")
    assert "1 high, 2 low" in comment["body"]
    assert "SQLi" not in comment["body"]
    assert "<!-- strix-report -->" in comment["body"]


def test_report_private_repo_lists_findings():
    r = report(
        {
            "RC": "2",
            "PRIVATE": "true",
            "SUMMARY": summary(
                {"high": 1},
                findings=[{"severity": "high", "title": "SQLi in x"}],
            ),
        }
    )
    (comment,) = called(r, "issues.createComment")
    assert "**high** SQLi in x" in comment["body"]


def test_report_updates_its_own_comment_and_releases_the_label():
    mine = {
        "id": 11,
        "user": {"type": "Bot"},
        "body": "<!-- strix-report -->\nold",
    }
    human = {
        "id": 12,
        "user": {"type": "User"},
        "body": "<!-- strix-report --> quoted by a person",
    }
    r = report({}, {"comments": [human, mine]})
    assert [c["comment_id"] for c in called(r, "issues.updateComment")] == [11]
    assert called(r, "issues.createComment") == []
    assert called(r, "issues.removeLabel")[0]["name"] == "strix-scan"


def test_report_tolerates_a_missing_label():
    report({}, {"labelMissing": True})


def nightly_report(env, state=None):
    return report({"MODE": "nightly", "PR_NUMBER": "", **env}, state)


def test_nightly_report_creates_the_tracking_issue_on_baseline():
    r = nightly_report({"SCANNED": "false", "RC": ""})
    (issue,) = called(r, "issues.create")
    assert f"<!-- strix-last-scanned: {HEAD} -->" in issue["body"]
    assert issue["labels"] == ["strix-nightly"]
    assert called(r, "issues.createComment") == []
    assert called(r, "repos.createCommitStatus") == []


def test_nightly_report_advances_after_a_completed_scan():
    r = nightly_report(
        {
            "ISSUE_NUMBER": "5",
            "BASE_SHA": LAST,
            "RC": "2",
            "SUMMARY": summary({"high": 1}),
        }
    )
    (update,) = called(r, "issues.update")
    assert f"<!-- strix-last-scanned: {HEAD} -->" in update["body"]
    (comment,) = called(r, "issues.createComment")
    assert comment["issue_number"] == 5
    assert LAST[:8] in comment["body"] and HEAD[:8] in comment["body"]


def test_nightly_report_keeps_the_marker_when_the_scan_errored():
    r = nightly_report({"ISSUE_NUMBER": "5", "BASE_SHA": LAST, "RC": "1"})
    assert called(r, "issues.update") == []
    assert len(called(r, "issues.createComment")) == 1


# --- scan job steps ----------------------------------------------------------


def run_summary(tmp_path, private, vulns):
    if vulns is not None:
        run_dir = tmp_path / "strix_runs" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "vulnerabilities.json").write_text(json.dumps(vulns))
    out = tmp_path / "out"
    out.write_text("")
    subprocess.run(
        ["python3", "-c", step("scan", "Summarise findings")["run"]],
        cwd=tmp_path,
        check=True,
        env={**os.environ, "PRIVATE": private, "GITHUB_OUTPUT": str(out)},
    )
    line = out.read_text().strip()
    assert line.startswith("summary=")
    return json.loads(line.removeprefix("summary="))


def test_summary_counts_by_severity(tmp_path):
    vulns = [
        {"severity": "HIGH", "title": "a"},
        {"severity": "high", "title": "b"},
        {"severity": "low", "title": "c"},
    ]
    s = run_summary(tmp_path, "false", vulns)
    assert s == {"counts": {"high": 2, "low": 1}, "total": 3, "report": True}


def test_summary_private_includes_titles(tmp_path):
    s = run_summary(
        tmp_path, "true", [{"severity": "critical", "title": "RCE"}]
    )
    assert s["findings"] == [{"severity": "critical", "title": "RCE"}]


def test_summary_without_report(tmp_path):
    assert run_summary(tmp_path, "false", None) == {
        "counts": {},
        "total": 0,
        "report": False,
    }


@pytest.mark.parametrize("rc,code", [("0", 0), ("2", 1), ("1", 1), ("", 1)])
def test_result_step_exit_codes(rc, code):
    proc = subprocess.run(
        ["bash", "-c", step("scan", "Result")["run"]],
        env={**os.environ, "RC": rc, "FAIL_ON": "high"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == code
    if rc == "2":
        assert "findings at or above high" in proc.stdout
    if code and rc != "2":
        assert "scanner error" in proc.stdout


@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl not installed"
)
def test_encrypted_report_round_trip(tmp_path):
    (tmp_path / "strix_runs" / "r").mkdir(parents=True)
    (tmp_path / "strix_runs" / "r" / "vulnerabilities.json").write_text(
        '[{"title": "secret finding"}]'
    )
    console = pathlib.Path("/tmp/strix-console.log")
    console.write_text("console")
    out = tmp_path / "out"
    out.write_text("")
    env = {
        **os.environ,
        "REPORT_KEY": "test-passphrase",
        "GITHUB_OUTPUT": str(out),
    }
    subprocess.run(
        [
            "bash",
            "-c",
            step("scan", "Encrypt the report (public repository)")["run"],
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    encrypted = pathlib.Path("/tmp/strix-report.tar.gz.enc")
    assert b"secret finding" not in encrypted.read_bytes()
    readme = (ROOT / "README.md").read_text()
    assert DECRYPT in readme
    dest = tmp_path / "restore"
    dest.mkdir()
    subprocess.run(
        [
            "bash",
            "-c",
            f"{DECRYPT} -in {encrypted} | tar -xz",
        ],
        cwd=dest,
        env={**os.environ, "STRIX_REPORT_KEY": "test-passphrase"},
        check=True,
    )
    assert (
        "secret finding"
        in (dest / "strix_runs/r/vulnerabilities.json").read_text()
    )
    encrypted.unlink()
    console.unlink()


def test_encrypt_without_key_keeps_nothing(tmp_path):
    out = tmp_path / "out"
    out.write_text("")
    proc = subprocess.run(
        [
            "bash",
            "-c",
            step("scan", "Encrypt the report (public repository)")["run"],
        ],
        cwd=tmp_path,
        env={**os.environ, "REPORT_KEY": "", "GITHUB_OUTPUT": str(out)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.read_text() == ""
    assert "not kept" in proc.stdout
