"""Standard-library checks. Windows tests spawn ONLY temporary test processes."""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import ModuleType, SimpleNamespace

from scripts.dev_runner import command_for, request_stop, read_state, owner_alive, sanitize_runtime_environment, ROOT

# Load the actual CLI parser without orchestrator.__init__ eagerly importing
# optional runtime dependencies; no mock copy of the parser is tested.
parse_args = runpy.run_path(str(ROOT / "orchestrator" / "trigger.py"))["parse_args"]


class PayloadTests(unittest.TestCase):
    def test_unicode_bom_file_and_legacy_payload(self):
        expected = {"audio_ref": "samples/中文 空格.wav"}
        with tempfile.TemporaryDirectory(prefix="payload space ") as folder:
            path = Path(folder) / "案例.json"
            path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8-sig")
            args = parse_args(["--workflow-def", "w.yaml", "--payload-file", str(path)])
            self.assertEqual(args.payload, expected)
        self.assertEqual(parse_args(["--workflow-def", "w", "--payload", '{"x":1}']).payload, {"x": 1})

    def test_invalid_inputs_fail_before_runtime_imports(self):
        cases = [[], ["--payload", "[]"], ["--payload", "null"], ["--payload", "invalid"],
                 ["--payload-file", "does-not-exist.json"],
                 ["--payload", "{}", "--payload-file", "unused.json"]]
        for case in cases:
            with self.subTest(case=case), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                parse_args(["--workflow-def", "w", *case])
            self.assertEqual(error.exception.code, 2)

    def test_services_do_not_own_ollama(self):
        command = command_for("services")
        self.assertNotIn("ollama", command)
        self.assertIn("agents", command)
        self.assertNotIn(".env", command)
        self.assertIn(".run/managed.empty.env", command)

    def test_generic_debug_does_not_leak_into_litellm(self):
        environment = {"DEBUG": "release", "KEEP": "yes"}
        sanitize_runtime_environment(environment, {})
        self.assertEqual(environment, {"KEEP": "yes"})
        with self.assertRaisesRegex(RuntimeError, "\.env DEBUG is not supported"):
            sanitize_runtime_environment({}, {"DEBUG": "release"})


class TriggerEventLoopTests(unittest.TestCase):
    def test_entrypoint_configures_policy_before_creating_event_loop(self):
        probe = "\n".join((
            "import asyncio, json",
            "import orchestrator.trigger as trigger",
            "before = type(asyncio.get_event_loop_policy()).__name__",
            "async def probe_main():",
            "    print(json.dumps({'before': before, 'policy': type(asyncio.get_event_loop_policy()).__name__, "
            "'loop': type(asyncio.get_running_loop()).__name__}))",
            "trigger.main = probe_main",
            "trigger.run()",
        ))
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        if os.name == "nt":
            self.assertEqual(result["policy"], "WindowsSelectorEventLoopPolicy")
            self.assertIn("SelectorEventLoop", result["loop"])
            self.assertNotIn("Proactor", result["loop"])
        else:
            self.assertEqual(result["policy"], result["before"])


class TriggerPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_shot_trigger_shares_and_always_closes_pool(self):
        import event_bus.factory
        import orchestrator.master_agent
        import orchestrator.run_state
        import orchestrator.trigger
        import orchestrator.workflow_def
        import persistence.pool

        pool = SimpleNamespace(close=AsyncMock())
        bus = SimpleNamespace(ensure_schema=AsyncMock())
        workflow = SimpleNamespace(name="test-workflow")
        argv = ["trigger", "--workflow-def", "test.yaml", "--payload", '{"x": 1}', "--thread-id", "test-thread"]

        for failure in (None, RuntimeError("publish failed")):
            pool.close.reset_mock()
            start_run = AsyncMock(side_effect=failure)
            with (
                patch.object(sys, "argv", argv),
                patch.object(persistence.pool, "get_shared_pool", AsyncMock(return_value=pool)),
                patch.object(event_bus.factory, "get_event_bus", return_value=bus) as get_bus,
                patch.object(orchestrator.workflow_def, "load_workflow_def", return_value=workflow),
                patch.object(orchestrator.run_state, "ensure_schema"),
                patch.object(orchestrator.master_agent, "start_run", start_run),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                if failure is None:
                    await orchestrator.trigger.main()
                else:
                    with self.assertRaisesRegex(RuntimeError, "publish failed"):
                        await orchestrator.trigger.main()
            get_bus.assert_called_once_with(pool=pool)
            pool.close.assert_awaited_once()


class DistillationTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_model_and_default_preserve_pending_gate(self):
        @contextlib.asynccontextmanager
        async def memory_context(_):
            yield object(), object()

        chat = Mock(return_value={"candidates": [{"rule": "verify actor", "evidence": ["case-1"]}]})
        remember = AsyncMock()
        recall = AsyncMock()
        imports = {
            "gateway.client": dict(chat_json=chat),
            "langgraph.store.base": dict(SearchItem=object),
            "persistence.call_log": dict(current_node_name=Mock(), current_thread_id=Mock()),
            "persistence.memory": dict(MemoryKind=SimpleNamespace(EPISODIC="episodic", PROCEDURAL="procedural"),
                                       parse_scope=lambda s: tuple(s.split("/")), recall=recall, remember=remember),
            "persistence.memory_lifespan": dict(open_agent_memory=memory_context),
        }
        modules = {}
        for name, attrs in imports.items():
            module = ModuleType(name)
            module.__dict__.update(attrs)
            modules[name] = module
        with patch.dict(sys.modules, modules):
            module = runpy.run_path(str(ROOT / "scripts" / "distill_procedural.py"))
        item = SimpleNamespace(key="case-1", value={"content": {"input": "example", "output": "false"}})
        for options, expected in (({}, "local-qwen3"), ({"model": "local-qwen"}, "local-qwen")):
            recall.side_effect = [[item], []]
            with contextlib.redirect_stdout(io.StringIO()):
                keys = await module["main"]("example/check", 20, **options)
            self.assertEqual(chat.call_args.args[0], expected)
            self.assertEqual(len(keys), 1)
            self.assertEqual(remember.call_args.kwargs["status"], "pending")


@unittest.skipUnless(os.name == "nt", "Windows process ownership")
class OwnershipTests(unittest.TestCase):
    def test_stop_preview_and_stale_pid_never_signal_another_process(self):
        from scripts.windows_job import process_birth
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / ".run").mkdir()
            state = dict(root=str(root), group="services", pid=os.getpid(),
                         birth=process_birth(os.getpid()), token="1" * 32, workflow="test")
            path = root / ".run" / "services.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            self.assertTrue(request_stop(root, "services", preview=True))
            self.assertFalse(list((root / ".run").glob("*.stop")))
            state["birth"] += 1
            path.write_text(json.dumps(state), encoding="utf-8")
            self.assertFalse(request_stop(root, "services"))
            self.assertFalse(list((root / ".run").glob("*.stop")))

    def test_supervisor_stop_kills_descendants_only(self):
        self.run_lifecycle(abrupt=False)

    def test_supervisor_exit_also_cleans_descendants(self):
        self.run_lifecycle(abrupt=True)

    def run_lifecycle(self, *, abrupt):
        with tempfile.TemporaryDirectory(prefix="dev 中文 ") as folder:
            root = Path(folder)
            beat = root / "heartbeat.txt"
            heartbeat = "import pathlib,time; p=pathlib.Path(" + repr(str(beat)) + ");\nwhile True:\n p.write_text(str(time.time())); time.sleep(.05)"
            spawn = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(heartbeat) + "]); time.sleep(60)"
            launch = "from pathlib import Path; from scripts.dev_runner import supervise; import sys; sys.exit(supervise(" + repr([sys.executable, "-c", spawn]) + ",Path(" + repr(str(root)) + "),'services','test'))"
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            child_env = os.environ.copy()
            child_env["PYTHONUTF8"] = "1"
            owner = subprocess.Popen([sys.executable, "-B", "-c", launch], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", env=child_env)
            try:
                deadline = time.monotonic() + 15
                while not beat.exists() and owner.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(beat.exists(), "supervised test child did not start")
                self.assertIsNotNone(read_state(root, "services"))
                if abrupt:
                    owner.kill()
                else:
                    self.assertTrue(request_stop(root, "services"))
                output, _ = owner.communicate(timeout=15)
                if not abrupt:
                    self.assertEqual(owner.returncode, 0, output)
                before = beat.read_text()
                time.sleep(.2)
                self.assertEqual(before, beat.read_text(), "grandchild survived Job cleanup")
                self.assertIsNone(unrelated.poll(), "unrelated process was stopped")
                state = read_state(root, "services")
                self.assertFalse(state and owner_alive(state))
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.communicate(timeout=10)
                unrelated.kill()
                unrelated.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
