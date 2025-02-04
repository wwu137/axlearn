# Copyright © 2023 Apple Inc.

"""Tests for module.py."""
# pylint: disable=protected-access
import threading
from typing import List, Optional

import jax.random
import numpy as np
from absl.testing import absltest
from jax import numpy as jnp

from axlearn.common.config import REQUIRED, Required, config_class
from axlearn.common.module import (
    InvocationContext,
    Module,
    OutputConflictError,
    Tensor,
    _global_context_stack,
    child_context,
    clone_context_stack,
    current_context,
)
from axlearn.common.module import functional as F
from axlearn.common.module import install_context_stack, new_output_collection, set_current_context
from axlearn.common.test_utils import TestWithTemporaryCWD


class OutputCollectionTest(absltest.TestCase):
    def test_output_collection_children(self):
        c = new_output_collection()
        c.summaries["x"] = 1
        c1 = c.add_child("c1")
        c1.summaries["y"] = 2
        c2 = c.add_child("c2")
        c2.summaries["z"] = 3
        self.assertEqual({"x": 1, "c1": {"y": 2}, "c2": {"z": 3}}, c.summaries)

    def test_output_collection_update(self):
        c1 = new_output_collection()
        c1.summaries["x"] = 1
        c1.state_updates["x"] = 2
        c1.module_outputs["x"] = 3

        c2 = new_output_collection()
        c2.summaries["y"] = 4
        c2.state_updates["y"] = 5
        c2.module_outputs["y"] = 6

        c1.update(c2)
        self.assertEqual({"x": 1, "y": 4}, c1.summaries)
        self.assertEqual({"x": 2, "y": 5}, c1.state_updates)
        self.assertEqual({"x": 3, "y": 6}, c1.module_outputs)


class TestModule(Module):
    pass


def new_test_module(name: str) -> TestModule:
    return TestModule.default_config().set(name=name).instantiate(parent=None)


class InvocationContextTest(absltest.TestCase):
    def test_context_output_collection(self):
        context = InvocationContext(
            name="root",
            parent=None,
            module=new_test_module("test"),
            is_training=True,
            prng_key=jax.random.PRNGKey(123),
            state={"x": 1},
            output_collection=new_output_collection(),
        )
        context.add_summary("x", 1)
        context.add_summary("y", 2)
        context.add_state_update("z", 3)
        self.assertEqual({"x": 1, "y": 2}, context.get_summaries())
        self.assertEqual({"z": 3}, context.get_state_updates())

    def test_context_stack(self):
        module1 = new_test_module("test1")
        module2 = new_test_module("test2")
        context1 = InvocationContext(
            name="context1",
            parent=None,  # root context
            module=module1,
            is_training=True,
            prng_key=jax.random.PRNGKey(123),
            state={"x": 1},
            output_collection=new_output_collection(),
        )
        with set_current_context(context1):
            self.assertIs(current_context(), context1)
            self.assertEqual(current_context().state["x"], 1)  # pytype: disable=attribute-error
            with self.assertRaisesRegex(ValueError, "parent must match the current context"):
                with set_current_context(context1):
                    pass
            with child_context("test2", module=module2, state={"x": 2}) as context2:
                self.assertIs(current_context(), context2)
                self.assertEqual(current_context().state["x"], 2)  # pytype: disable=attribute-error

            # No longer in context2, but still in context1.
            self.assertIs(current_context(), context1)
            self.assertEqual(current_context().state["x"], 1)  # pytype: disable=attribute-error

        context2 = context1.add_child("context2", module=module2, state={"x": 2})
        with self.assertRaisesRegex(ValueError, "must be a root context"):
            with set_current_context(context2):
                pass

    def test_context_stack_mutlithread(self):
        module1 = new_test_module("root")
        module1._add_child("child1", TestModule.default_config())
        module1._add_child("child2", TestModule.default_config())
        context1 = InvocationContext(
            name="root",
            parent=None,  # root context
            module=module1,
            is_training=True,
            prng_key=jax.random.PRNGKey(123),
            state={"child1": {"x": 1}, "child2": {"x": 2}},
            output_collection=new_output_collection(),
        )
        with set_current_context(context1):
            xs = set()
            barrier = threading.Barrier(2)
            orig_stack = _global_context_stack.stack

            def run_with_child_context(child_name, context_stack):
                self.assertIsNot(context_stack, orig_stack)
                self.assertIs(_global_context_stack.stack, orig_stack)
                # A thread should install the cloned context stack first.
                install_context_stack(context_stack)
                self.assertIs(_global_context_stack.stack, context_stack)

                # Now child_context() between different threads will not interfere with each other.
                with child_context(child_name):
                    x = current_context().state["x"]  # pytype: disable=attribute-error
                    xs.add(x)
                    barrier.wait()

            threads = []
            for child_name in ("child1", "child2"):
                # When creating a thread, clone the context stack to be installed inside the thread.
                t = threading.Thread(
                    target=run_with_child_context, args=[child_name, clone_context_stack()]
                )
                t.start()
                threads.append(t)

            # install_context_stack() calls in the threads do not interfere with the main stack.
            self.assertIs(_global_context_stack.stack, orig_stack)
            for t in threads:
                t.join(timeout=1)
            self.assertIs(_global_context_stack.stack, orig_stack)
            self.assertEqual(xs, {1, 2})

    def test_none_prng_key(self):
        module1 = new_test_module("test1")
        module2 = new_test_module("test2")
        context1 = InvocationContext(
            name="context1",
            parent=None,
            module=module1,
            prng_key=None,
            is_training=True,
            state={"x": 1},
            output_collection=new_output_collection(),
        )
        with set_current_context(context1):
            context2 = context1.add_child("context2", module=module2, state={"x": 2})
            self.assertEqual(context2.prng_key, None)


