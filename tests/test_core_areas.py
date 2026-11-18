# SPDX-License-Identifier: Apache-2.0
"""The harness: tasks and claims, the one-line indexes, rules in AGENTS.md, the team channel, upgrades."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from luma_playground.core import areas
from luma_playground.core.model import KnowledgeNote, Member
from luma_playground.core.paths import ProjectPaths
from luma_playground.core.project import create_project, upgrade_layout
from luma_playground.core.store import ProjectStore, StoreError


class GuideTests(unittest.TestCase):
    def test_guide_names_the_actor_env_var(self):
        self.assertIn("$PLAYGROUND_ACTOR", areas.GUIDE_MD)
        self.assertNotIn("agent:", areas.GUIDE_MD)

    def test_upgrading_a_project_does_not_rewrite_the_guide_for_each_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            project = create_project("Rally", "Rides to games", root=Path(folder), git=False,
                                     owner=Member(id="noor", name="Noor", role="owner"))
            guide = ProjectPaths(project.path).guide
            guide.write_text("an old guide", encoding="utf-8")
            self.assertIn("guide.md", upgrade_layout(project.path))
            first = guide.read_text()
            self.assertEqual(first, areas.GUIDE_MD)
            self.assertEqual(upgrade_layout(project.path), [])
            self.assertEqual(guide.read_text(), first)


class AreaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        project = create_project("Rally", "Rides to games", root=Path(self.tmp.name), git=False,
                                 owner=Member(id="noor", name="Noor", role="owner"))
        self.root = project.path
        self.store = ProjectStore(self.root)
        self.paths = ProjectPaths(self.root)

    def test_tasks_get_random_ids_and_show_in_the_index(self):
        a = self.store.create_task("Let parents offer rides", by="person:noor", priority="high")
        b = self.store.create_task("Show an empty schedule kindly", by="agent:codex@xps")
        self.assertRegex(a.id, r"^t-[0-9a-hjkmnp-tv-z]{5}$")
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(self.store.read_task(a.id).created_by, "person:noor")
        index = self.paths.index("tasks").read_text()
        self.assertIn(f"- {a.id} · high · Let parents offer rides", index.split("## Ready to pick up")[1])
        self.assertLess(index.index(a.id), index.index(b.id))   # high priority first
        self.assertNotIn("index", self.store.task_ids())

    def test_blocked_tasks_wait_and_ready_ones_lead(self):
        first = self.store.create_task("Save seats in the database", by="person:noor")
        later = self.store.create_task("Show seats on the class page", by="person:noor")
        self.store.update_task(later.id, lambda t: t.extra.__setitem__("blocked_by", [first.id]), by="person:noor")
        index = self.paths.index("tasks").read_text()
        ready, waiting = index.split("## Ready to pick up")[1].split("## Waiting")
        self.assertIn(first.id, ready)
        self.assertIn(f"blocked by {first.id}", waiting)
        self.store.update_task(first.id, lambda t: setattr(t, "status", "done"), by="person:noor")
        self.assertIn(later.id, self.paths.index("tasks").read_text().split("## Ready to pick up")[1].split("##")[0])

    def test_claims_keep_two_agents_off_the_same_task(self):
        task = self.store.create_task("Remind families", by="person:noor")
        claimed = self.store.claim_task(task.id, "agent:codex@xps")
        self.assertEqual((claimed.status, claimed.claimed_by), ("doing", "agent:codex@xps"))
        with self.assertRaises(StoreError):
            self.store.claim_task(task.id, "agent:claude@xps")
        self.store.claim_task(task.id, "agent:codex@xps")                # renewing your own claim is fine
        self.assertIn("claimed by agent:codex@xps", self.paths.index("tasks").read_text())
        expired = self.store.claim_task(task.id, "agent:codex@xps", hours=-1)
        self.assertEqual(expired.claim_holder(), "")
        self.store.claim_task(task.id, "agent:claude@xps")                # a lapsed claim is free
        done = self.store.update_task(task.id, lambda t: setattr(t, "status", "done"), by="agent:claude@xps")
        self.assertEqual((done.claimed_by, done.claimed_until), ("", ""))
        self.assertIn("## Recently finished", self.paths.index("tasks").read_text())

    def test_a_hand_edited_task_keeps_fields_playground_doesnt_know(self):
        task = self.store.create_task("Photo album", by="person:sam")
        path = self.paths.task(task.id)
        path.write_text(path.read_text().replace("labels: []", "labels: [photos]\nestimate: 2 days"))
        changed = self.store.update_task(task.id, lambda t: setattr(t, "priority", "low"), by="person:sam")
        self.assertEqual(changed.labels, ["photos"])
        self.assertIn("estimate: 2 days", path.read_text())

    def test_kept_rules_go_into_agents_md_and_suggested_ones_wait(self):
        self.store.write_knowledge(KnowledgeNote(slug="no-internal-notes", title="Never publish internal notes to GitHub",
                                                 extra={"kind": "rule", "status": "kept"}))
        self.store.write_knowledge(KnowledgeNote(slug="ask-before-costs", title="Ask before adding a monthly cost",
                                                 extra={"kind": "rule", "status": "suggested"}))
        self.store.write_knowledge(KnowledgeNote(slug="first-names", title="First names only on rides",
                                                 extra={"kind": "decision"}))
        agents = self.paths.agents_md.read_text()
        self.assertIn("- Never publish internal notes to GitHub\n" + areas.RULES_END, agents)
        self.assertNotIn("monthly cost", agents)
        index = self.paths.index("knowledge").read_text()
        self.assertIn("## Rules", index)
        self.assertIn("ask-before-costs.md · Ask before adding a monthly cost · suggested, not kept yet", index)
        self.assertIn("## Decisions", index)
        self.store.delete_knowledge("no-internal-notes")
        self.assertNotIn("internal notes", self.paths.agents_md.read_text())

    def test_a_hand_written_agents_md_is_never_touched(self):
        self.paths.agents_md.write_text("# Our own rules\n\nDo it our way.\n")
        self.store.write_knowledge(KnowledgeNote(slug="r", title="A rule", extra={"kind": "rule"}))
        self.assertEqual(self.paths.agents_md.read_text(), "# Our own rules\n\nDo it our way.\n")
        self.assertEqual(upgrade_layout(self.root), [])

    def test_bugs_index(self):
        self.store.create_issue("A reserved seat is lost after refreshing", severity="high")
        fixed = self.store.create_issue("Invitation shows an empty sender name")
        self.store.update_issue(fixed.number, lambda i: setattr(i, "status", "fixed"))
        index = self.paths.index("issues").read_text()
        self.assertIn("- #1 · open · high · A reserved seat is lost after refreshing", index)
        self.assertIn("## Recently closed", index)

    def test_team_channel_digest_is_short_and_recent(self):
        for n in range(30):
            self.store.post_channel(f"Step {n}", by="agent:codex@xps", kind="done")
        self.store.post_channel("Renaming src/auth to src/sign-in", by="agent:claude@xps", kind="heads-up",
                                files=["src/auth/"], task="t-7f3k2")
        digest = self.paths.channel_digest.read_text()
        self.assertEqual(digest.count("\n- "), areas.CHANNEL_DIGEST_LINES)
        self.assertIn("heads-up: Renaming src/auth to src/sign-in (t-7f3k2; files: src/auth/)", digest)
        self.assertNotIn("Step 0", digest)
        self.assertEqual(len(self.store.read_channel()), 31)
        with self.assertRaises(ValueError):
            self.store.post_channel("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", by="x")

    def test_old_projects_are_upgraded_once_and_keep_a_copy(self):
        old = ("# You are working inside Playground\n\nThis project is **Rally**\n\n## The Playground tools\n\n"
               "| When | Use |\n")
        self.paths.agents_md.write_text(old)
        self.paths.guide.unlink()
        self.paths.gemini_md.unlink()
        changed = upgrade_layout(self.root)
        self.assertEqual(sorted(changed), ["AGENTS.md", "GEMINI.md", "guide.md"])
        self.assertTrue(self.paths.agents_md.read_text().startswith("# Rally · a Playground project"))
        self.assertEqual((self.paths.local_dir / "AGENTS.md.before-upgrade").read_text(), old)
        self.assertEqual(upgrade_layout(self.root), [])

    def test_the_brief_says_what_changed_elsewhere_and_nothing_else(self):
        start = "2000-01-01T00:00:00Z"
        self.assertEqual(areas.brief_since(self.store, "2999-01-01T00:00:00Z"), "")
        self.store.post_channel("Renaming src/auth to src/sign-in", by="agent:claude@xps", kind="heads-up",
                                conversation="other-chat")
        self.store.post_channel("I'm on it", by="codex", kind="start", conversation="this-chat")
        self.store.create_task("Let parents offer rides", by="person:noor")
        self.store.create_issue("A reserved seat is lost", severity="high")
        self.store.write_knowledge(KnowledgeNote(slug="no-notes", title="Never publish internal notes",
                                                 extra={"kind": "rule", "status": "kept"}))
        text = areas.brief_since(self.store, start, conversation="this-chat", actor="codex")
        self.assertIn("heads-up: Renaming src/auth to src/sign-in", text)
        self.assertNotIn("I'm on it", text)                     # its own conversation's posts aren't news
        self.assertIn("new task: t-", text)
        self.assertIn("new bug: #1 A reserved seat is lost (high)", text)
        self.assertIn("new rule, follow it: Never publish internal notes", text)
        self.assertLessEqual(text.count("\n- "), areas.BRIEF_LINES)

    def test_a_sub_project_lives_inside_and_names_its_parent(self):
        child = create_project("Notes app", parent=self.root, git=False, owner=Member(id="noor", name="Noor"))
        self.assertEqual(child.path.parent, self.root)
        text = ProjectPaths(child.path).agents_md.read_text()
        self.assertIn("This is a sub-project of **", text)
        self.assertIn("(`../`): a focus, not a fence", text)
        self.assertIn("`project` on the playground tools", text)
        self.assertNotIn(str(self.root), text)
        self.assertTrue(ProjectPaths(child.path).index("tasks").is_file())


if __name__ == "__main__":
    unittest.main()


class AgentsMdRefreshTests(unittest.TestCase):
    def test_playgrounds_sections_follow_the_template_and_peoples_words_stay(self):
        from luma_playground.core import areas
        current = areas.render_agents_md("Kiln", "/p", summary="Bookings")
        old = current.replace("Keep part notes true when you change their code.", "Keep part notes true.").replace(
            "<!-- /playground:rules -->", "- Money is whole cents\n<!-- /playground:rules -->") + "\n## Ours\nKeep this.\n"
        fresh = areas.refresh_agents_md(old, current)
        self.assertIn("Keep part notes true when you change their code.", fresh)
        self.assertIn("- Money is whole cents", fresh)
        self.assertIn("Keep this.", fresh)
        self.assertEqual(areas.refresh_agents_md(fresh, current), fresh)
        mine = "# My own instructions\nNothing of Playground's here.\n"
        self.assertEqual(areas.refresh_agents_md(mine, current), mine)
