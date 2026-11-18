# SPDX-License-Identifier: Apache-2.0
"""GoalRunner end to end with the fake ACP agent and the fake model server, plus
the card translation rules on their own."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_openai_server import FakeOpenAI  # noqa: E402

from luma_playground.agents import acp, sandbox  # noqa: E402
from luma_playground.agents.connections import Connection  # noqa: E402
from luma_playground.agents.runner import TURN_END, CardTranslator, GoalRunner, mcp_servers_for, summarize_step  # noqa: E402
from luma_playground.core.events import EventBus  # noqa: E402
from luma_playground.core.model import GoalStatus, Member, ThreadEntry  # noqa: E402
from luma_playground.core.project import create_project  # noqa: E402
from luma_playground.core.store import ProjectStore  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_acp_agent.py")


def fold(entries: list[ThreadEntry]) -> list[ThreadEntry]:
    """What the UI shows: the last entry for each id, at the place of the first; a card
    folded into another (``data.folded_into``) isn't shown on its own."""
    order: list[str] = []
    last: dict[str, ThreadEntry] = {}
    for entry in entries:
        if entry.id not in last:
            order.append(entry.id)
        last[entry.id] = entry
    return [last[i] for i in order if not last[i].data.get("folded_into")]


# ── The translator on its own ──────────────────────────────────────────────

class TranslatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.cards: list[ThreadEntry] = []
        self.live: list[dict] = []
        self.now = [100.0]
        self.t = CardTranslator(self.root, self.cards.append, by="claude", live=lambda **kw: self.live.append(kw),
                                clock=lambda: self.now[0])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def chunk(self, text, mid=None):
        update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
        if mid:
            update["messageId"] = mid
        self.t.handle(update)

    def test_chunks_coalesce_into_one_message(self):
        for piece in ("Hel", "lo ", "there."):
            self.chunk(piece)
        self.assertEqual(self.cards, [])
        self.t.finish()
        self.assertEqual([(c.kind, c.text, c.by) for c in self.cards], [("message", "Hello there.", "claude")])
        self.assertTrue(any("partial_text" in s for s in self.live))

    def test_a_new_message_id_starts_a_new_card(self):
        self.chunk("One.", "m1")
        self.chunk("Two.", "m2")
        self.t.finish()
        self.assertEqual([c.text for c in self.cards], ["One.", "Two."])

    def test_tool_calls_group_into_a_step(self):
        self.chunk("Let me look.")
        self.t.handle({"sessionUpdate": "tool_call", "toolCallId": "a", "title": "Read `src/app.js`", "kind": "read",
                       "status": "in_progress", "locations": [{"path": os.path.join(self.root, "src/app.js")}]})
        self.t.handle({"sessionUpdate": "tool_call_update", "toolCallId": "a", "status": "completed"})
        self.t.handle({"sessionUpdate": "tool_call", "toolCallId": "b", "title": "Edit app.js", "kind": "edit",
                       "status": "pending"})
        self.now[0] += 4.5
        self.t.handle({"sessionUpdate": "tool_call_update", "toolCallId": "b", "status": "completed",
                       "content": [{"type": "diff", "path": os.path.join(self.root, "src/app.js"), "newText": "x"}]})
        self.t.handle({"sessionUpdate": "tool_call", "toolCallId": "c", "title": "npm test", "kind": "execute"})
        self.t.handle({"sessionUpdate": "tool_call_update", "toolCallId": "c", "status": "completed"})
        self.chunk("All good.")
        self.t.finish()
        kinds = [(c.kind, c.data.get("state")) for c in self.cards]
        self.assertEqual(kinds, [("message", None), ("step", "running"), ("step", "done"), ("message", None)])
        step = self.cards[2]
        self.assertEqual(step.id, self.cards[1].id)
        self.assertEqual(step.data["files"], ["src/app.js"])
        self.assertEqual(step.data["title"], "Changed src/app.js and ran a command")
        self.assertEqual(step.data["elapsed_s"], 4.5)
        self.assertEqual([c["status"] for c in step.data["tool_calls"]], ["done", "done", "done"])
        self.assertEqual(step.data["tool_calls"][0]["title"], "Read src/app.js")

    def test_playground_tools_are_not_steps(self):
        self.t.handle({"sessionUpdate": "tool_call", "toolCallId": "p", "title": "mcp__playground__playground_step_report",
                       "kind": "other"})
        self.t.handle({"sessionUpdate": "tool_call_update", "toolCallId": "p", "status": "completed"})
        self.t.finish()
        self.assertEqual(self.cards, [])

    def test_outside_paths_are_not_listed(self):
        self.t.handle({"sessionUpdate": "tool_call", "toolCallId": "a", "title": "Read", "kind": "read",
                       "locations": [{"path": "/etc/hosts"}, {"path": os.path.join(self.root, "ok.txt")}]})
        self.t.finish("end_turn")
        self.assertEqual(self.cards[-1].data["files"], ["ok.txt"])

    def test_failed_last_call_fails_the_step_when_the_turn_stops_short(self):
        self.t.handle({"sessionUpdate": "tool_call", "toolCallId": "a", "title": "Build", "kind": "execute"})
        self.t.handle({"sessionUpdate": "tool_call_update", "toolCallId": "a", "status": "failed"})
        self.t.finish("refusal")
        self.assertEqual(self.cards[-1].data["state"], "failed")

    def test_plan_card_is_rewritten_with_the_same_id(self):
        plan = lambda statuses: {"sessionUpdate": "plan", "entries": [  # noqa: E731
            {"content": "One", "priority": "high", "status": statuses[0]},
            {"content": "Two", "priority": "low", "status": statuses[1]}]}
        self.t.handle(plan(["in_progress", "pending"]))
        self.t.handle(plan(["completed", "in_progress"]))   # status only: live, no card
        self.assertEqual(len(self.cards), 1)
        self.t.handle(plan(["completed", "completed"]))
        self.t.finish()
        self.assertEqual(len(self.cards), 2)
        self.assertEqual(self.cards[0].id, self.cards[1].id)
        data = self.cards[1].data
        self.assertEqual(data["stories"], [{"title": "One", "checks": []}, {"title": "Two", "checks": []}])
        self.assertEqual(data["size"], "small")
        self.assertEqual([e["status"] for e in data["entries"]], ["completed", "completed"])

    def test_spend_shows_only_for_money_connections(self):
        t = CardTranslator(self.root, self.cards.append, show_money=True, cost_start=0.10, clock=lambda: 1.0)
        t.handle({"sessionUpdate": "tool_call", "toolCallId": "a", "title": "x", "kind": "read"})
        t.handle({"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": {"amount": 0.35, "currency": "USD"}})
        t.finish()
        self.assertEqual(self.cards[-1].data["spend_text"], "$0.25")
        self.assertAlmostEqual(t.turn_cost, 0.25)

    def test_replay_is_ignored(self):
        self.t.replaying = True
        self.chunk("old")
        self.t.replaying = False
        self.t.finish()
        self.assertEqual(self.cards, [])

    def test_summaries(self):
        self.assertEqual(summarize_step([{"title": "Run npm test", "kind": "execute"}], []), "Run npm test")
        calls = [{"title": "a", "kind": "edit"}, {"title": "b", "kind": "edit"}, {"title": "c", "kind": "edit"}]
        self.assertEqual(summarize_step(calls, ["a", "b", "c"]), "Changed 3 files")
        self.assertEqual(summarize_step([{"title": "r", "kind": "read"}, {"title": "s", "kind": "search"}], []),
                         "Looked through the project")


# ── The runner with real subprocesses ─────────────────────────────────────

class RunnerBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(base / "config"),
                                               "PLAYGROUND_DATA_DIR": str(base / "data"),
                                               "PLAYGROUND_HOME": str(base / "projects"),
                                               "PLAYGROUND_OFFLINE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = base / "agent-state"
        self.state.mkdir()
        self.bus = EventBus()
        self.states: list[dict] = []
        self.bus.subscribe("agent.state", lambda **kw: self.states.append(kw))
        project = create_project("Kiln", "A pottery class sign-up", root=base / "projects", git=False, bus=self.bus,
                                 owner=Member(id="noor", name="Noor", role="owner"))
        self.root = str(project.path)
        self.store = ProjectStore(self.root, bus=self.bus)
        self.goal = self.store.create_goal("Say hi", outcome="hello.txt says hi", owner="noor")
        self.agent_env = dict(os.environ, FAKE_ACP_STATE=str(self.state))
        self.runners: list[GoalRunner] = []
        self.claude = Connection(id="claude", label="Claude", kind="personal-plan", provider="claude", owner="noor")

    def tearDown(self) -> None:
        for runner in self.runners:
            runner.close()
        self.tmp.cleanup()

    def factory(self, connection, root, handlers, *, goal_id, bus, log_path, registry=None):
        env = dict(self.agent_env, FAKE_ACP_GOAL=goal_id, PLAYGROUND_ACTOR=connection.id, PLAYGROUND_GOAL=goal_id)
        return acp.AcpClient([sys.executable, FAKE], env=env, cwd=root, handlers=handlers, stderr_log=log_path,
                             name=connection.id)

    def runner(self, connection=None, **kw) -> GoalRunner:
        kw.setdefault("agent_factory", self.factory)
        kw.setdefault("sandbox_policy", sandbox.SandboxPolicy(self.root, backend="none"))
        runner = GoalRunner(self.root, self.goal.id, connection or self.claude, self.bus, **kw)
        self.runners.append(runner)
        return runner

    def calls(self) -> list[dict]:
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def thread(self) -> list[ThreadEntry]:
        return fold(self.store.read_thread(self.goal.id))

    def goal_status(self) -> GoalStatus:
        return self.store.read_goal(self.goal.id).status

    def wait_for(self, predicate, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        self.fail("timed out waiting")


class RunnerOptionTests(RunnerBase):
    def calls(self) -> list[dict]:
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_settings_are_offered_kept_and_put_back(self):
        seen = []
        self.bus.subscribe("agent.options", lambda **kw: seen.append(kw["options"]))
        runner = self.runner()
        runner.prepare()
        self.wait_for(lambda: runner.options())
        options = {o["id"]: o for o in runner.options()}
        self.assertEqual(options["model"]["currentValue"], "fast")
        self.assertEqual(options["reasoning_effort"]["category"], "thought_level")
        self.assertEqual(options["__mode"]["currentValue"], "default")     # the older modes shape, read alike
        runner.set_option("reasoning_effort", "high")
        self.wait_for(lambda: {"config": ["reasoning_effort", "high"]} in self.calls())
        from luma_playground.agents.runner import _as_options
        saved = _as_options(self.store.read_goal(self.goal.id).extra["agent_options"])["claude"]
        self.assertEqual(saved, {"reasoning_effort": "high"})
        runner.close()
        # A fresh start (new session) puts the choice back by itself.
        again = self.runner()
        again.prepare()
        self.wait_for(lambda: again.options())
        self.wait_for(lambda: self.calls().count({"config": ["reasoning_effort", "high"]}) == 2)
        self.assertTrue(seen)

    def test_the_connection_model_is_applied_without_a_conversation_choice(self):
        from luma_playground.agents.connections import Connection
        conn = Connection(id="claude", label="Claude", kind="personal-plan", provider="claude", owner="noor",
                          model="deep")
        runner = self.runner(conn)
        runner.prepare()
        self.wait_for(lambda: {"config": ["model", "deep"]} in self.calls())
        options = {o["id"]: o for o in runner.options()}
        self.assertEqual(options["model"]["currentValue"], "deep")

    def test_a_conversation_choice_beats_the_connection_model(self):
        from luma_playground.agents.connections import Connection
        self.store.update_goal(self.goal.id, lambda goal: goal.extra.__setitem__(
            "agent_options", {"claude": {"model": "fast"}}))
        conn = Connection(id="claude", label="Claude", kind="personal-plan", provider="claude", owner="noor",
                          model="deep")
        runner = self.runner(conn)
        runner.prepare()
        self.wait_for(lambda: runner.options())
        self.assertNotIn({"config": ["model", "deep"]}, self.calls())


class RunnerStopTests(RunnerBase):
    def test_stop_ends_the_turn_and_the_next_message_carries_on(self):
        runner = self.runner()
        runner.send("busy-work")
        self.wait_for(lambda: runner.busy and any("prompt" in c for c in self.calls()))
        self.assertTrue(runner.cancel())
        self.assertTrue(runner.wait_idle(15))
        self.assertIn({"method": "session/cancel"}, self.calls())
        self.assertTrue(any(c.role == "system" and c.text.startswith("Stopped.") for c in self.thread()))
        self.assertEqual(self.goal_status(), GoalStatus.PAUSED)
        self.assertEqual(self.states[-1]["state"], "stopped")
        self.assertFalse(runner.cancel(), "nothing to stop when it's idle")
        runner.send("hello again")
        self.wait_for(lambda: any("hello again" in c.get("prompt", "") for c in self.calls()))

    def test_stop_ends_an_agent_that_ignores_it(self):
        runner = self.runner()
        runner.send("quiet")
        self.wait_for(lambda: runner.busy and any("prompt" in c for c in self.calls()))
        runner.cancel(grace=0.5)
        self.assertTrue(runner.wait_idle(15))
        cards = self.thread()
        self.assertTrue(any(c.role == "system" and c.text.startswith("Stopped.") for c in cards))
        self.assertFalse(any((c.data or {}).get("error") for c in cards), "stopping isn't a failure")


class RunnerCutOffTests(RunnerBase):
    def test_a_step_left_running_by_a_cut_off_turn_is_closed_by_the_next(self):
        self.store.append_thread(self.goal.id, ThreadEntry(kind="step", role="agent", by="claude", id="step-old",
                                                           data={"title": "Working", "state": "running"}))
        runner = self.runner()
        runner.send("hello again")
        self.assertTrue(runner.wait_idle(15))
        old = next(e for e in self.store.read_thread(self.goal.id, fold=True) if e.id == "step-old")
        self.assertEqual((old.data["state"], old.data["title"]), ("done", "Stopped partway"))

    def test_a_step_left_by_another_local_agent_is_closed_by_the_next_turn(self):
        self.store.append_thread(self.goal.id, ThreadEntry(kind="step", role="agent", by="opencode",
                                                            id="step-other",
                                                            data={"title": "Working", "state": "running"}))
        runner = self.runner()
        runner.send("hello again")
        self.assertTrue(runner.wait_idle(15))
        other = next(e for e in self.store.read_thread(self.goal.id, fold=True) if e.id == "step-other")
        self.assertEqual(other.data["state"], "done")

    def test_a_teammates_live_step_is_left_alone(self):
        self.store.append_thread(self.goal.id, ThreadEntry(kind="step", role="agent", by="priya-claude",
                                                           id="step-theirs", data={"title": "Fixing", "state": "running"}))
        self.assertEqual(self.store.close_stale_steps(self.goal.id, {"claude"}), 0)
        theirs = next(e for e in self.store.read_thread(self.goal.id, fold=True) if e.id == "step-theirs")
        self.assertEqual(theirs.data["state"], "running")


class RunnerLateAnswerTests(RunnerBase):
    def test_an_answer_after_the_turn_ended_starts_the_next_one(self):
        from luma_playground.core.model import Option, PendingDecision
        runner = self.runner()
        self.store.write_pending(PendingDecision(
            id="windows-or-one", question="Separate note windows, or one window?", goal=self.goal.id,
            options=[Option(id="separate", label="Separate windows"), Option(id="one", label="One window")]))
        runner.answer_decision("windows-or-one", "separate")
        self.wait_for(lambda: any("the person answered your question" in c.get("prompt", "") for c in self.calls()))
        prompt = next(c["prompt"] for c in self.calls() if "the person answered" in c.get("prompt", ""))
        self.assertIn("Separate windows", prompt)
        self.assertTrue(runner.wait_idle(15))
        self.assertIsNone(self.store.read_pending("windows-or-one"), "delivered once")

    def test_an_answer_its_own_call_picked_up_isnt_sent_again(self):
        from luma_playground.core.model import Option, PendingDecision
        runner = self.runner()
        self.store.write_pending(PendingDecision(
            id="pick", question="Which?", goal=self.goal.id,
            options=[Option(id="a", label="A"), Option(id="b", label="B")]))
        runner._busy.set()                          # the agent is mid-turn, its call waiting
        runner.answer_decision("pick", "a")
        self.store.remove_pending("pick")           # ...and the MCP server handed the answer over
        self.assertEqual(runner._answer_notes(peek=True), [])
        runner._busy.clear()


class RunnerLivePullTests(RunnerBase):
    def test_a_remote_agents_work_comes_back_during_the_turn_and_stops_after(self):
        from luma_playground.agents import computers, runner as runner_mod
        runner = self.runner()
        runner._agent = mock.Mock(computer=computers.Computer(id="b", name="B", host="h"), remote_cwd="/srv/p")
        pulls = []
        with mock.patch.object(runner_mod, "LIVE_PULL_S", 0.05), \
                mock.patch.object(computers, "pull", side_effect=lambda *a, **k: pulls.append(a[2])):
            stop = runner._live_pull()
            self.wait_for(lambda: len(pulls) >= 2)
            stop.set()
            time.sleep(0.15)
            settled = len(pulls)
            time.sleep(0.15)
            self.assertEqual(len(pulls), settled)
        self.assertEqual(set(pulls), {"/srv/p"})
        runner._agent = None
        runner.close()

    def test_a_local_agent_has_no_live_pull(self):
        runner = self.runner()
        self.assertIsNone(runner._live_pull())
        runner.close()


class RunnerTurnTests(RunnerBase):
    def test_a_full_turn_becomes_cards(self):
        runner = self.runner()
        person = runner.send("Create a file hello.txt containing the word hi, then stop.")
        self.assertEqual(person.role, "person")
        self.assertTrue(runner.wait_idle(30))
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")
        cards = self.thread()
        summary = [(c.kind, c.role) for c in cards]
        self.assertEqual(summary, [("message", "person"), ("plan", "agent"), ("message", "agent"),
                                   ("step", "agent"), ("message", "agent")])
        plan, said, step, done = cards[1], cards[2], cards[3], cards[4]
        self.assertEqual([s["title"] for s in plan.data["stories"]], ["Write hello.txt", "Check it worked"])
        self.assertEqual([e["status"] for e in plan.data["entries"]], ["completed", "completed"])
        self.assertEqual(said.text, "I'll create the file now.")
        self.assertEqual(step.data["state"], "done")
        self.assertEqual(step.data["files"], ["hello.txt"])
        self.assertEqual(step.data["title"], "Changed hello.txt and ran a command")
        self.assertEqual(step.data["spend_text"], "")      # a personal plan: no dollars shown
        self.assertEqual(done.text, "Done. hello.txt says hi.")
        self.assertTrue(all(c.by == "claude" for c in cards[1:]))
        self.assertEqual(self.goal_status(), GoalStatus.READY)
        self.assertEqual(self.states[-1]["state"], "idle")
        # The goal context went with the first prompt, and the MCP server was offered with the goal.
        prompt = next(e for e in self.calls() if "prompt" in e)["prompt"]
        self.assertIn(self.goal.id, prompt)
        self.assertIn("Create a file hello.txt", prompt)
        mcp = next(e for e in self.calls() if "mcpServers" in e)["mcpServers"]
        self.assertEqual(mcp[0]["name"], "playground")
        self.assertIn({"name": "PLAYGROUND_GOAL", "value": self.goal.id}, mcp[0]["env"])
        # The agent's escape attempts failed, and the permission was granted automatically (an edit in the project).
        log = self.calls()
        self.assertIsNotNone(next(e for e in log if "escape_error" in e)["escape_error"])
        self.assertEqual(next(e for e in log if "permission" in e)["permission"]["outcome"]["optionId"], "allow")
        # Usage was recorded for the connection.
        runs = Path(os.environ["PLAYGROUND_DATA_DIR"], "usage", "runs.jsonl").read_text()
        self.assertIn('"connection": "claude"', runs)
        self.assertIn("Runs on Noor's Claude", runs)

    def test_ask_policy_turns_permission_into_a_decision_card(self):
        runner = self.runner(permission_policy="ask")
        runner.send("make hello")
        self.wait_for(lambda: any(c.kind == "decision" for c in self.thread()))
        # The card is written first, then the conversation is marked as waiting for the person.
        self.wait_for(lambda: self.states and self.states[-1]["state"] == "waiting")
        card = next(c for c in self.thread() if c.kind == "decision")
        self.assertEqual(card.data["question"], "Claude wants to change a file")
        self.assertEqual([o["id"] for o in card.data["options"]], ["allow", "always", "reject"])
        self.assertEqual(card.data["recommended"], "allow")
        self.assertEqual(card.data["default"], "reject")
        self.assertTrue(card.data["open"])
        self.assertEqual(self.goal_status(), GoalStatus.NEEDS_YOU)
        self.assertEqual(self.states[-1]["state"], "waiting")
        runner.answer_decision(card.data["id"], "allow")
        self.assertTrue(runner.wait_idle(30))
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")
        answered = next(c for c in self.thread() if c.kind == "decision")
        self.assertEqual(answered.data["answered"]["option"], "allow")
        self.assertFalse(self.store.paths.pending(card.data["id"]).exists())
        self.assertEqual(self.goal_status(), GoalStatus.READY)

    def test_rejecting_keeps_the_file_unwritten(self):
        runner = self.runner(permission_policy="ask")
        runner.send("make hello")
        self.wait_for(lambda: any(c.kind == "decision" for c in self.thread()))
        card = next(c for c in self.thread() if c.kind == "decision")
        with self.assertRaises(ValueError):
            runner.answer_decision(card.data["id"], "not-an-option")
        runner.answer_decision(card.data["id"], "reject")
        self.assertTrue(runner.wait_idle(30))
        self.assertFalse(Path(self.root, "hello.txt").exists())

    def test_answer_through_the_store_also_unblocks(self):
        runner = self.runner(permission_policy="ask")
        runner.send("make hello")
        self.wait_for(lambda: any(c.kind == "decision" for c in self.thread()))
        card = next(c for c in self.thread() if c.kind == "decision")
        self.store.answer_decision(card.data["id"], "allow", by="noor")
        self.assertTrue(runner.wait_idle(30))
        self.assertTrue(Path(self.root, "hello.txt").exists())

    def test_stop_cancels_and_pauses(self):
        runner = self.runner(permission_policy="ask")
        runner.send("cancel-me")
        self.wait_for(lambda: any(c.kind == "decision" for c in self.thread()))
        runner.stop()
        self.assertTrue(runner.wait_idle(20))
        self.assertEqual(self.goal_status(), GoalStatus.PAUSED)
        self.assertEqual(self.states[-1]["state"], "stopped")
        perm = next(e for e in self.calls() if "permission" in e)["permission"]
        self.assertEqual(perm, {"outcome": {"outcome": "cancelled"}})
        self.assertIn({"method": "session/cancel"}, self.calls())
        self.assertIsNone(runner._agent)

    def test_mcp_decision_marks_the_goal_needs_you(self):
        runner = self.runner()
        runner.send("mcp-ask")
        self.assertTrue(runner.wait_idle(30))
        self.assertEqual(self.goal_status(), GoalStatus.NEEDS_YOU)
        self.assertEqual(self.states[-1]["state"], "waiting")
        runner.answer_decision("pick-colour-ab12", "green")
        self.assertEqual(self.store.read_answer("pick-colour-ab12").label, "Green")

    def test_session_is_resumed_natively_by_a_new_runner(self):
        first = self.runner()
        first.send("two-steps")
        self.assertTrue(first.wait_idle(30))
        session = first.session_id
        first.close()
        self.runners.remove(first)
        saved = json.loads(Path(self.root, ".playground/local/sessions.json").read_text())
        self.assertEqual(saved[self.goal.id]["session_id"], session)
        second = self.runner()
        second.send("two-steps again")
        self.assertTrue(second.wait_idle(30))
        methods = [e.get("method") for e in self.calls() if "method" in e]
        self.assertEqual(methods.count("session/new"), 1)
        self.assertIn("session/resume", methods)
        prompts = [e["prompt"] for e in self.calls() if "prompt" in e]
        self.assertNotIn("you're working on the goal", prompts[-1])  # a resumed session already has the context
        self.assertTrue(prompts[-1].endswith(TURN_END), "the turn-end reminder still closes every message")
        steps = [c for c in self.thread() if c.kind == "step"]
        self.assertEqual(len(steps), 4)  # each read was its own step, split by the agent's talk

    def test_switch_writes_a_handoff_and_briefs_the_next_agent(self):
        runner = self.runner()
        runner.send("two-steps")
        self.assertTrue(runner.wait_idle(30))
        codex = Connection(id="codex", label="Codex", kind="personal-plan", provider="codex", owner="noor")
        runner.switch(codex)
        self.assertTrue(runner.wait_idle(30))
        handoff = [c for c in self.thread() if c.kind == "handoff"][-1]
        self.assertEqual(handoff.data["from_connection"], "claude")
        self.assertEqual(handoff.data["to_connection"], "codex")
        self.assertIn("Say hi", handoff.data["brief"])
        self.assertIn("Read 1.", handoff.data["brief"])
        self.assertEqual(self.store.read_goal(self.goal.id).connection, "codex")
        self.assertEqual(self.states[-1]["state"], "handoff")
        runner.edit_handoff(handoff.data["brief"] + "\n\nAlso: keep it short.")
        runner.continue_handoff()
        self.assertTrue(runner.wait_idle(30))
        prompt = [e["prompt"] for e in self.calls() if "prompt" in e][-1]
        self.assertIn("keep it short", prompt)
        self.assertIn("You're picking up a goal", prompt)
        self.assertTrue(all(c.by == "codex" for c in self.thread()[-2:] if c.role == "agent"))

    def test_switching_a_brand_new_conversation_writes_no_handoff(self):
        runner = self.runner()
        codex = Connection(id="codex", label="Codex", kind="personal-plan", provider="codex", owner="noor")
        runner.switch(codex)
        self.assertTrue(runner.wait_idle(30))
        self.assertFalse(any(c.kind == "handoff" for c in self.thread()))
        self.assertEqual(self.store.read_goal(self.goal.id).connection, "codex")
        self.assertNotEqual(self.states[-1]["state"], "handoff")

    def test_switch_closes_steps_the_previous_agent_left_running(self):
        self.store.append_thread(self.goal.id, ThreadEntry(
            kind="step", role="agent", by="claude", id="stale-step",
            data={"title": "Working", "state": "running", "files": [], "tool_calls": []}))
        runner = self.runner()
        codex = Connection(id="codex", label="Codex", kind="personal-plan", provider="codex", owner="noor")
        runner.switch(codex)
        self.assertTrue(runner.wait_idle(30))
        steps = [c for c in self.thread() if c.kind == "step" and c.id == "stale-step"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].data["state"], "done")
        self.assertTrue(steps[0].data.get("cut_off"))

    def test_switch_replaces_the_previous_agents_model_list(self):
        runner = self.runner()
        runner._options = [{"id": "model", "name": "Model", "category": "model",
                            "options": [{"value": "old-model", "name": "Old"}]}]
        codex = Connection(id="codex", label="Codex", kind="personal-plan", provider="codex", owner="noor")
        with mock.patch("luma_playground.agents.models.available",
                        return_value=[{"value": "new-model", "name": "New"}]):
            runner.switch(codex)
            self.assertTrue(runner.wait_idle(30))
        options = {o["id"]: o for o in runner.options()}
        self.assertEqual([c["value"] for c in options["model"]["options"]], ["new-model"])

    def test_switch_to_the_same_account_carries_on(self):
        runner = self.runner()
        runner.send("two-steps")
        self.assertTrue(runner.wait_idle(30))
        renamed = Connection(id="claude", label="Claude (renamed)", kind="personal-plan", provider="claude", owner="noor")
        runner.switch(renamed)
        self.assertTrue(runner.wait_idle(30))
        self.assertFalse(any(c.kind == "handoff" for c in self.thread()))
        self.assertIsNotNone(runner._agent)

    def test_someone_elses_personal_plan_is_refused(self):
        runner = self.runner(person="priya")
        runner.send("hi")
        self.assertTrue(runner.wait_idle(30))
        last = self.thread()[-1]
        self.assertEqual(last.role, "system")
        self.assertIn("own plan", last.text)
        self.assertEqual(self.calls(), [])  # the agent was never started
        with self.assertRaises(Exception):
            runner.switch(Connection(id="noors", label="Noor's", kind="personal-plan", provider="codex", owner="noor"))

    def test_sign_in_needed_then_terminal_login_then_retry(self):
        self.agent_env["FAKE_ACP_AUTH"] = "1"
        runner = self.runner()
        runner.send("make hello")
        self.assertTrue(runner.wait_idle(30))
        last = self.thread()[-1]
        self.assertEqual(last.data.get("action"), "sign-in")
        self.assertIn("never sees your password", last.text)
        self.assertEqual(self.states[-1]["state"], "needs-sign-in")
        methods = {m["id"]: m for m in self.states[-1]["methods"]}
        self.assertEqual(methods["terminal-login"]["type"], "terminal")
        argv, env = runner.sign_in_command("terminal-login")
        login = acp.LoginTerminal(argv, env, self.root, prefer="pty")
        login.start()
        self.assertEqual(login.wait(20), 0)
        runner.signed_in()
        self.assertTrue(runner.wait_idle(30))
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")

    def test_a_crash_is_explained_and_the_next_message_restarts(self):
        runner = self.runner()
        runner.send("crash")
        self.assertTrue(runner.wait_idle(30))
        last = self.thread()[-1]
        self.assertEqual(last.role, "system")
        self.assertIn("stopped partway through", last.text)
        self.assertEqual(last.data["action"], "retry")
        self.assertEqual(self.goal_status(), GoalStatus.NEEDS_YOU)
        runner.send("make hello")
        self.assertTrue(runner.wait_idle(30))
        self.assertTrue(Path(self.root, "hello.txt").exists())

    def test_mcp_servers_come_from_the_project(self):
        servers = mcp_servers_for(self.root, self.goal.id, "claude")
        self.assertEqual(servers[0]["args"], ["--project", self.root])
        self.assertTrue(servers[0]["command"].endswith("playground-mcp"))