class NestedModule(Module):
    """A nested module."""

    @config_class
    class Config(Module.Config):
        child: Optional[Module.Config] = None

    def __init__(self, cfg: Config, *, parent: Module):
        super().__init__(cfg, parent=parent)
        cfg = self.config
        if cfg.child is not None:
            self._add_child("child1", cfg.child)
            self._add_child("child2", cfg.child)

    def forward(self, x: Tensor) -> Tensor:
        cfg = self.config
        # It is ok to call another method of 'self' directly, if it is invoked only once in the
        # current context.
        self.vprint(1, "input={x} cfg.child=\n{child}", x=x, child=str(cfg.child))
        x = self.inc(x)
        if cfg.child is not None:
            # Can also call children directly, if each child is invoked only once.
            x = self.child1(x)
            x = self.child2(x)
        self.vprint(1, "output={x}", x=x)
        return x

    def invoke_grandchildren(self, x):
        x = self.child1.child2(x)
        x = self.child2.child1(x)
        return x

    def forward2(self, x: Tensor) -> Tensor:
        cfg = self.config
        x = self.inc(x)
        x = self.inc(x)
        if cfg.child is not None:
            # Can also call children's method directly, if each child is invoked only once and
            # the method is wrapped for auto child context.
            x = self.child1.forward2(x)
            x = self.child2.forward2(x)
        return x

    def inc(self, x: Tensor) -> Tensor:
        self.add_summary("x", x)
        y = x + self.state["inc"]
        self.add_module_output("y", y)
        return y

    def invoke_self_multiple_times_causing_conflict(self, x: Tensor, n: int) -> Tensor:
        for _ in range(n):
            x = self.inc(x)
        return x

    def invoke_self_multiple_times(self, x: Tensor, n: int) -> Tensor:
        # To call self.inc multiple times, create a context explicitly for each invocation
        # with a different `name`.
        for i in range(n):
            with child_context(f"self_call{i}", module=self):
                x = self.inc(x)
        return x

    def invoke_child_multiple_times_causing_conflict(self, x: Tensor, n: int) -> Tensor:
        for _ in range(n):
            x = self.child1(x)
        return x

    def invoke_child_multiple_times(self, x: Tensor, n: int) -> Tensor:
        # To call a child multiple times, create a child context explicitly for each invocation
        # with a different `name`.
        for i in range(n):
            with child_context(f"child1_call{i}", module=self.child1):
                x = self.child1(x)
        return x


