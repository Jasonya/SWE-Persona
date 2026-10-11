"""SWE-specific ports for the shared persona controller and rubric checker."""
import bootstrap
from bootstrap import ROOT, REFERENCE, SCRIPTS
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from checkers import CheckResult
from controller import Controller
from dockerx import Container
from registry import Candidate, harness_trigger, load_persona
from rubric_checker import RubricSpec, grade
from patch_export import untracked_fingerprints, tracked_image_baseline, observed_worktree_tree, export_worktree_prediction
from execution_policy import IndependentSamples

PROTOCOL = "swe-live-multilang-reference-v5"
LEGACY_PROTOCOL = "swe-live-multilang-reference-v4"
FULLSET_PROTOCOL = "swe-live-multilang-fullset-v1"
PERSONA = "manager_no_code"
TEMPLATE = "manager_conclusion_only_delivery"
REVISION = "22091f6ce331c5c60c241d76512d4be7ee1a555b"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# Collection planning is frozen separately from the episode/checker runtime.
# These modules must not be imported by an episode or its scoring path.
COLLECTION_MODULES = {"run_fullset.py", "fullset_protocol.py", "run_qualification.py"}


def source_hashes():
    paths = [p for p in list(REFERENCE.glob("*.py")) + list(Path(__file__).parent.rglob("*.py"))
             if not p.name.startswith("test_") and p.name not in COLLECTION_MODULES
             and "collection" not in p.relative_to(ROOT).parts]
    paths += list((Path(__file__).parent / 'fixtures').glob('*.cjs'))
    paths += [ROOT / "human-interactive-coding-personas.v3.json", SCRIPTS / "patch_export.py",
              SCRIPTS / "run_evaluation.py", SCRIPTS / "runtime_output.py", SCRIPTS / "evaluation_runtime.py",
              SCRIPTS / "protected_patch.py"]
    return {p.relative_to(ROOT).as_posix(): file_hash(p) for p in sorted(paths)}


def collection_source_hashes():
    directory = Path(__file__).parent
    paths = [directory / name for name in COLLECTION_MODULES]
    paths += [p for p in (directory / 'collection').rglob('*.py') if not p.name.startswith('test_')]
    return {p.relative_to(ROOT).as_posix(): file_hash(p) for p in sorted(paths)}


def repository_revisions():
    revisions = {}
    for name, directory in (("team", ROOT), ("swe_bench_live", ROOT / ".local/SWE-bench-Live"),
                            ("repolaunch", ROOT / ".local/SWE-bench-Live/launch")):
        result = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                                capture_output=True, text=True, encoding="utf-8")
        revisions[name] = result.stdout.strip() if result.returncode == 0 else None
    return revisions


def interaction_audit_errors(judge, transcript, checks, events):
    from judge import ITEMS
    scores = (judge or {}).get("scores", [])
    expected = {key for key, _ in ITEMS}
    if len(scores) != len(expected) or {s.get("item") for s in scores} != expected:
        return ["interaction_judge_missing_or_duplicate_items"]
    refs = {m["message_id"] for m in transcript}
    refs |= {c.get("evidence_ref") for c in checks} | {e.get("id") for e in events}
    refs.discard(None)
    errors = []
    for score in scores:
        cited = score.get("evidence_message_ids", [])
        if score.get("score") not in (0, 1, 2) or not cited or not set(cited) <= refs:
            errors.append("interaction_judge_ungrounded:" + score["item"])
    return errors


