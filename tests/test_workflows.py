"""Structure and safety checks for .github/workflows/pipeline.yml and scan-pass.yml.

PyYAML is not a dependency, so a tiny reader for the YAML subset these files use (block
mappings and sequences, ``|`` block scalars, ``[a, b]`` / ``{}`` flow values, comments) turns
them into dicts and lists. The checks mirror docs/PLAN.md §2 and §9 and the check_repo lint.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"
sys.path.insert(0, str(ROOT / "tools"))
import check_repo  # noqa: E402

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
PRIVATE_REDIRECT = '3>&1 1>>"$GPCLEAN_PRIVATE_DIR/run.log" 2>&1'
MAIN = "github.ref == 'refs/heads/main'"
# The log upload runs even after a failed step, but never with a token the scope check
# rejected (the check fails closed: nothing else touches Drive with a wrongly scoped token).
UPLOAD_IF = "${{ always() && steps.scope.outcome == 'success' }}"


# ------------------------------------------------------------------ a tiny YAML reader


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(text: str):
    text = text.strip()
    if text == "{}":
        return {}
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(part) for part in text[1:-1].split(",") if part.strip()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _split_key(text: str) -> tuple[str, str] | None:
    m = re.match(r"""^("[^"]*"|'[^']*'|[^\s:'"][^:]*?)\s*:(?:\s+(.*)|$)""", text)
    if not m:
        return None
    return _scalar(m.group(1)), (m.group(2) or "")


def parse_yaml(text: str):
    """Parse the YAML subset of this repo's workflows into dicts/lists/scalars."""
    raw = text.split("\n")
    lines: list[tuple[int, str, int]] = []  # (indent, content, raw index)
    for idx, line in enumerate(raw):
        content = _strip_comment(line)
        if content.strip():
            lines.append((len(content) - len(content.lstrip()), content.strip(), idx))
    pos = 0

    def block_scalar(parent_indent: int) -> str:
        nonlocal pos
        body = []
        while pos < len(lines) and lines[pos][0] > parent_indent:
            body.append(raw[lines[pos][2]])
            pos += 1
        return "\n".join(body)

    def value_after(rest: str, indent: int):
        nonlocal pos
        if rest in ("|", ">", "|-", ">-"):
            return block_scalar(indent)
        if rest:
            return _scalar(rest)
        if pos < len(lines) and (lines[pos][0] > indent
                                 or (lines[pos][0] == indent and lines[pos][1].startswith("- "))):
            return node(lines[pos][0])
        return None

    def mapping(indent: int, first: tuple[str, str] | None = None) -> dict:
        nonlocal pos
        out: dict = {}
        if first is not None:
            key, rest = first
            out[key] = value_after(rest, indent)
        while pos < len(lines) and lines[pos][0] == indent and not lines[pos][1].startswith("- "):
            _, content, raw_idx = lines[pos]
            kv = _split_key(content)
            assert kv is not None, f"cannot parse line {raw_idx + 1}"
            pos += 1
            out[kv[0]] = value_after(kv[1], indent)
        return out

    def sequence(indent: int) -> list:
        nonlocal pos
        out = []
        while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("- "):
            item = lines[pos][1][2:].strip()
            pos += 1
            kv = _split_key(item)
            if kv is None:
                out.append(_scalar(item))
            else:
                out.append(mapping(indent + 2, kv))
        return out

    def node(indent: int):
        if lines[pos][1].startswith("- "):
            return sequence(indent)
        return mapping(indent)

    result = node(0)
    assert pos == len(lines), "unparsed trailing lines"
    return result


def load(name: str) -> tuple[str, dict]:
    text = (WF / name).read_text(encoding="utf-8")
    return text, parse_yaml(text)


@pytest.fixture(scope="module")
def pipeline():
    return load("pipeline.yml")


@pytest.fixture(scope="module")
def scan_pass():
    return load("scan-pass.yml")


