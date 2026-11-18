"""Regression checks for durable, project-scoped task execution."""

import os
os.environ["LANGSMITH_TRACING"] = "false"

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from maf import department as d
from maf.events.store import EventStore
from maf.model_config import ModelConfig
from maf.projects import Project

ROOT = Path(__file__).resolve().parents[1]


class FakeTeam:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.on_attempt = None
        config = ModelConfig.load(ROOT)
        self.profiles = {role: config.profile(role) for role, _ in config.roles}

    def _ask(self, role, prompt, schema, **context):
        self.on_attempt('nine_router', 'test-model')
        response = next(self.responses)
        if callable(response):
            response = response()
        return schema.model_validate(response)


def action(tool):
    return dict(message='Working on the task', tool=tool)


def plan():
    return {'outcome': {'action': 'plan', 'summary': 'Express and SQLite project', 'tasks': [
        dict(id='dev', title='Implement route', role='developer', instructions='Create server.js',
             acceptance=['Server route created'], depends_on=[], estimate_minutes=2, max_calls=5),
        dict(id='qa', title='Test route', role='qa', instructions='Run tests',
             acceptance=['Tests pass'], depends_on=['dev'], estimate_minutes=2, max_calls=4),
    ]}}


class DepartmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        workspace = self.root / 'project'
        workspace.mkdir()
        (workspace / '.maf').mkdir()
        self.project = Project('a' * 32, 'Test', workspace, 'Express and SQLite')
        self.global_database = self.root / 'global.sqlite'
        with closing(sqlite3.connect(self.global_database)) as connection:
            EventStore(connection)
        self.run_id = 'b' * 32
        d.create(self.project, self.global_database, self.run_id, 'Build inventory app with Express')

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        return d.load(self.project, self.run_id)

    def tick(self, response):
        state = self.state()
        state['next_call_at'] = 0
        d.save(self.project, state)
        team = FakeTeam([response])
        with patch('maf.department.ModelTeam.from_config', return_value=team):
            d.build_worker(self.project, self.global_database, ROOT).invoke({'run_id': self.run_id})

    def approve(self):
        state = self.state()
        d.decide(self.project, self.global_database, state, 'approve', state['pending_approvals'][0]['id'])

    def test_plan_approval_tools_qa_and_finish(self):
        self.tick(plan())
        self.assertEqual(self.state()['status'], 'waiting_approval')
        self.assertFalse((self.project.workspace / 'server.js').exists())
        self.approve()
        self.tick(action(dict(action='write_file', path='server.js', content='console.log("ok")')))
        self.assertTrue((self.project.workspace / 'server.js').exists())
        self.tick(action(dict(action='finish', summary='Route implemented')))
        self.tick(action(dict(action='command', command='printf "tests passed\\n"', timeout_seconds=5)))
        self.tick(action(dict(action='finish', summary='QA command passed')))
        self.tick(action(dict(action='finish', summary='unused')))
        self.assertEqual(self.state()['status'], 'done')
        with closing(sqlite3.connect(self.project.storage / 'events.sqlite')) as connection:
            types = {r[0] for r in connection.execute('select type from events')}
            self.assertTrue({'llm.call', 'agent.message', 'command.finished', 'run.finished'} <= types)

    def test_qa_cannot_finish_without_real_tests(self):
        self.tick(plan())
        self.approve()
        self.tick(action(dict(action='finish', summary='Dev done')))
        self.tick(action(dict(action='finish', summary='Invented QA pass')))
        self.assertEqual(self.state()['status'], 'blocked')
        self.assertIn('QA', self.state()['failures'][0]['error'])

    def test_change_during_model_call_cancels_old_tool_and_requires_approval(self):
        self.tick(plan())
        self.approve()
        entered, release = threading.Event(), threading.Event()
        def response():
            entered.set()
            self.assertTrue(release.wait(3))
            return action(dict(action='write_file', path='stale.js', content='old request'))
        state = self.state()
        state['next_call_at'] = 0
        d.save(self.project, state)
        with patch('maf.department.ModelTeam.from_config', return_value=FakeTeam([response])):
            worker = threading.Thread(target=d.tick, args=(self.project, self.global_database, self.run_id, ROOT))
            worker.start()
            self.assertTrue(entered.wait(3))
            d.decide(self.project, self.global_database, self.state(), 'change_spec', request='Add stock transfers')
            release.set()
            worker.join(4)
            self.assertFalse(worker.is_alive())
        self.assertFalse((self.project.workspace / 'stale.js').exists())
        self.tick(plan())
        self.assertEqual(self.state()['spec_version'], 2)
        self.assertEqual(self.state()['status'], 'waiting_approval')
        self.assertIn('stock transfers', self.state()['request'])

    def test_budget_pause_and_resume(self):
        state = self.state()
        state['max_calls'] = 0
        d.save(self.project, state)
        self.tick(plan())
        self.assertEqual(self.state()['status'], 'blocked')
        d.decide(self.project, self.global_database, self.state(), 'continue')
        self.assertEqual(self.state()['phase'], 'planning')
        self.tick(plan())
        self.approve()
        d.decide(self.project, self.global_database, self.state(), 'pause')
        self.tick(action(dict(action='finish', summary='unused')))
        self.assertEqual(self.state()['status'], 'paused')

    def test_greeting_and_dependency_validation(self):
        self.tick({'outcome': {'action': 'reply', 'message': 'Xin chào'}})
        self.assertEqual(self.state()['status'], 'responded')
        bad = plan()['outcome']
        bad['tasks'][0]['depends_on'] = ['qa']
        with self.assertRaises(ValueError):
            d.ProjectPlan.model_validate(bad)
        with self.assertRaises(ValueError):
            d.safe_path(self.project, '../outside.txt')


if __name__ == '__main__':
    unittest.main()


class GatewayDepartmentTests(unittest.TestCase):
    def test_async_submission_and_pm_progress(self):
        from maf.gateway import create_app, ProjectRequest, RunRequest
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, MAF_GATEWAY_TOKEN='test', MAF_N8N_EVENT_URL='', MAF_WORKSPACE_ROOT=temp):
            app = create_app(data_dir=Path(temp) / 'gateway')
            routes = {(method, route.path): route.endpoint for route in app.routes[-1].original_router.routes for method in route.methods}
            project = routes['POST', '/projects'](ProjectRequest(name='Inventory', workspace=temp, folder='inventory'))
            with patch('maf.department.ModelTeam.from_config', side_effect=AssertionError('Submission must not call a model')):
                state = routes['POST', '/runs'](RunRequest(project_id=project['id'], request='Build Express inventory', max_calls=40, calls_per_minute=2))
                self.assertEqual(state['status'], 'planning')
                self.assertEqual(state['max_calls'], 40)
            self.assertEqual(routes['GET', '/runs/{run_id}'](state['run_id'])['phase'], 'planning')
            listed = routes['GET', '/runs'](100, project['id'])
            self.assertEqual(listed['runs'][0]['project_id'], project['id'])
            routes['POST', '/runs/{run_id}/pause'](state['run_id'])
            self.assertTrue(routes['GET', '/runs/{run_id}'](state['run_id'])['pause_requested'])