class ModuleConsumingSharedChild(Module):
    """A module to consume a shared child."""

    def forward(self, x: Tensor) -> Tensor:
        shared_module = self.get_shared_module("shared")
        return x * shared_module.state["weight"]


class ModuleProvidingSharedChild(Module):
    """A module to provide a shared child."""

    @config_class
    class Config(Module.Config):
        shared_child: Required[Module.Config] = REQUIRED
        child: Required[Module.Config] = REQUIRED
        shared_module_name: str = "shared"

    def __init__(self, cfg: Config, *, parent: Module):
        super().__init__(cfg, parent=parent)
        cfg = self.config
        self._add_child("shared_child", cfg.shared_child)
        self._share_with_descendants(self.shared_child, shared_module_name=cfg.shared_module_name)
        self._add_child("child1", cfg.child)
        self._add_child("child2", cfg.child)

    def forward(self, x: Tensor) -> Tensor:
        return self.child1(x) + self.child2(x)

    def get_descendant_shared_module(self, *, path: List[str], shared_module_name: str):
        # Note: this function assumes that inner nodes (non-leaves) of the module hierarchy are
        # `ModuleProvidingSharedChild`s.
        part, *path = path
        module = getattr(self, part)
        with child_context(part, module=module):
            if not path:
                return module.get_shared_module(shared_module_name)
            return module.get_descendant_shared_module(
                path=path, shared_module_name=shared_module_name
            )


