"""Smoke test: drives the server over MCP stdio against a throwaway Moodle that has local_fullmcp.

Use a dev site only: it creates courses. Run with the project's venv:

    FULLMCP_TEST_URL=http://localhost:8080 FULLMCP_TEST_TOKEN=... python tests/smoke_e2e.py
"""
import asyncio, json, os, sys, tempfile, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

URL = os.environ['FULLMCP_TEST_URL']
TOKEN = os.environ['FULLMCP_TEST_TOKEN']
FILES = tempfile.mkdtemp(prefix='fullmcp-smoke-')
RUN = str(int(time.time()))[-6:]
results = []

def parse(r):
    txt = ''.join(c.text for c in r.content if getattr(c, 'text', None))
    try: return json.loads(txt) if txt else None
    except ValueError: return txt

async def main():
    params = StdioServerParameters(command=sys.executable, args=['-c', 'from fullmcp_server.server import main; main()'], env={
        'MOODLE_URL': URL, 'MOODLE_TOKEN': TOKEN, 'MOODLE_ALLOW_WRITE': 'true',
        'FULLMCP_FILES_DIR': FILES, 'PATH': os.environ['PATH']})
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            async def call(_n, **kw):
                try:
                    r = await s.call_tool(_n, kw)
                except Exception as e:
                    return ('EXC', str(e)[:200])
                return ('ERR', parse(r)) if r.isError else ('ok', parse(r))
            async def step(_label, _n, _ok=True, **kw):
                st, val = await call(_n, **kw)
                good = (st == 'ok') == _ok
                results.append(good)
                short = json.dumps(val)[:150] if val is not None else 'null'
                print(('PASS' if good else 'FAIL'), _label, '->', st, short)
                return val if st == 'ok' else None

            tools = [t.name for t in (await s.list_tools()).tools]
            print('tools:', len(tools))
            gone = ['delete_courses','download_file','create_user','delete_users','assign_role','enrol_user','get_grades','get_user_grades','send_message','grade_assignment','get_enrolled_users','get_my_courses']
            leaked = [t for t in gone if t in tools]
            results.append(not leaked and len(tools) == 37); print('PASS' if not leaked and len(tools)==37 else 'FAIL', 'tool list: 37, none of the removed ones', leaked)

            info = await step('server_info', 'server_info')
            print('   plugin_installed =', info and info.get('plugin_installed'))
            await step('verify_connection', 'verify_connection')
            await step('list_courses', 'list_courses')
            c = await step('create_course', 'create_course', fullname='E2E '+RUN, shortname='e2e'+RUN, categoryid=1)
            cid = (c[0]['id'] if isinstance(c, list) else c['id'])
            sec = await step('create_section', 'create_section', courseid=cid, name='Week 1', summary='<p>s</p>')
            n = sec['sectionnum']
            pg = await step('create_page', 'create_page', courseid=cid, name='Page', content='<p>Hello</p>', section=n)
            await step('create_label', 'create_label', courseid=cid, content='<b>hi</b>', section=n)
            await step('create_url', 'create_url', courseid=cid, name='Link', externalurl='https://example.org', section=n)
            await step('create_book', 'create_book', courseid=cid, name='Book', chaptertitle='Ch1', chaptercontent='<p>x</p>', section=n)
            await step('create_forum', 'create_forum', courseid=cid, name='Forum', section=n)
            await step('create_choice', 'create_choice', courseid=cid, name='Poll', question='Q?', options=['a','b'], section=n)
            await step('create_assignment', 'create_assignment', courseid=cid, name='Assign', section=n)
            q = await step('create_quiz', 'create_quiz', courseid=cid, name='Quiz', section=n, grade=10.0)
            qc = q['cmid']
            await step('add_quiz_question', 'add_quiz_question', quizcmid=qc, name='MC', questiontext='Pick', answers=[{'text':'A','fraction':1.0},{'text':'B','fraction':0.0}])
            await step('add_truefalse', 'add_truefalse_question', quizcmid=qc, name='TF', questiontext='True?', correctanswer=1)
            await step('add_shortanswer', 'add_shortanswer_question', quizcmid=qc, name='SA', questiontext='Cap?', answers=['Paris'])
            await step('add_essay', 'add_essay_question', quizcmid=qc, name='ES', questiontext='Discuss')
            await step('add_numerical', 'add_numerical_question', quizcmid=qc, name='NU', questiontext='Pi?', answers=[{'answer':3.14,'tolerance':0.01,'fraction':1.0}])
            await step('add_matching', 'add_matching_question', quizcmid=qc, name='MA', questiontext='Match', pairs=[{'question':'a','answer':'b'},{'question':'c','answer':'d'}])
            await step('update_activity', 'update_activity', cmid=pg['cmid'], name='Page renamed', visible=1)
            await step('update_page', 'update_page', cmid=pg['cmid'], content='<p>Changed</p>')
            await step('update_section', 'update_section', courseid=cid, sectionnum=sec['sectionnum'], name='Week 1b', visible=1)
            sec2 = await step('create_section 2', 'create_section', courseid=cid, name='Week 2')
            await step('move_activity', 'move_activity', cmid=pg['cmid'], section=sec2['sectionnum'])
            content = await step('get_course_content', 'get_course_content', courseid=cid)
            mods = sum(len(x['modules']) for x in content) if isinstance(content, list) else '?'
            print('   modules in course:', mods)
            await step('list_quizzes', 'list_quizzes', courseids=[cid])
            await step('delete_activity', 'delete_activity', cmid=pg['cmid'])
            await step('delete_section', 'delete_section', courseid=cid, sectionnum=sec2['sectionnum'])
            bk = await step('backup_course', 'backup_course', courseid=cid, save_to='e2e.mbz')
            ok_file = os.path.getsize(os.path.join(FILES, 'e2e.mbz')) > 1000
            results.append(ok_file); print('PASS' if ok_file else 'FAIL', 'backup file written in files dir', bk and bk.get('size'))
            await step('restore_course', 'restore_course', filepath='e2e.mbz', categoryid=1, fullname='E2E restored '+RUN, shortname='e2er'+RUN)
            await step('backup path escape blocked', 'backup_course', _ok=False, courseid=cid, save_to='../evil.mbz')
            await step('restore path escape blocked', 'restore_course', _ok=False, filepath='/etc/passwd', shortname='x'+RUN)
            await step('build_course', 'build_course', outline={'fullname':'Built '+RUN,'shortname':'b'+RUN,'categoryid':1,'sections':[
                {'name':'S1','activities':[{'type':'page','name':'P','content':'<p>x</p>'},{'type':'quiz','name':'Q','questions':[{'qtype':'truefalse','name':'T','text':'t','correct':1}]}]},
                {'name':'S2','activities':[{'type':'forum','name':'F'}]}]})
            await step('removed tool is unknown', 'delete_courses', _ok=False, courseids=[cid])
    print(f'\n{sum(results)}/{len(results)} checks passed')
asyncio.run(main())
sys.exit(0 if all(results) else 1)