def test_reader_handles_the_subset():
    doc = parse_yaml("a: 1\nb:\n  - x: '2'\n    y: [p, \"q\"]\n  - z\n"
                     "c: |\n  echo hi # not a comment\nd: {}\n")
    assert doc == {"a": 1, "b": [{"x": "2", "y": ["p", "q"]}, "z"],
                   "c": "  echo hi # not a comment", "d": {}}


# ----------------------------------------------------------------------- shared rules


def step_jobs(doc: dict) -> dict:
    return {name: job for name, job in doc["jobs"].items() if "steps" in job}


def uses_secret(job: dict) -> bool:
    return any("secrets." in str(v) for s in job["steps"] for v in (s.get("env") or {}).values())


@pytest.mark.parametrize("name", ["pipeline.yml", "scan-pass.yml"])
def test_check_repo_lint_passes(name):
    path = f".github/workflows/{name}"
    data = (WF / name).read_bytes()
    assert check_repo.check_file(path, "100644", data) == []


@pytest.mark.parametrize("name", ["pipeline.yml", "scan-pass.yml"])
def test_lint_would_catch_an_expression_in_run(name):
    text = (WF / name).read_text(encoding="utf-8")
    bad = text.replace("run: rm -rf \"$RUNNER_TEMP/rc\"",
                       "run: rm -rf \"$RUNNER_TEMP/rc\" ${{ inputs.folder }}", 1)
    assert bad != text
    assert any("expression inside run" in p for p in check_repo.lint_workflow(name, bad))


@pytest.mark.parametrize("name", ["pipeline.yml", "scan-pass.yml"])
def test_common_safety_rules(name):
    text, doc = load(name)
    code = "\n".join(_strip_comment(line) for line in text.split("\n"))  # comments may say it
    assert doc["permissions"] == {}
    assert doc["defaults"] == {"run": {"shell": "bash"}}
    assert doc["env"]["GPCLEAN_PUBLIC_FD"] == "3"
    for forbidden in ("secrets: inherit", "actions/cache", "upload-artifact",
                      "download-artifact", "pull_request", "id-token", "set -x", "--dump",
                      "ACTIONS_STEP_DEBUG", "uv run"):
        assert forbidden not in code
    assert not re.search(r":\s*write\b", code)
    # The selftest's shard size is hashed into cfg, so it must not be a per-file workflow
    # value that could drift between pipeline.yml and scan-pass.yml (it is a code default).
    for knob in ("GPCLEAN_PHOTOS_PER_SHARD", "GPCLEAN_SELFTEST_FAIL_SHARD"):
        assert knob not in code

    for job_name, job in step_jobs(doc).items():
        assert job["runs-on"] == "ubuntu-24.04", job_name
        assert job["permissions"] == {"contents": "read"}, job_name
        assert isinstance(job.get("timeout-minutes"), int), job_name
        steps = job["steps"]
        assert steps[0]["uses"] == CHECKOUT
        assert steps[0]["with"] == {"persist-credentials": False}
        assert sum(1 for s in steps if "uses" in s) == 1  # checkout is the only action
        runs = [s["run"] for s in steps if "run" in s]
        for run in runs:
            assert "${{" not in run.replace("${{ matrix.", ""), job_name
        assert any("bash tools/install_tools.sh" in r for r in runs)
        assert any("uv sync --locked" in r for r in runs)
        # Jobs holding the Drive token get no dev tools (pytest) and never the mcp group.
        for run in runs:
            if "uv sync" in run:
                assert run.startswith("uv sync --locked --no-dev"), job_name
                assert "mcp" not in run, job_name
        for run in runs:
            if "-m gpclean" in run:
                assert run.startswith(".venv/bin/python -m gpclean "), job_name
                if "ci-upload-logs" in run:
                    assert "3>/dev/null" in run and run.rstrip().endswith("|| true")
                else:
                    assert run.rstrip().endswith(PRIVATE_REDIRECT), job_name
        if not uses_secret(job):
            assert "environment" not in job, job_name
            continue
        # Every job that touches Drive: the environment, the decode step, the scope check,
        # then always-steps that upload the private logs and delete the credentials.
        assert job["environment"] == "photos", job_name
        decode = [s for s in steps if "RCLONE_CONFIG_B64" in (s.get("env") or {})]
        assert len(decode) == 1
        assert decode[0]["env"] == {"RCLONE_CONFIG_B64": "${{ secrets.RCLONE_CONFIG_B64 }}"}
        body = decode[0]["run"]
        assert [line.strip() for line in body.splitlines()][:4] == [
            "umask 077",
            'mkdir -p "$RUNNER_TEMP/rc"',
            """printf '%s' "$RCLONE_CONFIG_B64" | base64 -d > "$RUNNER_TEMP/rc/rclone.conf\"""",
            'chmod 600 "$RUNNER_TEMP/rc/rclone.conf"',
        ]
        after = steps[steps.index(decode[0]) + 1]
        assert "ci-scope-check" in after["run"], job_name
        assert after["id"] == "scope", job_name
        assert steps[-2]["if"] == UPLOAD_IF and "ci-upload-logs" in steps[-2]["run"]
        uploads = [s for s in steps if "ci-upload-logs" in s.get("run", "")]
        assert uploads == [steps[-2]], job_name
        # Every other step after the scope check needs it to have succeeded (the default).
        for s in steps[steps.index(after) + 1:-2]:
            assert "always()" not in str(s.get("if", "")), job_name
        # A Drive job never runs for a ref other than main (A-4): either its own if: says
        # so, or it needs a job that does (checked per file below).
        assert steps[-1] == {"name": steps[-1]["name"], "if": "always()",
                             "run": 'rm -rf "$RUNNER_TEMP/rc"'}
        # The secret appears only in the decode step.
        assert code.count("secrets.") == len(
            [j for j in step_jobs(doc).values() if uses_secret(j)])


