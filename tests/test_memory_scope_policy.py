from __future__ import annotations

import unittest

from core import memory_scope_policy as policy


class ScopePolicyTest(unittest.TestCase):
    # --- public scene: all public convos are shared across souls ----------

    def test_public_posts_admitted_everywhere(self) -> None:
        for channel in ("public_post", "comment", "chat"):
            self.assertEqual(
                policy.classify("public", channel=channel, reply_soul="gotoh").verdict,
                policy.HARD,
            )

    def test_other_souls_public_comments_are_admissible_in_public_scene(self) -> None:
        # gotoh replying publicly can retrieve the user's comment-conversation with kita
        d = policy.classify("thread:20260616-001", channel="comment", reply_soul="gotoh")
        self.assertEqual(d.verdict, policy.HARD)
        self.assertTrue(d.admissible)
        self.assertFalse(d.needs_discretion)

    # --- private chat: only the owning soul, and only with discretion in public

    def test_own_private_is_soft_in_public_scene(self) -> None:
        d = policy.classify("private:soul:gotoh", channel="public_post", reply_soul="gotoh")
        self.assertEqual(d.verdict, policy.SOFT)
        self.assertTrue(d.admissible)
        self.assertTrue(d.needs_discretion)  # may retrieve, must self-judge before saying

    def test_own_private_is_free_in_private_scene(self) -> None:
        d = policy.classify("private:soul:gotoh", channel="chat", reply_soul="gotoh")
        self.assertEqual(d.verdict, policy.HARD)
        self.assertFalse(d.needs_discretion)

    def test_other_souls_private_is_always_forbidden(self) -> None:
        for channel in ("public_post", "comment", "chat"):
            d = policy.classify("private:soul:kita", channel=channel, reply_soul="gotoh")
            self.assertEqual(d.verdict, policy.FORBIDDEN)
            self.assertFalse(d.admissible)

    def test_no_acting_soul_gets_only_public(self) -> None:
        self.assertTrue(policy.classify("public", channel="public_post", reply_soul=None).admissible)
        self.assertFalse(
            policy.classify("private:soul:gotoh", channel="public_post", reply_soul=None).admissible
        )

    # --- query plan helper -------------------------------------------------

    def test_plan_public_scene_includes_own_private_with_discretion(self) -> None:
        plan = policy.admissible_visibility_filters("comment", "gotoh")
        self.assertTrue(plan["public"])
        self.assertEqual(plan["private_self"], "private:soul:gotoh")
        self.assertTrue(plan["private_self_needs_discretion"])

    def test_plan_private_scene_includes_own_private_freely(self) -> None:
        plan = policy.admissible_visibility_filters("chat", "gotoh")
        self.assertEqual(plan["private_self"], "private:soul:gotoh")
        self.assertFalse(plan["private_self_needs_discretion"])

    def test_plan_without_soul_is_public_only(self) -> None:
        plan = policy.admissible_visibility_filters("public_post", None)
        self.assertTrue(plan["public"])
        self.assertIsNone(plan["private_self"])

    def test_helpers(self) -> None:
        self.assertTrue(policy.is_public_visibility("public"))
        self.assertTrue(policy.is_public_visibility("thread:p1"))
        self.assertFalse(policy.is_public_visibility("private:soul:gotoh"))
        self.assertEqual(policy.private_soul_of("private:soul:gotoh"), "gotoh")
        self.assertIsNone(policy.private_soul_of("public"))


if __name__ == "__main__":
    unittest.main()
