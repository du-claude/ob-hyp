"""Integration checks with invented text in a disposable directory."""
import asyncio
import json
import pathlib
import tempfile
from ob_hyp import hyp, runtime


async def check(root):
    config = root / 'config.toml'
    config.write_text('data_dir = "./data"\nactor = "test-author"\naudiences = ["test-author"]\n[hybrid_search]\nenabled = true\nstrong_gate = true\n')
    cfg = runtime.load(config)
    bid = await runtime.write(cfg, '虚构：天文台的望远镜用于观察木星。', '虚构计划')
    assert any(r['id'] == bid for r in await runtime.search(cfg, '望远镜'))
    mgr = runtime.manager(cfg)
    await mgr.create('虚构：望远镜属于另一作者。', actor='other-author', name='other')
    raw = await mgr.create('虚构：望远镜的待消化稿。', actor='test-author', digestion_state='raw')
    found = await runtime.search(cfg, '望远镜', 50)
    assert all(r['metadata']['actor'] == 'test-author' and r['id'] != raw for r in found)
    source = root / 'documents.jsonl'
    record = dict(ref='test:one', text='虚构：望远镜观察木星。')
    source.write_text(json.dumps(record, ensure_ascii=False)+'\n')
    assert hyp.ingest(cfg,source) == {'imported':1,'total':1}
    assert hyp.ingest(cfg,source) == {'imported':0,'total':1}
    assert hyp.query(cfg,'望远镜')[0]['text'] == record['text']
    source.write_text(json.dumps(dict(ref='test:two',text='虚构第二条'))+'\n'+json.dumps({**record,'text':'虚构冲突'})+'\n')
    try:
        hyp.ingest(cfg,source)
    except ValueError:
        pass
    else:
        raise AssertionError('conflicting original was accepted')
    assert hyp.query(cfg,'望远镜')[0]['text'] == record['text']
    import sqlite3
    with sqlite3.connect(cfg['data_dir'] / 'hyp.db') as con:
        assert con.execute('select count(*) from spans').fetchone()[0] == 1
        assert con.execute('pragma integrity_check').fetchone()[0] == 'ok'
    source.write_text(json.dumps({**record,'actor':'other-author'})+'\n')
    try:
        hyp.ingest(cfg,source)
    except ValueError:
        pass
    else:
        raise AssertionError('foreign author was accepted')
    print('PASS: OB write/search, foreign-author and quarantine filter; Hyp original text, idempotency, conflict rollback, integrity, author rejection')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='verify-', dir='.') as directory:
        asyncio.run(check(pathlib.Path(directory).resolve()))
