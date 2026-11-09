"""Check a deployed server over streamable HTTP: tool count and one real read call.

    FULLMCP_BEARER_TOKEN=... python tests/http_check.py [https://lmsmcp.tsopany.com/mcp]
"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = sys.argv[1] if len(sys.argv) > 1 else 'https://lmsmcp.tsopany.com/mcp'
TOKEN = os.environ['FULLMCP_BEARER_TOKEN']


async def main():
    async with streamablehttp_client(URL, headers={'Authorization': f'Bearer {TOKEN}'}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print('tools:', len(tools))
            result = await session.call_tool('list_courses', {})
            print('list_courses error:', result.isError)
            print(result.content[0].text[:300] if result.content else '(empty)')
            return 1 if result.isError else 0

sys.exit(asyncio.run(main()))