# --------------------------------------------------------------------------- pipeline


def test_pipeline_trigger_and_inputs(pipeline):
    text, doc = pipeline
    assert list(doc["on"]) == ["workflow_dispatch"]
    inputs = doc["on"]["workflow_dispatch"]["inputs"]
    assert inputs["folder"]["type"] == "string" and inputs["folder"]["default"] == "Takeout"
    assert inputs["threshold"]["type"] == "choice"
    assert inputs["threshold"]["options"] == ["2", "3", "4", "5"]
    assert inputs["threshold"]["default"] == "3"
    assert inputs["include_albums"] == {**inputs["include_albums"], "type": "boolean",
                                        "default": False}
    assert inputs["mode"]["options"] == ["full", "probe", "merge_only", "selftest"]
    assert inputs["clip_model"]["options"] == ["b32", "b16", "none"]
    assert inputs["workers"]["options"] == [str(i) for i in range(1, 9)]
    assert inputs["workers"]["default"] == "6"
    assert "${{" not in doc["run-name"]
    assert doc["concurrency"] == {"group": "gpclean", "cancel-in-progress": False}
    # Inputs reach code through env only.
    for key in ("folder", "threshold", "include_albums", "mode", "clip_model", "workers"):
        assert doc["env"][f"GPCLEAN_{key.upper()}"] == "${{ inputs.%s }}" % key


