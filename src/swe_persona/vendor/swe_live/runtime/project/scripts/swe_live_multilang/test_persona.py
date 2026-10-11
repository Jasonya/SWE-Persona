import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from persona_protocol import (CRITERIA, RoleBudget, binding, calibrate, cumulative_status,
                              judge_reply, outcome, simulator_message, validate_sample)
from run_baseline import BudgetExhausted
from run_persona import main, run_dialogue


def vote(verdict="satisfied", evidence="real execution", message="real reply"):
    return {"criteria":[
        {"id":"summary_format","verdict":verdict,"reason":"fixture",
         "citations":[{"ref":"message","quote":message}]},
        {"id":"status_grounding","verdict":verdict,"reason":"fixture",
         "citations":[{"ref":"message","quote":message},{"ref":"snapshot","quote":evidence}]}]}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.persona,self.template=binding()

    def test_fabricated_quote_and_unknown_ref_cannot_pass(self):
        sample=vote()
        evidence={"message":"real reply","snapshot":"real execution"}
        sample["criteria"][1]["citations"][1]["quote"]="invented proof"
        self.assertEqual(validate_sample(sample,evidence)["status_grounding"],"unsupported")
        sample["criteria"][1]["citations"][1]={"ref":"secret_oracle","quote":"real execution"}
        self.assertEqual(validate_sample(sample,evidence)["status_grounding"],"unsupported")

    def test_self_report_alone_cannot_establish_truth(self):
        sample=vote()
        sample["criteria"][1]["citations"]=[{"ref":"message","quote":"real reply"}]
        self.assertEqual(validate_sample(sample,{"message":"real reply"})["status_grounding"],"unsupported")

    def test_duplicate_or_missing_criterion_is_unknown(self):
        sample=vote()
        sample["criteria"][1]["id"]="summary_format"
        self.assertEqual(set(validate_sample(sample,{}).values()),{"unsupported"})

    def test_majority_and_deterministic_format_checks(self):
        llm=Mock()
        llm.json_obj.side_effect=[vote(),vote(),vote("unsupported")]
        result=judge_reply(llm,self.template,"real reply",{"snapshot":"real execution"})
        self.assertEqual(result["status"],"met")
        llm.json_obj.side_effect=[vote(message="```"),vote(message="```"),vote(message="```")]
        result=judge_reply(llm,self.template,"```sh\necho test\n```",{"snapshot":"real execution"})
        self.assertEqual(result["status"],"not_met")

    def test_simulator_cannot_invent_contract_or_omit_disclosure(self):
        llm=Mock()
        public={"instance_id":"sample","problem_statement":"A public warning is missing.","environment":{}}
        llm.json_obj.return_value={"tone":"brief","contract_quote":"A public warning"}
        text,decision=simulator_message(llm,self.persona,public,self.template,2,"done","unknown")
        self.assertIn(self.template["user_requirement"],text)
        self.assertNotIn("steps",decision["observation"])
        llm.json_obj.return_value={"tone":"brief","contract_quote":"Invented hidden requirement"}
        with self.assertRaises(ValueError):
            simulator_message(llm,self.persona,public,self.template,2,"done","unknown")

    def test_historical_violation_is_not_erased(self):
        self.assertEqual(cumulative_status([{"status":"not_met"},{"status":"met"}]),"not_met")
        self.assertFalse(outcome(True,True,[{"status":"not_met"},{"status":"met"}],True,True)["joint_success"])

    def test_joint_success_requires_all_three_components(self):
        met=[{"status":"met"}]
        self.assertTrue(outcome(True,True,met,True,True)["joint_success"])
        self.assertFalse(outcome(False,True,met,True,True)["joint_success"])
        self.assertIsNone(outcome(True,False,met,True,True)["joint_success"])
        self.assertIsNone(outcome(True,True,met,False,True)["joint_success"])
        self.assertIsNone(outcome(True,True,[{"status":"unknown"}],True,True)["joint_success"])
        self.assertIsNone(outcome(None,True,met,True,True)["joint_success"])

    def test_auxiliary_budget_has_no_nested_retries(self):
        client=Mock()
        budget=RoleBudget(client,agent_calls=1,auxiliary_calls=1,seconds=60)
        budget.json_obj([],{},role="calibration.one")
        self.assertEqual(client.json_obj.call_args.kwargs["attempts"],1)
        self.assertEqual(client.json_obj.call_args.kwargs["repairs"],0)
        with self.assertRaises(BudgetExhausted):
            budget.json_obj([],{},role="checker.two")
        budget.text([],role="agent.turn1")
        with self.assertRaises(BudgetExhausted):
            budget.text([],role="agent.turn2")

    def test_failed_calibration_stays_failed(self):
        llm=Mock()
        llm.json_obj.return_value={"criteria":[]}
        result=calibrate(llm,self.template)
        self.assertFalse(result["passed"])
        self.assertEqual(llm.json_obj.call_count,18)