class ModuleTest(TestWithTemporaryCWD):
    def test_parent_children(self):
        cfg = NestedModule.default_config().set(
            name="root", child=NestedModule.default_config(), vlog=2
        )
        root: NestedModule = cfg.instantiate(parent=None)
        self.assertIsInstance(root, NestedModule)
        self.assertEqual(root.path(), "root")
        self.assertSetEqual(set(root.children.keys()), {"child1", "child2"})

        child1 = root.child1
        self.assertIsInstance(child1, NestedModule)
        self.assertEqual(child1.path(), "root.child1")
        self.assertSetEqual(set(child1.children.keys()), set())

        # Call 'root.forward'.
        one = jnp.ones([])
        root_state = {"inc": one, "child1": {"inc": one * 10}, "child2": {"inc": one * 20}}
        y, output_collection = F(
            root,
            state=root_state,
            prng_key=jax.random.PRNGKey(1),
            is_training=True,
            inputs=dict(x=jnp.zeros([])),
        )
        np.testing.assert_array_equal(y, 31)
        self.assertEqual(
            output_collection.summaries,
            {
                "x": 0,
                "child1": {"x": 1},
                "child2": {"x": 11},
            },
        )

        with self.assertRaisesRegex(OutputConflictError, "multiple times"):
            F(
                root,
                method="invoke_self_multiple_times_causing_conflict",
                state=root_state,
                prng_key=jax.random.PRNGKey(1),
                is_training=True,
                inputs=dict(x=jnp.zeros([]), n=3),
            )

        # Call 'root.invoke_self_multiple_times'.
        y, output_collection = F(
            root,
            method="invoke_self_multiple_times",
            state=root_state,
            prng_key=jax.random.PRNGKey(1),
            is_training=True,
            inputs=dict(x=jnp.zeros([]), n=3),
        )
        np.testing.assert_array_equal(y, 3)
        self.assertEqual(
            output_collection.summaries,
            {
                "self_call0": {"x": 0},
                "self_call1": {"x": 1},
                "self_call2": {"x": 2},
            },
        )

        with self.assertRaisesRegex(OutputConflictError, "child1 already present"):
            F(
                root,
                method="invoke_child_multiple_times_causing_conflict",
                state=root_state,
                prng_key=jax.random.PRNGKey(1),
                is_training=True,
                inputs=dict(x=jnp.zeros([]), n=3),
            )

        # Call 'root.invoke_child_multiple_times'.
        y, output_collection = F(
            root,
            method="invoke_child_multiple_times",
            state=root_state,
            prng_key=jax.random.PRNGKey(1),
            is_training=True,
            inputs=dict(x=jnp.zeros([]), n=3),
        )
        np.testing.assert_array_equal(y, 30)
        self.assertEqual(
            output_collection.summaries,
            {
                "child1_call0": {"x": 0},
                "child1_call1": {"x": 10},
                "child1_call2": {"x": 20},
            },
        )

    def test_invoking_grandchildren(self):
        cfg = NestedModule.default_config().set(
            name="root",
            child=NestedModule.default_config().set(
                child=NestedModule.default_config(),
            ),
        )
        root: NestedModule = cfg.instantiate(parent=None)

        # Call 'root.forward'.
        one = jnp.ones([])
        root_state = {
            "inc": one,
            "child1": {
                "inc": one * 10,
                "child1": {"inc": one * 11},
                "child2": {"inc": one * 12},
            },
            "child2": {
                "inc": one * 20,
                "child1": {"inc": one * 21},
                "child2": {"inc": one * 22},
            },
        }
        y, output_collection = F(
            root,
            state=root_state,
            prng_key=jax.random.PRNGKey(1),
            is_training=True,
            inputs=dict(x=jnp.zeros([])),
            method="invoke_grandchildren",
        )
        # invoke_grandchildren calls child1.child2 and child2.child1.
        np.testing.assert_array_equal(y, 12 + 21)
        self.assertEqual(
            output_collection.summaries,
            {
                "child1": {"child2": {"x": 0}},
                "child2": {"child1": {"x": 12}},
            },
        )

    def test_shared_modules(self):
        cfg = ModuleProvidingSharedChild.default_config().set(
            name="root",
            shared_child=TestModule.default_config(),
            child=ModuleConsumingSharedChild.default_config(),
        )
        root: NestedModule = cfg.instantiate(parent=None)
        self.assertSetEqual(set(root.children.keys()), {"shared_child", "child1", "child2"})
        self.assertEqual(root._paths_to_shared_modules, {"shared": ["shared_child"]})

        # It is an error to share a child under the same name.
        with self.assertRaisesRegex(ValueError, "shared"):
            root._share_with_descendants(root.child1, shared_module_name="shared")

        # Call 'root.forward' with `w` as the weight of shared_child.
        x = jnp.ones([]) * 2

        def fn(w: Tensor):
            root_state = {
                "shared_child": {
                    "weight": w,
                },
                "child1": {},
            }
            y, _ = F(
                root,
                state=root_state,
                prng_key=jax.random.PRNGKey(1),
                is_training=True,
                inputs=dict(x=x),
            )
            # root_state is not mutated.
            self.assertSetEqual(set(root_state.keys()), {"shared_child", "child1"})
            self.assertSetEqual(set(root_state["child1"].keys()), set())
            return y

        w = jnp.ones([]) * 3
        value, grads = jax.value_and_grad(fn)(w)
        self.assertEqual(value, w * x * 2)
        # Verify that gradients are propagated back to 'w'.
        self.assertEqual(grads, x * 2)

    def test_multiple_shared_modules_from_ancestors(self):
        cfg = ModuleProvidingSharedChild.default_config().set(
            name="root",
            shared_child=TestModule.default_config(),
            child=ModuleProvidingSharedChild.default_config().set(
                shared_child=TestModule.default_config(),
                child=ModuleConsumingSharedChild.default_config(),
            ),
        )
        root: NestedModule = cfg.instantiate(parent=None)
        self.assertSetEqual(set(root.children.keys()), {"shared_child", "child1", "child2"})
        self.assertEqual(root._paths_to_shared_modules, {"shared": ["shared_child"]})

        # child1 has its own 'shared_child', which is visible to its descendants.
        self.assertEqual(root.child1._paths_to_shared_modules, {"shared": ["shared_child"]})
        # child2 also has its own 'shared_child'.
        self.assertEqual(root.child2._paths_to_shared_modules, {"shared": ["shared_child"]})

        # We can also share an indirect descendant.
        root._share_with_descendants(root.child1.child1, shared_module_name="child_1_1")
        self.assertEqual(
            root._paths_to_shared_modules,
            {"shared": ["shared_child"], "child_1_1": ["child1", "child1"]},
        )
        self.assertEqual(root.child1._paths_to_shared_modules, {"shared": ["shared_child"]})
        self.assertEqual(root.child1.child2._paths_to_shared_modules, {})
        self.assertEqual(root.child2._paths_to_shared_modules, {"shared": ["shared_child"]})
        self.assertEqual(root.child2.child2._paths_to_shared_modules, {})

    def test_get_shared_module(self):
        cfg = ModuleProvidingSharedChild.default_config().set(
            name="root",
            # A subtree that shares modules within.
            shared_child=ModuleProvidingSharedChild.default_config().set(
                shared_child=TestModule.default_config(),
                child=ModuleConsumingSharedChild.default_config(),
                shared_module_name="inner_shared",
            ),
            shared_module_name="outer_shared",
            # A child that is outside of the subtree that shares modules.
            # While it is able to access the subtree itself (due to its parent sharing the subtree),
            # it cannot access the shared modules within it, because its parent (or more generally,
            # any ancestor) does not explicitly share them.
            child=NestedModule.default_config(),
        )
        root: NestedModule = cfg.instantiate(parent=None)
        root_state = {
            "shared_child": {
                "shared_child": {"weight": jnp.ones([])},
                "child1": {},
                "child2": {},
            },
            "child1": {},
            "child2": {},
        }

        # Test that we can access "outer_shared" from within the "outer_shared" subtree.
        for path in ["shared_child", "shared_child/child1", "shared_child/child2"]:
            module, _ = F(
                root,
                state=root_state,
                prng_key=jax.random.PRNGKey(1),
                is_training=True,
                method="get_descendant_shared_module",
                inputs=dict(path=path.split("/"), shared_module_name="outer_shared"),
            )
            self.assertIs(module.module, root.shared_child)
            self.assertEqual(module.state, root_state["shared_child"])

        # Test that we can access "inner_shared" from within the "outer_shared" subtree.
        for path in ["shared_child", "shared_child/child1", "shared_child/child2"]:
            module, _ = F(
                root,
                state=root_state,
                prng_key=jax.random.PRNGKey(1),
                is_training=True,
                method="get_descendant_shared_module",
                inputs=dict(path=path.split("/"), shared_module_name="inner_shared"),
            )
            self.assertIs(module.module, root.shared_child.shared_child)
            self.assertEqual(module.state, root_state["shared_child"]["shared_child"])

        # Test that we can access "outer_shared" from its sibling.
        module, _ = F(
            root,
            state=root_state,
            prng_key=jax.random.PRNGKey(1),
            is_training=True,
            method="get_shared_module",
            inputs=dict(shared_module_name="outer_shared"),
        )
        self.assertIs(module.module, root.shared_child)
        self.assertEqual(module.state, root_state["shared_child"])

        # Test that we cannot access "inner_shared" from outside of "outer_shared".
        with self.assertRaisesRegex(ValueError, "ancestor that shares 'inner_shared'"):
            F(
                root,
                state=root_state,
                prng_key=jax.random.PRNGKey(1),
                is_training=True,
                method="get_descendant_shared_module",
                inputs=dict(path=["child1"], shared_module_name="inner_shared"),
            )

        # Test when state doesn't align with the module path.
        with self.assertRaisesRegex(ValueError, "state does not contain 'shared_child'"):
            F(
                root,
                state={},
                prng_key=jax.random.PRNGKey(1),
                is_training=True,
                method="get_shared_module",
                inputs=dict(shared_module_name="outer_shared"),
            )


if __name__ == "__main__":
    absltest.main()