def test_pipeline_job_graph(pipeline):
    _text, doc = pipeline
    jobs = doc["jobs"]
    assert set(jobs) == {"probe", "plan", "pass1", "pass2", "pass3", "merge", "report"}
    assert jobs["probe"]["if"] == f"${{{{ {MAIN} && inputs.mode == 'probe' }}}}"
    assert jobs["plan"]["if"] == f"${{{{ {MAIN} && inputs.mode != 'probe' }}}}"
    assert jobs["plan"]["environment"] == "photos"
    for n in (1, 2, 3):
        job = jobs[f"pass{n}"]
        assert job["uses"] == "./.github/workflows/scan-pass.yml"
        assert job["permissions"] == {"contents": "read"}
        assert "environment" not in job and "secrets" not in job
        assert job["with"]["pass"] == n
    assert jobs["pass1"]["needs"] == "plan"
    assert jobs["pass1"]["if"] == "${{ inputs.mode != 'merge_only' && inputs.mode != 'probe' }}"
    for n in (2, 3):
        prev = f"pass{n - 1}"
        assert jobs[f"pass{n}"]["needs"] == prev
        assert jobs[f"pass{n}"]["if"] == (
            f"${{{{ !cancelled() && needs.{prev}.outputs.done == 'false' && "
            f"needs.{prev}.outputs.quota == 'false' && inputs.mode != 'merge_only' }}}}")
    merge = jobs["merge"]
    assert merge["needs"] == ["plan", "pass1", "pass2", "pass3"]
    assert merge["if"] == ("${{ !cancelled() && needs.plan.result == 'success' && "
                           "inputs.mode != 'probe' }}")
    assert merge["environment"] == "photos"
    report = jobs["report"]
    assert report["needs"] == ["plan", "pass1", "pass2", "pass3", "merge"]
    # A probe dispatched from another branch still gets a (red) report.
    assert report["if"] == ("${{ !cancelled() && (inputs.mode != 'probe' || "
                            "github.ref != 'refs/heads/main') }}")
    assert "environment" not in report
    assert "ci-report" in report["steps"][-1]["run"]
    assert report["env"]["GPCLEAN_QUOTA3"] == "${{ needs.pass3.outputs.quota }}"
    # A quota stop or skipped zips in the plan reach the report too.
    assert jobs["plan"]["outputs"]["quota"] == "${{ steps.plan.outputs.quota }}"
    assert jobs["plan"]["outputs"]["bad_zips"] == "${{ steps.plan.outputs.bad_zips }}"
    assert report["env"]["GPCLEAN_QUOTA_PLAN"] == "${{ needs.plan.outputs.quota }}"
    assert report["env"]["GPCLEAN_BAD_ZIPS"] == "${{ needs.plan.outputs.bad_zips }}"


def test_pipeline_commands(pipeline):
    _text, doc = pipeline
    jobs = doc["jobs"]

    def cmds(job):
        return [re.search(r"-m gpclean (\S+)", s["run"]).group(1)
                for s in jobs[job]["steps"] if "-m gpclean" in s.get("run", "")]

    assert cmds("probe") == ["ci-scope-check", "probe", "ci-upload-logs"]
    assert cmds("plan") == ["ci-scope-check", "selftest-upload", "ci-plan", "ci-upload-logs"]
    assert cmds("merge") == ["ci-scope-check", "ci-merge", "ci-upload-logs"]
    assert cmds("report") == ["ci-report"]
    selftest = [s for s in jobs["plan"]["steps"] if "selftest-upload" in s.get("run", "")][0]
    assert selftest["if"] == "${{ inputs.mode == 'selftest' }}"
    assert any("--group clip" in s.get("run", "") for s in jobs["probe"]["steps"])
    assert jobs["plan"]["outputs"]["n_shards"] == "${{ steps.plan.outputs.n_shards }}"
    assert jobs["merge"]["outputs"]["partial"] == "${{ steps.merge.outputs.partial }}"


# -------------------------------------------------------------------------- scan-pass


def test_scan_pass_interface(scan_pass):
    _text, doc = scan_pass
    assert list(doc["on"]) == ["workflow_call"]
    call = doc["on"]["workflow_call"]
    assert call["inputs"]["pass"]["type"] == "number"
    assert set(call["inputs"]) == {"pass", "folder", "include_albums", "mode", "clip_model",
                                   "workers"}
    assert call["outputs"]["done"]["value"] == "${{ jobs.finalize.outputs.done }}"
    assert call["outputs"]["quota"]["value"] == "${{ jobs.finalize.outputs.quota }}"
    assert doc["env"]["GPCLEAN_PASS"] == "${{ inputs.pass }}"