class DialogueTests(unittest.TestCase):
    def fixture(self):
        persona,template=binding()
        public={"instance_id":"public-only","problem_statement":"Fix the missing warning.","environment":{}}
        runtime=Mock(commands=[])
        agent=Mock(history=[])
        def reply(message,turn,step_budget):
            value=Mock(reply="1. Use the fix.\n2. Work remains unverified.\n3. No decision needed.",status="done",message_id=f"a-{turn}")
            value.to_json.return_value={"reply":value.reply,"status":"done"}
            return value
        agent.run_turn.side_effect=reply
        return persona,template,public,runtime,agent

    def test_done_in_turn_one_cannot_bypass_persona(self):
        persona,template,public,runtime,agent=self.fixture()
        with tempfile.TemporaryDirectory() as temp, \
             patch("run_persona.TerminalAgent",return_value=agent), \
             patch("run_persona.untracked_fingerprints",return_value={}), \
             patch("run_persona.export_prediction",return_value="public model diff"), \
             patch("run_persona.simulator_message",return_value=(template["user_requirement"],{})), \
             patch("run_persona.judge_reply",return_value={"status":"met"}):
            result=run_dialogue(Mock(),runtime,{"base_commit":"base"},public,persona,template,
                                {"passed":True},Path(temp),"Please fix this.")
            self.assertEqual(agent.run_turn.call_count,2)
            self.assertTrue(result["disclosed"])
            ledger=json.loads((Path(temp)/"ledger.json").read_text())
            self.assertEqual(ledger["entries"][0]["introduced_turn"],2)
            self.assertEqual(ledger["entries"][0]["satisfaction_status"],"met")

    def test_missing_gate_blocks_agent(self):
        persona,template,public,runtime,agent=self.fixture()
        with tempfile.TemporaryDirectory() as temp, patch("run_persona.TerminalAgent",return_value=agent) as factory:
            with self.assertRaises(ValueError):
                run_dialogue(Mock(),runtime,{"base_commit":"base"},public,persona,template,
                             {"passed":False},Path(temp),"Please fix this.")
            factory.assert_not_called()

    def test_budget_exhaustion_before_disclosure_is_not_success(self):
        persona,template,public,runtime,agent=self.fixture()
        agent.run_turn.side_effect=BudgetExhausted("fixture")
        with tempfile.TemporaryDirectory() as temp, \
             patch("run_persona.TerminalAgent",return_value=agent), \
             patch("run_persona.untracked_fingerprints",return_value={}), \
             patch("run_persona.export_prediction",return_value="partial diff"):
            result=run_dialogue(Mock(),runtime,{"base_commit":"base"},public,persona,template,
                                {"passed":True},Path(temp),"Please fix this.")
            self.assertTrue(result["budget_exhausted"])
            self.assertFalse(result["disclosed"])
            self.assertFalse(result["checked_final"])


