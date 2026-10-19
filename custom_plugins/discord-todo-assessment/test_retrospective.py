import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from test_bridge import load_plugin

class RetrospectiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_routes_tool_registration_and_runtime_policy(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        m=load_plugin()
        with tempfile.TemporaryDirectory() as d:
            s=m.RetrospectiveStore(Path(d)/'policy.db')
            calls=[]; tools={}; factories={}
            async def complete(**kw):
                calls.append(kw)
                return SimpleNamespace(parsed={'decisions':[dict(action='dismiss',confidence=.9,reason='fixture')]})
            ctx=SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete), inject_message=lambda text,**kw: calls.append(text) or True,register_tool=lambda name,**kw:tools.update({name:kw}),register_platform_handler=lambda name,f:factories.update({name:f}),get_config=lambda key,default=None:default)
            m.register(ctx)
            self.assertIn('discord_todo_retrospective_propose',tools)
            async def fetch(t): return SimpleNamespace(id=int(t),parent_id=1560943737688039474,guild=SimpleNamespace(id=555555555555555555))
            app=web.Application()
            m.wire_http(app,ctx,'x'*32,start_options=dict(fetch_thread=fetch,allowed_parents=['1560943737688039474'],state_path=Path(d)/'starts.db'),retrospective_store=s)
            p=dict(run_id='r',thread_id='111111111111111111',user_id='333333333333333333',guild_id='555555555555555555',corrections=[dict(id='c')],context='evidence')
            headers={'Authorization':'Bearer '+'x'*32}
            async with TestClient(TestServer(app)) as c:
                resp=await c.post('/plugins/discord-todo/v1/retrospective/start',json=p,headers=headers)
                self.assertEqual(resp.status,202)
                cap=calls[0].split('DRAFT_CAPABILITY=')[1].split('\n')[0]
                draft=s.propose(cap,dict(rules=[],memory=[dict(text='Runtime lesson',evidence_ids=['c'])],summary='Useful memory'))
                request=dict(run_id='r',user_id=p['user_id'],proposal_hash=draft['proposal_hash'])
                resp=await c.post('/plugins/discord-todo/v1/retrospective/prepare-approval',json=request)
                self.assertEqual(resp.status,401)
                for bad in [dict(request,user_id='wrong'),dict(request,proposal_hash='0'*64),dict(request,proposal_hash='short')]:
                    resp=await c.post('/plugins/discord-todo/v1/retrospective/prepare-approval',json=bad,headers=headers)
                    self.assertEqual(resp.status,422)
                resp=await c.post('/plugins/discord-todo/v1/retrospective/prepare-approval',json=request,headers=headers)
                self.assertEqual(resp.status,200)
                frozen=await resp.json()
                self.assertEqual(frozen['state'],'approval_pending')
                self.assertEqual(frozen['proposal'],draft['proposal'])
                self.assertEqual(s.active()['version'],'2')
                with self.assertRaises(ValueError): s.propose(cap,dict(rules=[],memory=[],summary='replacement'))
                resp=await c.post('/plugins/discord-todo/v1/retrospective/approve',json=request,headers=headers)
                self.assertEqual(resp.status,200)
                receipt=await resp.json()
                resp=await c.post('/plugins/discord-todo/v1/assess',json=dict(reports=[],labels=[],open_todos=[]),headers=headers)
                self.assertEqual(resp.status,200)
                self.assertEqual((await resp.json())['guidance_version'],receipt['policy_version'])
                self.assertIn('Runtime lesson',str(calls[-1]))
                self.assertNotIn('tools',calls[-1])

    async def test_stale_baseline_no_change_and_foreign_evidence(self):
        m=load_plugin()
        with tempfile.TemporaryDirectory() as d:
            s=m.RetrospectiveStore(Path(d)/'policy.db'); prompts=[]
            ctx=SimpleNamespace(inject_message=lambda text,**kw:prompts.append(text) or True)
            async def fetch(t): return SimpleNamespace(id=int(t),parent_id=1560943737688039474,guild=SimpleNamespace(id=555555555555555555))
            base=dict(user_id='333333333333333333',guild_id='555555555555555555',corrections=[dict(id='c1')],context='evidence')
            async def begin(run,thread):
                await m.retrospective_start(ctx,dict(base,run_id=run,thread_id=thread),fetch_thread=fetch,allowed_parents=['1560943737688039474'],store=s)
                return prompts[-1].split('DRAFT_CAPABILITY=')[1].split('\n')[0]
            cap=await begin('r1','111111111111111111')
            cap2=await begin('r2','222222222222222222')
            with self.assertRaises(ValueError): s.propose('0'*64,dict(rules=[],memory=[],summary='No change'))
            with self.assertRaises(ValueError): s.propose(cap,dict(rules=[dict(text='foreign',evidence_ids=['other'])],memory=[],summary='bad'))
            nochange=s.propose(cap,dict(rules=[],memory=[],summary='No reusable change: one-off override'))
            receipt=s.approve('r1',base['user_id'],nochange['proposal_hash'])
            self.assertEqual(receipt['policy_version'],'2')
            changed=s.propose(cap2,dict(rules=[dict(text='Use concrete evidence',evidence_ids=['c1'])],memory=[],summary='Clarification'))
            cap3=await begin('r3','444444444444444444')
            stale=s.propose(cap3,dict(rules=[],memory=[],summary='No change'))
            s.approve('r2',base['user_id'],changed['proposal_hash'])
            with self.assertRaises(ValueError): s.approve('r3',base['user_id'],stale['proposal_hash'])
            self.assertEqual(s.approve('r1',base['user_id'],nochange['proposal_hash']),receipt)
            with self.assertRaises(ValueError):
                await m.retrospective_start(ctx,dict(base,run_id='foreign',thread_id='666666666666666666',guild_id='777777777777777777'),fetch_thread=fetch,allowed_parents=['1560943737688039474'],store=s)

    async def test_durable_draft_requires_exact_owner_hash_approval(self):
        m = load_plugin()
        self.assertTrue(hasattr(m, 'RetrospectiveStore'), 'durable retrospective missing')
        with tempfile.TemporaryDirectory() as d:
            s = m.RetrospectiveStore(Path(d)/'policy.db')
            calls=[]
            ctx=SimpleNamespace(inject_message=lambda text, **kw: calls.append((text,kw)) or True)
            async def fetch(t):
                return SimpleNamespace(id=int(t),parent_id=1560943737688039474,guild=SimpleNamespace(id=555555555555555555))
            p=dict(run_id='run1',thread_id='111111111111111111',user_id='333333333333333333',guild_id='555555555555555555',corrections=[dict(id='c1',guild_id='555555555555555555')],context='Investigate correction')
            r=await m.retrospective_start(ctx,p,fetch_thread=fetch,allowed_parents=['1560943737688039474'],store=s)
            self.assertEqual(r['status'],'accepted')
            capability=calls[0][0].split('DRAFT_CAPABILITY=')[1].split('\n')[0]
            proposal=dict(rules=[dict(text='Prefer concrete report evidence.',evidence_ids=['c1'])],memory=[],summary='Clarify evidence use')
            draft=s.propose(capability,proposal)
            self.assertEqual(s.active()['version'],'2')
            with self.assertRaises(ValueError): s.approve('run1','444444444444444444',draft['proposal_hash'])
            with self.assertRaises(ValueError): s.approve('run1',p['user_id'],'0'*64)
            with self.assertRaises(ValueError): s.prepare_approval('run1','wrong',draft['proposal_hash'])
            with self.assertRaises(ValueError): s.prepare_approval('run1',p['user_id'],'0'*64)
            replacement=s.propose(capability,dict(proposal,summary='B won before prepare'))
            with self.assertRaises(ValueError): s.prepare_approval('run1',p['user_id'],draft['proposal_hash'])
            self.assertEqual(s.status('run1',p['user_id'])['proposal_hash'],replacement['proposal_hash'])
            draft=s.propose(capability,proposal)
            frozen=s.prepare_approval('run1',p['user_id'],draft['proposal_hash'])
            self.assertEqual(frozen['state'],'approval_pending')
            self.assertEqual(s.active()['version'],'2')
            with self.assertRaises(ValueError): s.propose(capability,dict(proposal,summary='raced replacement'))
            self.assertEqual(s.prepare_approval('run1',p['user_id'],draft['proposal_hash']),frozen)
            receipt=s.approve('run1',p['user_id'],draft['proposal_hash'])
            reopened=m.RetrospectiveStore(Path(d)/'policy.db')
            self.assertEqual(reopened.status('run1',p['user_id'])['policy_version'],receipt['policy_version'])
            self.assertEqual(reopened.approve('run1',p['user_id'],draft['proposal_hash']),receipt)
            self.assertIn('Prefer concrete',str(reopened.active()))
            await m.retrospective_start(ctx,p,fetch_thread=fetch,allowed_parents=['1560943737688039474'],store=reopened)
            self.assertEqual(len(calls),1)