def test_scan_pass_jobs(scan_pass):
    _text, doc = scan_pass
    jobs = doc["jobs"]
    assert set(jobs) == {"pending", "scan", "finalize"}
    for job in jobs.values():
        assert job["environment"] == "photos"
    pending = jobs["pending"]
    assert set(pending["outputs"]) == {"count", "of", "workers", "ids", "quota"}
    scan = jobs["scan"]
    assert scan["needs"] == "pending"
    assert scan["if"] == "${{ needs.pending.outputs.count != '0' }}"
    assert scan["continue-on-error"] is True
    assert scan["timeout-minutes"] == 350
    assert scan["strategy"] == {"fail-fast": False, "matrix": {
        "worker": "${{ fromJSON(needs.pending.outputs.workers) }}"}}
    assert scan["env"]["GPCLEAN_WORKER"] == "${{ matrix.worker }}"
    assert any("--group clip" in s.get("run", "") for s in scan["steps"])
    run = [s["run"] for s in scan["steps"] if "ci-scan" in s.get("run", "")][0]
    assert '--worker "$GPCLEAN_WORKER" --of "$GPCLEAN_OF"' in run
    assert pending["if"] == f"${{{{ {MAIN} }}}}"
    final = jobs["finalize"]
    assert final["needs"] == ["pending", "scan"]
    assert final["if"] == f"${{{{ !cancelled() && {MAIN} }}}}"
    # When pending itself stops on a quota no worker writes a flag; finalize must know.
    assert final["env"]["GPCLEAN_PENDING_QUOTA"] == "${{ needs.pending.outputs.quota }}"
    assert final["outputs"] == {"done": "${{ steps.finalize.outputs.done }}",
                                "quota": "${{ steps.finalize.outputs.quota }}"}


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _guarded_jobs(jobs: dict) -> set[str]:
    """Jobs that can run only for refs/heads/main.

    A job is guarded when its own if: checks the ref, or when it needs a guarded job and
    either has no status function in its if: (then a skipped need skips it too) or its if:
    requires that need to have run (its result is success, or it set an output; a skipped
    job sets none).
    """
    guarded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name in guarded:
                continue
            cond = str(job.get("if", ""))
            ok = MAIN in cond
            ups = [n for n in _needs(job) if n in guarded]
            if not ok and ups:
                if "always()" in cond or "!cancelled()" in cond:
                    ok = any(re.search(
                        rf"needs\.{n}\.(result == 'success'|outputs\.\w+ == '\w+')", cond)
                        for n in ups)
                else:
                    ok = True
            if ok:
                guarded.add(name)
                changed = True
    return guarded


def test_every_drive_job_is_guarded_to_main(pipeline, scan_pass):
    """A dispatch from a feature branch never runs code with the Drive token (A-4)."""
    for (_text, doc) in (pipeline, scan_pass):
        jobs = doc["jobs"]
        guarded = _guarded_jobs(jobs)
        for name, job in jobs.items():
            if job.get("environment") == "photos" or "uses" in job:
                assert name in guarded, name
    # The reusable workflow's own jobs check the ref too (defence in depth).
    assert scan_pass[1]["jobs"]["pending"]["if"] == f"${{{{ {MAIN} }}}}"
    # The guard itself is what the check relies on: dropping it is caught.
    jobs = dict(pipeline[1]["jobs"])
    jobs["plan"] = {**jobs["plan"], "if": "${{ inputs.mode != 'probe' }}"}
    assert "merge" not in _guarded_jobs(jobs) and "pass1" not in _guarded_jobs(jobs)


@pytest.mark.parametrize("name", ["pipeline.yml", "scan-pass.yml"])
def test_log_upload_waits_for_a_good_scope(name):
    _text, doc = load(name)
    n = 0
    for job in step_jobs(doc).values():
        for step in job["steps"]:
            if "ci-upload-logs" in step.get("run", ""):
                assert step["if"] == UPLOAD_IF
                n += 1
    assert n == 3