class EntryPointTests(unittest.TestCase):
    def exercise(self, passed):
        client=Mock()
        client.info.return_value={"OSType":"linux"}
        client.images.get.return_value.id="immutable-image"
        runtime=Mock(commands=[])
        runtime.sh.return_value=(0,"/usr/bin/timeout\nbase\n","")
        task={"instance_id":"sample", "problem_statement":"Public issue", "base_commit":"base",
              "docker_image":"image", "patch":"HIDDEN_GOLD", "test_patch":"HIDDEN_TEST"}
        episode={"patch":"model change","checks":[{"status":"met"}],"disclosed":True,
                 "model_done":True,"budget_exhausted":False,"turns":2,"checked_final":True}
        with tempfile.TemporaryDirectory() as temp, \
             patch("run_persona.ROOT",Path(temp)), patch("run_persona.read_task",return_value=task), \
             patch("run_persona.load_local_env"), patch.dict(os.environ,{"AZURE_OPENAI_API_KEY":"unit-test-not-a-key"}), \
             patch("sys.argv",["run_persona.py"]), patch("docker.from_env",return_value=client), \
             patch("run_persona.AgentContainer",return_value=runtime), patch("run_persona.Llm"), \
             patch("run_persona.calibrate",return_value={"passed":passed}), \
             patch("run_persona.opening_message",return_value="Please fix this."), \
             patch("run_persona.run_dialogue",return_value=episode) as dialogue, \
             patch("run_persona.evaluate",return_value=(True,True)) as evaluator:
            result=main()
            paths=list(Path(temp).glob('.local/multilang-persona/runs/*/summary.json'))
            summary=json.loads(paths[0].read_text())
            runtime.remove.assert_called_once()
            if passed:
                self.assertEqual(result,0)
                self.assertTrue(summary["joint_success"])
                self.assertTrue(summary["evaluation_valid"])
                # Private row never travels through the dialogue interface.
                args=dialogue.call_args.args
                self.assertNotIn("HIDDEN_",json.dumps(args[2:7]))
                evaluator.assert_called_once()
            else:
                self.assertEqual(result,2)
                self.assertEqual(summary["status"],"checker_calibration_failed")
                self.assertIsNone(summary["joint_success"])
                dialogue.assert_not_called()
                evaluator.assert_not_called()

    def test_full_entrypoint_saves_result_and_cleans_container(self):
        self.exercise(True)

    def test_entrypoint_stops_on_calibration_failure(self):
        self.exercise(False)

class SnapshotTests(unittest.TestCase):
    fixture = DialogueTests.fixture

    def test_late_code_change_invalidates_checker_snapshot(self):
        persona,template,public,runtime,agent=self.fixture()
        with tempfile.TemporaryDirectory() as temp, \
             patch("run_persona.TerminalAgent",return_value=agent), \
             patch("run_persona.untracked_fingerprints",return_value={}), \
             patch("run_persona.export_prediction",side_effect=["v1","v1","changed-late"]), \
             patch("run_persona.simulator_message",return_value=(template["user_requirement"],{})), \
             patch("run_persona.judge_reply",return_value={"status":"met"}):
            result=run_dialogue(Mock(),runtime,{"base_commit":"base"},public,persona,template,
                                {"passed":True},Path(temp),"Please fix this.")
            self.assertFalse(result["checked_final"])
            ledger=json.loads((Path(temp)/"ledger.json").read_text())
            self.assertEqual(ledger["entries"][0]["satisfaction_status"],"stale")

    def test_missing_final_check_cannot_reuse_previous_pass(self):
        persona,template,public,runtime,agent=self.fixture()
        def reply(message,turn,step_budget):
            status="working" if turn==2 else "done"
            value=Mock(reply="A manager reply",status=status,message_id=f"a-{turn}")
            value.to_json.return_value={"reply":value.reply,"status":status}
            return value
        agent.run_turn.side_effect=reply
        with tempfile.TemporaryDirectory() as temp, \
             patch("run_persona.TerminalAgent",return_value=agent), \
             patch("run_persona.untracked_fingerprints",return_value={}), \
             patch("run_persona.export_prediction",return_value="same patch"), \
             patch("run_persona.simulator_message",return_value=(template["user_requirement"],{})), \
             patch("run_persona.judge_reply",side_effect=[{"status":"met"},BudgetExhausted("checker budget")]):
            result=run_dialogue(Mock(),runtime,{"base_commit":"base"},public,persona,template,
                                {"passed":True},Path(temp),"Please fix this.")
            self.assertTrue(result["budget_exhausted"])
            self.assertFalse(result["checked_final"])


if __name__ == "__main__":
    unittest.main()