class RunnerAriTests(RunnerBase):
    def test_ari_runs_through_the_same_runner(self):
        fake = FakeOpenAI([
            {"text": "On it.", "tool_calls": [
                {"name": "update_plan", "arguments": {"entries": [{"content": "Write hello.txt", "status": "completed"}]}},
                {"name": "write_file", "arguments": {"path": "hello.txt", "content": "hi"}}]},
            {"text": "Done."}])
        url = fake.start()
        self.addCleanup(fake.stop)
        ari = Connection(id="ari-local", label="Ari", kind="ari", provider="ari", owner="noor", endpoint="ollama",
                         base_url=url, model="qwen3.8:27b")
        runner = GoalRunner(self.root, self.goal.id, ari, self.bus,
                            sandbox_policy=sandbox.SandboxPolicy(self.root, backend="none"))
        self.runners.append(runner)
        runner.send("Create a file hello.txt containing the word hi, then stop.")
        self.assertTrue(runner.wait_idle(30))
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")
        cards = [(c.kind, c.text or c.data.get("title", "")) for c in self.thread() if c.role == "agent"]
        self.assertEqual(cards, [("message", "On it."), ("plan", ""), ("step", "Write hello.txt"), ("message", "Done.")])
        self.assertEqual(self.goal_status(), GoalStatus.READY)


if __name__ == "__main__":
    unittest.main()
