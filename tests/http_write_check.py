"""WRITES to the live site: make a hidden test course with a section, page, label, url and quiz,
then remove the activities and the section. The server has no delete-course tool by design, so the
empty course "MCP write test ... delete me" stays; delete it in Moodle.

    FULLMCP_BEARER_TOKEN=... python tests/http_write_check.py [https://lmsmcp.tsopany.com/mcp]
"""
import asyncio
import json
import os
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = sys.argv[1] if len(sys.argv) > 1 else 'https://lmsmcp.tsopany.com/mcp'
TOKEN = os.environ['FULLMCP_BEARER_TOKEN']
RUN = str(int(time.time()))


async def main():
    ok = []
    async with streamablehttp_client(URL, headers={'Authorization': f'Bearer {TOKEN}'}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()

            async def step(_label, _tool, **args):
                res = await s.call_tool(_tool, args)
                text = res.content[0].text if res.content else ''
                good = not res.isError
                ok.append(good)
                print('PASS' if good else 'FAIL', _label, '' if good else text[:200])
                return json.loads(text) if good and text else None

            c = await step('create_course', 'create_course', fullname=f'MCP write test {RUN} - delete me',
                           shortname=f'mcpwt{RUN}', categoryid=1, visible=0)
            cid = (c[0] if isinstance(c, list) else c)['id'] if c else None
            if cid is None:
                return 1
            sec = await step('create_section', 'create_section', courseid=cid, name='Week 1')
            n = sec['sectionnum']
            page = await step('create_page', 'create_page', courseid=cid, name='Page', content='<p>Hello</p>', section=n)
            label = await step('create_label', 'create_label', courseid=cid, content='<b>hi</b>', section=n)
            url = await step('create_url', 'create_url', courseid=cid, name='Link', externalurl='https://example.org', section=n)
            quiz = await step('create_quiz', 'create_quiz', courseid=cid, name='Quiz', section=n, grade=10.0)
            await step('add_truefalse', 'add_truefalse_question', quizcmid=quiz['cmid'], name='TF',
                       questiontext='True?', correctanswer=1)
            await step('update_page', 'update_page', cmid=page['cmid'], content='<p>Changed</p>')
            for name, act in (('page', page), ('label', label), ('url', url), ('quiz', quiz)):
                await step(f'delete_{name}', 'delete_activity', cmid=act['cmid'])
            await step('delete_section', 'delete_section', courseid=cid, sectionnum=n)
    print(f'{sum(ok)}/{len(ok)} passed; course id {cid} ("MCP write test {RUN} - delete me") left in Moodle, delete it there')
    return 0 if all(ok) else 1

sys.exit(asyncio.run(main()))
