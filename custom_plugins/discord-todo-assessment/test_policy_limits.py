import json
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_bridge import load_plugin


class PolicyLimitsTests(unittest.IsolatedAsyncioTestCase):
    def seed(self, m, path):
        s = m.RetrospectiveStore(path)
        baseline = s.active()
        run = dict(run_id='r', thread_id='t', user_id='u', capability='a'*64,
                   state='investigating', baseline=baseline, baseline_hash=baseline['hash'],
                   baseline_version=baseline['version'], payload={'corrections': [{'id': 'c'}]})
        with closing(s.connect()) as db, db:
            db.execute('INSERT INTO retrospective_runs VALUES (?,?,?)', ('r', 't', m.canonical(run)))
        # Reopen also exercises the old-schema migration path.
        with closing(s.connect()) as db, db:
            db.execute('DROP TABLE IF EXISTS draft_capabilities')
        return m.RetrospectiveStore(path)

    async def test_index_migrates_once_and_lookup_does_not_decode_history(self):
        m = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'p.db'
            s = self.seed(m, path)
            with closing(s.connect()) as db, db:
                for i in range(200):
                    db.execute('INSERT INTO retrospective_runs VALUES (?,?,?)',
                               (str(i), str(i), m.canonical(dict(capability=f'{i:064x}', state='applied', evidence='x'*10000))))
                db.execute('DROP TABLE draft_capabilities')
            s = m.RetrospectiveStore(path)
            # Existing index is the migration marker: reopening must not rescan history.
            real_loads = m.json.loads
            decoded = []
            def loads(value, *args, **kwargs):
                decoded.append(len(value))
                return real_loads(value, *args, **kwargs)
            with patch.object(m.json, 'loads', side_effect=loads):
                s = m.RetrospectiveStore(path)
                s.propose('a'*64, dict(rules=[], memory=[], summary='none'))
            self.assertLess(max(decoded), 10000)
            with closing(s.connect()) as db, db:
                rows = db.execute('SELECT capability_hash,run_id FROM draft_capabilities').fetchall()
                self.assertEqual(len(rows), 201)
                self.assertIn((m.hashlib.sha256(('a'*64).encode()).hexdigest(), 'r'), rows)
                plan = db.execute('EXPLAIN QUERY PLAN SELECT run_id FROM draft_capabilities WHERE capability_hash=?', ('x',)).fetchall()
                self.assertIn('SEARCH', str(plan))

    async def test_raw_fields_evidence_and_total_utf8_budget(self):
        m = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            s = self.seed(m, Path(d)/'p.db')
            for text, evidence in [(' '+ 'x' + ' '*5000, ['c']), ('😀'*1100, ['c']), ('ok', ['c']*101), ('ok', ['c','c']), ('ok', ['c'*129])]:
                with self.subTest(text=text[:10], count=len(evidence)), self.assertRaises(ValueError):
                    s.propose('a'*64, dict(rules=[dict(text=text,evidence_ids=evidence)],memory=[],summary='x'))
            with self.assertRaises(ValueError):
                s.propose('a'*64, dict(rules=[],memory=[],summary='x'+' '*5000))
            proposal = dict(rules=[dict(text='x'*3900,evidence_ids=['c']) for _ in range(5)], memory=[], summary='x')
            with self.assertRaises(ValueError): s.propose('a'*64, proposal)

    async def test_exact_policy_budget_and_next_assessment(self):
        m = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            s = self.seed(m, Path(d)/'p.db')
            proposal = dict(rules=[dict(text='x'*3900, evidence_ids=['c']) for _ in range(4)],
                            memory=[dict(text='😀', evidence_ids=['c'])], summary='exact budget')
            size = len(m.canonical({k:proposal[k] for k in ('rules','memory')}).encode())
            proposal['memory'][0]['text'] += 'x'*(16384-size)
            self.assertEqual(len(m.canonical({k:proposal[k] for k in ('rules','memory')}).encode()), 16384)
            draft = s.propose('a'*64, proposal)
            oversized = json.loads(m.canonical(proposal))
            oversized['memory'][0]['text'] += 'x'
            with self.assertRaises(ValueError): s.propose('a'*64, oversized)
            s.approve('r','u',draft['proposal_hash'])
            calls = []
            async def complete(**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(parsed={'decisions':[dict(action='dismiss', confidence=.9, reason='ok')]})
            ctx = SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete))
            await m.assess(ctx, dict(reports=[], labels=[], open_todos=[]), policy_store=s)
            self.assertEqual(len(calls), 1)
            self.assertIn('😀', calls[0]['instructions'])
            with self.assertRaises(ValueError):
                await m.assess(ctx, dict(reports=[dict(id='r',text='x'*m.ASSESSMENT_INPUT_MAX_BYTES)],labels=[],open_todos=[]),policy_store=s)
            self.assertEqual(len(calls), 1)

    async def test_maximum_policy_leaves_room_for_full_catalog_and_small_report(self):
        m = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            s = self.seed(m, Path(d)/'p.db')
            proposal = dict(rules=[dict(text='x'*3900, evidence_ids=['c']) for _ in range(4)],
                            memory=[dict(text='lesson', evidence_ids=['c'])], summary='catalog headroom fixture')
            size = len(m.canonical({k:proposal[k] for k in ('rules','memory')}).encode())
            proposal['memory'][0]['text'] += 'x'*(m.LEARNED_POLICY_MAX_BYTES-size)
            draft = s.propose('a'*64, proposal)
            s.approve('r','u',draft['proposal_hash'])
            # Representative 37-label PB records, including metadata normally returned by getFullList.
            labels = [dict(id=f'{i:015}', name=f'Plugin {i}', description='Plugin behavior and scope. '*8,
                           repo='https://github.com/Team-Sneakymouse/Example', guild_id='391355330241757205',
                           thread_id='', collectionId='todolabels00001', collectionName='todo_list_labels') for i in range(37)]
            payload = dict(reports=[dict(id='r', context='A small player report. '*20)], labels=labels, open_todos=[])
            self.assertGreater(len(m.canonical(payload).encode()), 10000)
            calls = []
            async def complete(**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(parsed={'decisions':[dict(action='dismiss', confidence=.9, reason='fixture only')]})
            await m.assess(SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete)),payload,policy_store=s)
            self.assertEqual(len(calls),1)

    async def test_migration_failure_rolls_back_without_deleting_history(self):
        m = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'p.db'
            s = self.seed(m, path)
            with closing(s.connect()) as db, db:
                db.execute('DROP TABLE draft_capabilities')
                db.execute('INSERT INTO retrospective_runs VALUES (?,?,?)', ('bad','bad','{}'))
            with self.assertRaises(KeyError): m.RetrospectiveStore(path)
            with closing(s.connect()) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM retrospective_runs').fetchone()[0], 2)
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='draft_capabilities'").fetchone())

    async def test_http_oversized_active_policy_is_422_and_tool_rejects_draft(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        m = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            s = self.seed(m, Path(d)/'p.db')
            tools = {}
            ctx = SimpleNamespace(register_tool=lambda name,**kw:tools.update({name:kw}),
                                  register_platform_handler=lambda *args:None)
            m.register(ctx)
            with patch.object(m, 'RetrospectiveStore', return_value=s):
                result = tools['discord_todo_retrospective_propose']['handler'](
                    dict(capability='a'*64,proposal=dict(rules=[],memory=[],summary='x'+' '*5000)))
                self.assertEqual(json.loads(result), {'error':'invalid_draft'})
            with closing(s.connect()) as db, db:
                policy = s.active(db)
                policy['rules'] = [dict(text='x'*3900,evidence_ids=['c']) for _ in range(5)]
                db.execute('UPDATE policy SET data=?', (m.canonical(policy),))
            app = web.Application()
            m.wire_http(app,ctx,'x'*32,retrospective_store=s)
            async with TestClient(TestServer(app)) as client:
                response = await client.post('/plugins/discord-todo/v1/assess',json=dict(reports=[],labels=[],open_todos=[]),headers={'Authorization':'Bearer '+'x'*32})
                self.assertEqual(response.status,422)

    async def test_legacy_oversize_rejected_at_approval_and_assessment(self):
        m = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            s = self.seed(m, Path(d)/'p.db')
            draft = s.propose('a'*64, dict(rules=[],memory=[],summary='x'))
            large = [dict(text='x'*3900,evidence_ids=['c']) for _ in range(5)]
            with closing(s.connect()) as db, db:
                run = s.run(db, 'r'); run['proposal']['rules'] = large; s.save(db, run)
            with self.assertRaises(ValueError): s.prepare_approval('r','u',draft['proposal_hash'])
            with self.assertRaises(ValueError): s.approve('r','u',draft['proposal_hash'])
            self.assertEqual(s.active()['version'], '2')
            with closing(s.connect()) as db, db:
                policy = s.active(db); policy['rules'] = large
                db.execute('UPDATE policy SET data=?', (m.canonical(policy),))
            async def unexpected(**kwargs): self.fail('oversized policy reached model')
            with self.assertRaises(ValueError):
                await m.assess(SimpleNamespace(llm=SimpleNamespace(acomplete_structured=unexpected)), dict(reports=[],labels=[],open_todos=[]), policy_store=s)