class Manifest:
    def __init__(self, task_path):
        # JSONL records are separated by LF, not every Unicode line separator.
        # U+0085/U+2028/U+2029 may occur legally inside an unescaped JSON string.
        rows = [json.loads(line) for line in Path(task_path).read_text(encoding="utf-8").split("\n") if line.strip()]
        if len(rows) != 1:
            raise ValueError("Expected exactly one frozen task")
        row = rows[0]
        for field in ("instance_id", "problem_statement", "base_commit", "docker_image"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError("Missing " + field)
        self.task_id, self.instruction = row["instance_id"], row["problem_statement"]
        self.base_commit, self.docker_image = row["base_commit"], row["docker_image"]
        self.task_path = Path(task_path).resolve()
        self.task_sha256 = file_hash(task_path)
        self.deliverables = ["repository patch"]
        self.environment = {"platform": "linux", "workdir": "/testbed", "cpus": 4,
                            "memory": "16g", "network": "none",
                            "permissions": "Writable task repository; no host mounts or host credentials",
                            "tools": "bash shell; preinstalled repository tools"}
        # Guard complete private payloads, not short solution lines which an
        # independent agent could legitimately reproduce. Allowlisting below is
        # the primary boundary; the original row is never stored on this object.
        self._markers = [json.dumps(row[k], ensure_ascii=False) if not isinstance(row[k], str) else row[k]
                         for k in ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS")
                         if row.get(k) and len(str(row[k])) > 80]

    def public_view(self):
        return {"task_id": self.task_id, "source": "SWE-bench-Live/MultiLang@" + REVISION,
                "original_instruction": self.instruction, "environment": self.environment,
                "frozen_contract": {"deliverable": "Patch against the frozen repository commit",
                                    "acceptance": "Original isolated official tests, unchanged"}}

    def frozen_digest(self):
        return digest({"public": self.public_view(), "private_task_sha256": self.task_sha256})

    def assert_no_leak(self, rendered, where):
        if any(marker in rendered for marker in self._markers):
            raise ValueError("Private evaluator material in " + where)


def candidates(persona_id=PERSONA, task_binding=None):
    if task_binding is not None:
        from task_binding import candidate_for
        if task_binding["persona"] != persona_id:
            raise ValueError("Wrong task-bound persona")
        persona, bound = candidate_for(task_binding)
        return persona, bound, coverage_for(bound.template_id)
    if persona_id != PERSONA:
        from personas import PERSONAS, pr_candidate
        if persona_id not in PERSONAS:
            raise ValueError("Unsupported persona")
        persona, bound = pr_candidate()
        return persona, bound, coverage_for(bound.template_id)
    persona = load_persona(PERSONA)
    template = next(t for t in persona["attributes"]["evolved_requirements"] if t["id"] == TEMPLATE)
    if template["bindings"]:
        raise ValueError("Slot-free template changed; revalidate the binder")
    bound = Candidate(
        candidate_id="cand-m1", requirement_id="req.manager_conclusions.01", template_id=TEMPLATE,
        persona_id=PERSONA, bindings={}, template=template,
        checker={"id": "rubric_judge", "version": "1", "parameters": {
            "requirement": "req.manager_conclusions.01@1", "samples": 3,
            "message_clauses": ["met_if[0]"],
            "subcheck": {"id": "deliverable_shape", "version": "1", "parameters": {}}}},
        schedule={"trigger_event": harness_trigger(template), "earliest_turn": 2,
                  "deadline_turn": 4, "priority": 1, "required": True},
        disclosure_markers=["three points"],
        contract_compat="Only constrains replies and evidence-grounded status; original issue and tests stay frozen",
        evidence_refs=["public:original_instruction", "environment:observed repository patch and shell executions"],
    )
    return persona, bound, coverage_for(TEMPLATE)


def coverage_for(template_id):
    return [{"persona": p["id"], "template": t["id"],
                 "status": "bound_pending_validation" if t["id"] == template_id else "not_applicable",
                 "reason": "Preregistered communication/handoff binder" if t["id"] == template_id else
                 "No validated SWE MultiLang binder/checker in this version; not a claim of intrinsic incompatibility"}
                for p in read(ROOT / "human-interactive-coding-personas.v3.json")["personas"]
                for t in p["attributes"]["evolved_requirements"]]


class Runtime(Container):
    """Reference shell interface, with in-container command cleanup on timeout."""
    def __post_init__(self):
        self.commands = []

    def start(self):
        owner = os.environ.get("PERSONACODE_SAMPLE_OWNER")
        # Random per-sample names and ownership labels allow precise timeout cleanup.
        command = ["docker", "run", "-d", "--name", self.name, "--memory", self.memory,
                   "--cpus", self.cpus, "--network", self.network,
                   "--security-opt", "no-new-privileges"]
        if owner:
            command += ["--label", "personacode.sample=" + owner]
        for mount in self.mounts:
            command += mount.arg()
        command += [self.image, "sleep", "infinity"]
        remaining = self.deadline.remaining() if getattr(self, "deadline", None) else 180
        result = subprocess.run(command, capture_output=True, text=True, timeout=min(180, remaining))
        if result.returncode:
            raise RuntimeError("Task container start failed: " + result.stderr)
        return result.stdout.strip()

    def sh(self, script, timeout=900, workdir="/testbed"):
        if not hasattr(self, "commands"):
            self.commands = []
        deadline = getattr(self, "deadline", None)
        is_agent = getattr(self, "command_source", "harness") == "agent"
        enforce = deadline and (is_agent or deadline.phase == "sample")
        if enforce:
            timeout = min(timeout, deadline.remaining())
        started = time.monotonic()
        if is_agent:
            print(f"[shell] command started; timeout={timeout:.1f}s", flush=True)
        wrapped = "timeout --kill-after=1s " + str(max(.001, timeout)) + "s bash -lc " + shlex.quote(script)
        execution_user = vars(self).get('agent_user') if is_agent else None
        command = ["docker", "exec", "-w", workdir]
        if execution_user:
            command += ['--user', execution_user, '--env', 'HOME=/tmp', '--env', 'GIT_CONFIG_COUNT=1',
                        '--env', 'GIT_CONFIG_KEY_0=safe.directory', '--env', 'GIT_CONFIG_VALUE_0=/testbed']
        command += [self.name, "bash", "-lc", wrapped]
        process = subprocess.run(command,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=timeout if enforce and deadline.phase == "sample" else timeout + 30)
        result = process.returncode, process.stdout, process.stderr
        self.commands.append({"command": script, "exit_code": result[0], "stdout": result[1], "stderr": result[2],
                              "source": getattr(self, "command_source", "harness")})
        self.commands[-1]["seconds"] = round(time.monotonic() - started, 3)
        self.commands[-1]['execution_user'] = execution_user or 'root'
        self.commands[-1]['phase'] = 'review' if execution_user else 'implementation'
        self.commands[-1]['agent_turn'] = vars(self).get('agent_turn') if is_agent else None
        if is_agent:
            print(f"[shell] finished in {time.monotonic() - started:.1f}s; exit={result[0]}", flush=True)
        if enforce:
            deadline.remaining()
        return result


def command_evidence(directory, commands):
    """All command receipts plus recent output, with full logs retained separately.

    A prefix of a growing raw log hides every later test forever. The model gets
    a complete compact status index and the latest eight output excerpts instead.
    Both are actual host-written files, so exact artifact quotes stay auditable.
    """
    directory = Path(directory)
    preview = max(80, min(500, 24000 // max(1, len(commands))))
    index = []
    for number, command in enumerate(commands, 1):
        text = command.get("command", "")
        index.append({"step": number, "exit_code": command["exit_code"],
                      "command": text[:preview], "command_truncated": len(text) > preview,
                      "full_record_sha256": digest(command)})
    recent = []
    for number, command in list(enumerate(commands, 1))[-8:]:
        recent.append({"step": number, "exit_code": command["exit_code"],
                       "command": command.get("command", "")[:2000],
                       "command_truncated": len(command.get("command", "")) > 2000,
                       "stdout_tail": command.get("stdout", "")[-3000:],
                       "stderr_tail": command.get("stderr", "")[-1000:],
                       "output_truncated": len(command.get("stdout", "")) > 3000 or len(command.get("stderr", "")) > 1000,
                       "full_record_sha256": digest(command)})
    save(directory / "commands.index.json", {"scope": "Complete index of all observed agent shell commands; exits describe commands, not necessarily tests",
         "commands": index})
    save(directory / "commands.recent.json", {"scope": "Latest eight commands with bounded output tails; omitted output is not evidence of absence. Full records are retained in commands.json.",
         "commands": recent})
    # Earlier failures must not disappear as successful follow-ups accumulate.
    # Keep output from every command, with a global character budget, while
    # retaining the larger recent window for the latest validation details.
    quota = max(0, min(2400, 60000 // max(1, len(commands))))
    history = []
    for number, command in enumerate(commands, 1):
        stderr = command.get("stderr", "")
        stdout = command.get("stdout", "")
        err_budget = min(len(stderr), quota // 3)
        out_budget = quota - err_budget
        history.append({"step": number, "exit_code": command["exit_code"],
                        "stdout_tail": stdout[-out_budget:] if out_budget else "",
                        "stderr_tail": stderr[-err_budget:] if err_budget else "",
                        "output_truncated": len(stdout) > out_budget or len(stderr) > err_budget,
                        "full_record_sha256": digest(command)})
    save(directory / "commands.history.json", {
        "scope": "Bounded output for every observed agent command; pair step numbers with commands.index.json. Truncation is NOT proof of missing records. Full records remain in commands.json.",
        "commands": history})
    return [{"path": "harness-observed/" + name, "sha256": file_hash(directory / name),
             "text": (directory / name).read_text(encoding="utf-8"), "truncated": False}
            for name in ("commands.index.json", "commands.recent.json", "commands.history.json")]


class Snapshots:
    """Immutable patch + observed commands; final verifier reconstructs in a fresh image."""
    def __init__(self, runtime, manifest, run):
        self.runtime, self.manifest, self.run = runtime, manifest, Path(run)
        baseline = tracked_image_baseline(runtime, manifest.base_commit)
        initial_patch = self.run / "initial_tracked.diff"
        initial_patch.write_bytes(baseline.pop("tracked_patch").encode("utf-8"))
        self.baseline = baseline | {"patch_file": initial_patch.name,
                                    "patch_sha256": file_hash(initial_patch),
                                    "worktree_tree": observed_worktree_tree(runtime)}
        save(self.run / "initial_baseline.json", self.baseline)
        self.initial = untracked_fingerprints(runtime)
        self.items = {}
        save(self.run / "initial_untracked.json", self.initial)

    def capture(self, turn):
        # Export housekeeping must not be mistaken for agent tests/evidence.
        commands = [c for c in self.runtime.commands if c.get("source") == "agent"]
        patch = export_worktree_prediction(self.runtime, self.baseline["worktree_tree"])
        auxiliary = None
        fixture = vars(self.runtime).get("task_fixture")
        if fixture is not None:
            auxiliary = fixture.capture(patch, commands)
        patch_digest = digest({"patch": patch, "fixture_state": auxiliary["state"]}) if auxiliary else digest(patch)
        path = self.run / "snapshots" / f"turn-{turn}"
        path.mkdir(parents=True)
        (path / "patch.diff").write_bytes(patch.encode("utf-8"))
        save(path / "commands.json", commands)
        if auxiliary is not None:
            save(path / "auxiliary.json", auxiliary)
        files = []
        for name in ("patch.diff",):
            file = path / name
            content = file.read_bytes().decode("utf-8")
            files.append({"path": "harness-observed/" + name, "sha256": file_hash(file),
                          "text": content[:60000], "truncated": len(content) > 60000})
        files.extend(command_evidence(path, commands))
        binding = self.run / "binding.json"
        if binding.is_file():
            files.append({"path": "harness-observed/binding.json", "sha256": file_hash(binding),
                          "text": binding.read_text(encoding="utf-8")})
        task_binding = self.run / "task-binding" / "binding.json"
        if task_binding.is_file():
            from task_binding import evidence_files
            files.extend(evidence_files(task_binding))
        item = {"patch": patch, "digest": patch_digest, "commands": commands,
                "files": files, "listing": [{k: f[k] for k in ("path", "sha256")} for f in files]}
        if auxiliary is not None:
            item["auxiliary"] = auxiliary
        self.items[str(path)] = item
        return str(path), patch_digest, item


class ExactGrounding:
    """Keep the team's aggregation; reject fabricated quotes before they reach it."""
    def __init__(self, llm, bundle, transcript, subcheck):
        self.llm = llm
        self.allowed = {f["path"]: f["text"] for f in bundle["files"]}
        self.allowed.update({m["message_id"]: m["text"] for m in transcript if m["role"] == "agent"})
        self.allowed["subcheck:" + subcheck["checker"]] = json.dumps(subcheck, ensure_ascii=False)

    def json_obj(self, *args, **kwargs):
        out = self.llm.json_obj(*args, **kwargs)
        for clause in out.get("clauses", []):
            grounds = clause.get("grounding", [])
            if not grounds or any(not g.get("quote", "").strip() or g.get("ref") not in self.allowed
                                  or g["quote"] not in self.allowed[g["ref"]] for g in grounds):
                clause["verdict"] = "unsupported"
                clause["grounding"] = []
        return out


class Checkers:
    DRIVER_MODES = {("deliverable_shape", "1"): "patch_presence", ("rubric_judge", "1"): "rubric",
                    ("task_fixture", "1"): "task_fixture"}

    def __init__(self, snapshots, llm, run, manifest, verification_seconds=900):
        self.snapshots, self.llm, self.run_dir, self.manifest = snapshots, llm, Path(run), manifest
        self.results, self.cache = [], {}
        self.verification_seconds = verification_seconds
        self.review_deadline = None
        self.task_binding = None

    def registered(self, checker_id, version):
        return (checker_id, version) in self.DRIVER_MODES or (checker_id, version) == ("official_tests", "1")

    def run(self, checker_id, version, parameters, snapshot_image, snapshot_digest, turn=None, context=None):
        if not self.registered(checker_id, version):
            raise KeyError("Unregistered checker")
        context = context or {}
        key = digest([checker_id, version, parameters, snapshot_image, snapshot_digest, context])
        if key in self.cache:
            return self.cache[key]
        item = self.snapshots.items[snapshot_image]
        try:
            if checker_id == "deliverable_shape":
                status = "pass" if item["patch"].strip() else "fail"
                reason, evidence = "patch_present" if status == "pass" else "patch_absent", {
                    "patch_sha256": item["digest"], "patch_present": bool(item["patch"].strip()),
                    "command_count": len(item["commands"]),
                    "scope": "Patch presence is not correctness; exit 0 only describes that observed command"}
            elif checker_id == "official_tests":
                status, reason, evidence = self.official(item["patch"])
            elif checker_id == "task_fixture":
                from task_fixtures import verify
                if self.task_binding is None or parameters.get("binding") != digest(self.task_binding):
                    raise ValueError("Task fixture binding missing or changed")
                status, reason, evidence = verify(self.task_binding, item, context, runner=self)
            else:
                status, reason, evidence = self.rubric(parameters, item, context, snapshot_image, snapshot_digest, turn)
        except Exception as exc:
            status, reason, evidence = "unknown", "checker_error", {"error_type": type(exc).__name__}
        result = CheckResult(checker_id, version, parameters, status, reason, evidence, snapshot_digest,
                             evidence_ref=f"chk-{len(self.results) + 1:03d}", turn=turn)
        self.results.append(result)
        self.cache[key] = result
        with (self.run_dir / "checks.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(result.to_json(), ensure_ascii=False) + "\n")
        return result

    def rubric(self, parameters, item, context, image, snapshot, turn):
        # Only post-activation messages are governed by the new requirement.
        transcript = context.get("transcript", [])
        disclosures = [m["turn"] for m in transcript if m["role"] == "user"
                       and m.get("disclosed_requirement") == parameters["requirement"]]
        if not disclosures:
            return "unknown", "no_disclosure", {}
        transcript = [m for m in transcript if m["turn"] >= min(disclosures)]
        check = parameters.get("subcheck", {"id": "deliverable_shape", "version": "1", "parameters": {}})
        probe = self.run(check["id"], check["version"], check["parameters"], image, snapshot, turn, context)
        sub = {"checker": probe.ref, "status": probe.status, "reason_code": probe.reason_code,
               "evidence_ref": probe.evidence_ref, "evidence": probe.evidence}
        spec = RubricSpec.from_template(context["satisfaction"], samples=3,
                                       message_grounded_clauses=parameters["message_clauses"])
        with IndependentSamples(self.llm, samples=3) as parallel:
            wrapped = ExactGrounding(parallel, item, transcript, sub)
            status, reason, evidence = grade(wrapped, parameters["requirement"], context["statement"],
                                             spec, item, transcript, sub)
        evidence["judge_model"] = self.llm.model
        return status, reason, evidence

    def official(self, patch):
        save(self.run_dir / "predictions.json", {self.manifest.task_id: {"model_patch": patch}})
        command = [sys.executable, "-u", str(SCRIPTS / "run_evaluation.py"),
                   "--dataset", str(self.manifest.task_path), "--patch_dir", str(self.run_dir / "predictions.json"),
                   "--platform", "linux", "--workers", "1", "--overwrite", "0",
                   "--output_dir", str(self.run_dir / "evaluation")]
        seconds = self.verification_seconds
        if self.review_deadline and self.review_deadline.expires is not None:
            seconds = self.review_deadline.remaining()
        command += ["--wall-timeout-seconds", str(seconds)]
        if getattr(self, 'protect_official_tests', False):
            command += ['--protect-official-tests']
        print("[final review] official tests; see", self.run_dir / "evaluation.log", flush=True)
        with (self.run_dir / "evaluation.log").open("w", encoding="utf-8") as out:
            rc = subprocess.run(command, stdout=out, stderr=subprocess.STDOUT).returncode
        path = self.run_dir / "evaluation/results.json"
        report = read(path) if path.is_file() else {}
        valid = rc == 0 and report.get("submitted") == 1 and report.get("error") == 0 and report.get("incomplete") == 0
        status = ("pass" if report.get("success") == 1 else "fail") if valid else "unknown"
        return status, "official_tests_" + status, {"report": report, "report_path": "evaluation/results.json",
                  "report_sha256": file_hash(path) if path.is_file() else None,
                  "reward": (1 if status == "pass" else 0) if valid else None, "exit_code": rc}


class SweController(Controller):
    def snapshot(self, container, turn):
        image, code_digest, item = self.checkers.snapshots.capture(turn)
        changed = self.current_digest is not None and code_digest != self.current_digest
        if self.initial_digest is None:
            self.initial_digest = code_digest
        self.current_image, self.current_digest = image, code_digest
        if changed:
            stale = self.ledger.mark_stale_for_snapshot(code_digest, turn)
            if stale:
                self.add_event("evidence_stale", turn, {"requirements": stale, "snapshot": code_digest})
        return image, code_digest, changed

    def run_probes(self, turn):
        result = self.checkers.run("deliverable_shape", "1", {}, self.current_image, self.current_digest, turn)
        if result.status == "pass" and "deliverable_present" not in self.fired:
            self.add_event("deliverable_present", turn, {"artifact": "repository patch"})
        return [result]

    @staticmethod
    def _achievability_proof(validation_report):
        return {"rubric_judge": "pass" if validation_report and validation_report.get("gate_passed") else "unknown",
                "task_fixture": "pass" if validation_report and validation_report.get("gate_passed") else "unknown",
                "source": "local SWE persona checker calibration (separate from task outcomes)"}

    def checker_context(self, entry, transcript):
        result = super().checker_context(entry, transcript)
        # Include the activation metadata for rubric scope, never private tools.
        result["transcript"] = transcript or []
        return result

    def validate_decision(self, decision, obs):
        errors = super().validate_decision(decision, obs)
        # A model cannot override the registered rubric/samples via request parameters.
        for request in decision.get("verification_requests", []):
            entry = self.ledger.entries.get(request.get("requirement_ref"))
            try:
                overrides = json.loads(request.get("parameters") or "{}")
            except (ValueError, TypeError):
                overrides = True
            if entry and (request.get("checker_id") != entry.checker["id"]
                          or request.get("version") != entry.checker["version"]
                          or overrides):
                errors.append("Verification must use the active requirement's exact registered checker and empty parameters")
        return errors
