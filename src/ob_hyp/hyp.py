"""Explicit JSONL import, immutable originals and disposable lexical indexes."""
import collections
import hashlib
import json
import pathlib
import sqlite3
from . import hyp_core as core

SCHEMA = '''
CREATE TABLE IF NOT EXISTS spans(span_id INTEGER PRIMARY KEY, source TEXT, tier TEXT, actor TEXT, ts TEXT, ref TEXT UNIQUE NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_tri USING fts5(text,content='spans',content_rowid='span_id',tokenize='trigram');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_word USING fts5(words,tokenize='unicode61 remove_diacritics 0');
CREATE TABLE IF NOT EXISTS words(word TEXT PRIMARY KEY,df INTEGER,tf INTEGER);
CREATE TABLE IF NOT EXISTS edges(word TEXT,span_id INTEGER,tf INTEGER,PRIMARY KEY(word,span_id));
CREATE TABLE IF NOT EXISTS nodes(canonical TEXT PRIMARY KEY,aliases TEXT,type TEXT,df INTEGER,tf INTEGER);
CREATE TABLE IF NOT EXISTS node_edges(canonical TEXT,span_id INTEGER,tf INTEGER,actor TEXT,ts TEXT,polarity TEXT,extracted_by TEXT,PRIMARY KEY(canonical,span_id));
CREATE TABLE IF NOT EXISTS candidate_meta(name TEXT PRIMARY KEY,value TEXT);
'''


def setup(cfg):
    core.DICT_SOURCES = tuple(cfg['dictionary_files'])
    core.ALIASES = cfg['aliases_file'] or cfg['data_dir'] / 'aliases.json'
    # A fresh tokenizer prevents dictionaries leaking between stores in one process.
    core._jieba = None
    core._SPACED_ALIASES = None
    core._PROTECT_RE = None
    import jieba
    jb = jieba.Tokenizer()
    jb.setLogLevel(60) if hasattr(jb, 'setLogLevel') else jieba.setLogLevel(60)
    jb.tmp_dir = str(cfg['data_dir'])
    for _, word, freq, tag in core.source_dict_entries():
        jb.add_word(word, int(freq) if freq else None, tag)
    core._jieba = jb


def fingerprint(cfg):
    values = [str(p) + ':' + hashlib.sha256(p.read_bytes()).hexdigest() for p in cfg['dictionary_files']]
    if core.ALIASES.exists():
        values.append(hashlib.sha256(core.ALIASES.read_bytes()).hexdigest())
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def ingest(cfg, filename):
    root = cfg['data_dir']; root.mkdir(parents=True, exist_ok=True)
    setup(cfg)
    records = []
    for i, line in enumerate(pathlib.Path(filename).read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        if not isinstance(r, dict) or set(r) - {'ref', 'text', 'ts', 'actor'}:
            raise ValueError(f'第 {i} 行字段不合法')
        if not all(isinstance(r.get(x), str) and r[x].strip() for x in ('ref', 'text')):
            raise ValueError(f'第 {i} 行必须含非空 ref/text')
        if r.get('actor', cfg['actor']) != cfg['actor']:
            raise ValueError(f'第 {i} 行 actor 与当前本地使用者不符')
        if not isinstance(r.get('ts', ''), str):
            raise ValueError(f'第 {i} 行 ts 必须是字符串')
        records.append(('document', 'raw', cfg['actor'], r.get('ts', ''), r['ref'], r['text']))
    con = sqlite3.connect(root / 'hyp.db')
    try:
        con.executescript(SCHEMA)
        con.execute('BEGIN IMMEDIATE')
        con.execute("INSERT OR IGNORE INTO candidate_meta VALUES('schema','1')")
        if con.execute("SELECT value FROM candidate_meta WHERE name='schema'").fetchone()[0] != '1':
            raise ValueError('未知库版本')
        added = 0
        for row in records:
            old = con.execute('SELECT source,tier,actor,ts,ref,text FROM spans WHERE ref=?', (row[4],)).fetchone()
            if old is not None:
                if tuple(old) != row:
                    raise ValueError('ref 已存在但原文或元数据不同；拒绝改写')
                continue
            con.execute('INSERT INTO spans(source,tier,actor,ts,ref,text) VALUES(?,?,?,?,?,?)', row)
            added += 1
        # Derived tables are rebuilt within the same transaction. Original rows are never replaced.
        con.execute("INSERT INTO fts_tri(fts_tri) VALUES('delete-all')")
        for table in ('fts_word', 'words', 'edges', 'nodes', 'node_edges'):
            con.execute(f'DELETE FROM {table}')
        df = collections.Counter(); tf = collections.Counter()
        for sid, text in con.execute('SELECT span_id,text FROM spans').fetchall():
            counts = collections.Counter(core.words_of(text))
            con.execute('INSERT INTO fts_tri(rowid,text) VALUES(?,?)', (sid, text))
            con.execute('INSERT INTO fts_word(rowid,words) VALUES(?,?)', (sid, ' '.join(core.words_of(text))))
            con.executemany('INSERT INTO edges VALUES(?,?,?)', [(w,sid,c) for w,c in counts.items()])
            df.update(counts.keys()); tf.update(counts)
        con.executemany('INSERT INTO words VALUES(?,?,?)', [(w,df[w],tf[w]) for w in df])
        core.build_node_layer(con, commit=False)
        con.execute("INSERT OR REPLACE INTO candidate_meta VALUES('dictionary',?)", (fingerprint(cfg),))
        con.commit()
        return {'imported': added, 'total': con.execute('SELECT count(*) FROM spans').fetchone()[0]}
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def query(cfg, text, limit=5):
    if not text.strip() or not 1 <= limit <= 50:
        raise ValueError('query 非空，limit 必须为 1..50')
    setup(cfg)
    path = cfg['data_dir'] / 'hyp.db'
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if con.execute("SELECT value FROM candidate_meta WHERE name='dictionary'").fetchone()[0] != fingerprint(cfg):
            raise ValueError('词典或别名变化，请重新导入以重建派生索引')
        terms = core.words_of(text)
        scores = core._bm25(con, 'fts_word', terms, limit)
        out = []
        for sid, score in sorted(scores.items(), key=lambda x: -x[1]):
            row = con.execute('SELECT actor,ts,ref,text FROM spans WHERE span_id=?', (sid,)).fetchone()
            if row[0] != cfg['actor']:
                raise ValueError('库含其他 actor，拒绝返回；请使用独立数据目录')
            out.append(dict(span_id=sid, score=score, actor=row[0], ts=row[1], ref=row[2], text=row[3]))
        return out
    finally:
        con.close()
