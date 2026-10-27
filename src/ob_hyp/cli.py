import argparse
import asyncio
import json
import os
from . import hyp, runtime


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description='OB + Hyp local candidate')
    p.add_argument('--config', default='config.toml')
    sub = p.add_subparsers(dest='command', required=True)
    w = sub.add_parser('ob-write'); w.add_argument('text'); w.add_argument('--name')
    for name in ('ob-search', 'hyp-query'):
        q = sub.add_parser(name); q.add_argument('query'); q.add_argument('--limit', type=int, default=5)
    i = sub.add_parser('hyp-import'); i.add_argument('file')
    sub.add_parser('mcp')
    a = p.parse_args(); cfg = runtime.load(a.config)
    if a.command == 'ob-write':
        result = {'bucket_id': asyncio.run(runtime.write(cfg,a.text,a.name))}
    elif a.command == 'ob-search':
        result = asyncio.run(runtime.search(cfg,a.query,a.limit))
    elif a.command == 'hyp-import':
        result = hyp.ingest(cfg,a.file)
    elif a.command == 'hyp-query':
        result = hyp.query(cfg,a.query,a.limit)
    else:
        from .mcp_server import serve
        serve(cfg)
        return
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
