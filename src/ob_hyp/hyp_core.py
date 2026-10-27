"""Portable Hyp lexical core, extracted from hq.py; see SOURCE.md."""
import json
import re
import pathlib
_jieba = None
_SPACED_ALIASES = None
_PROTECT_RE = None
DICT_SOURCES = ()
ALIASES = pathlib.Path('__unused_aliases__')
TOKEN_RE = re.compile(r"[一-鿿]|[A-Za-z0-9_][A-Za-z0-9_\-\.]*")
ASCII_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-\.]*")
STOP_DF_RATIO = 0.08
ALIAS_WEIGHT = 0.5
NEG_WORDS = {"不", "没", "没有", "别", "不要", "未", "不是", "无", "非", "不再", "不能", "不会", "不想", "从不", "从没"}
UNCERTAIN_WORDS = {"可能", "也许", "大概", "好像", "似乎", "不确定", "待核", "待", "或许", "估计"}

def register_source_policy(con):
    con.create_function('word_layer_eligible', 4, lambda source, tier, text, ref: source == 'document' and tier == 'raw', deterministic=True)
def source_dict_entries() -> list[tuple[str, str, str | None, str | None]]:
    missing = [str(p) for p in DICT_SOURCES if not p.exists()]
    if missing:
        raise FileNotFoundError('词典碎片缺失：' + ', '.join(missing))
    out = []
    for path in DICT_SOURCES:
        for raw in path.read_text(encoding='utf-8').splitlines():
            body = raw.split('#', 1)[0].strip()
            if not body:
                continue
            parts = body.split()
            word = parts[0]
            freq = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
            tag = parts[2] if len(parts) > 2 else None
            out.append((raw, word, freq, tag))
    return out

def merged_dict_entries() -> list[tuple[str, str, str | None, str | None]]:
    merged = {}
    for raw, word, freq, tag in source_dict_entries():
        key = word
        if key in merged:
            del merged[key]
        merged[key] = (raw, word, freq, tag)
    return list(merged.values())

def tokenizer():
    global _jieba
    if _jieba is None:
        import jieba
        jieba.setLogLevel(60)
        for _raw, w, freq, tag in source_dict_entries():
            jieba.add_word(w, int(freq) if freq else None, tag)
        _jieba = jieba
    return _jieba

def _spaced_aliases() -> list[tuple[str, str]]:
    global _SPACED_ALIASES
    if _SPACED_ALIASES is None:
        pairs = []
        try:
            raw = json.loads(ALIASES.read_text(encoding='utf-8')) if ALIASES.exists() else {}
            for canon, als in raw.items():
                for a in als:
                    if any((ch.isspace() for ch in a)):
                        pairs.append((a, canon))
        except Exception:
            pairs = []
        _SPACED_ALIASES = sorted(pairs, key=lambda p: -len(p[0]))
    return _SPACED_ALIASES

def _protect_re() -> re.Pattern:
    global _PROTECT_RE
    if _PROTECT_RE is None:
        mixed = set()
        cand = list(dict_words().keys())
        try:
            raw = json.loads(ALIASES.read_text(encoding='utf-8')) if ALIASES.exists() else {}
            for k, vs in raw.items():
                cand.append(k)
                cand.extend(vs)
        except Exception:
            pass
        for w in cand:
            if re.search('[一-鿿]', w) and re.search('[A-Za-z0-9]', w) and (not any((ch.isspace() for ch in w))):
                mixed.add(w)
        alts = sorted(mixed, key=len, reverse=True)
        pat = '|'.join((re.escape(w) for w in alts))
        _PROTECT_RE = re.compile((f'(?:{pat})|' if pat else '') + ASCII_WORD_RE.pattern, re.IGNORECASE)
    return _PROTECT_RE

def words_of(text: str) -> list[str]:
    for a, canon in _spaced_aliases():
        if a in text:
            text = text.replace(a, canon)
    out = []
    jb = tokenizer()
    pos = 0
    for m in _protect_re().finditer(text):
        gap = text[pos:m.start()]
        if gap:
            for w in jb.lcut(gap):
                w = w.strip()
                if w and TOKEN_RE.search(w):
                    out.append(w.lower())
        tok = m.group(0).rstrip('.-_')
        if tok and TOKEN_RE.search(tok):
            out.append(tok.lower())
        pos = m.end()
    gap = text[pos:]
    if gap:
        for w in jb.lcut(gap):
            w = w.strip()
            if w and TOKEN_RE.search(w):
                out.append(w.lower())
    return out

def load_aliases() -> dict:
    if not ALIASES.exists():
        return {}
    raw = json.loads(ALIASES.read_text(encoding='utf-8'))
    m: dict[str, set] = {}
    for k, vs in raw.items():
        grp = {k.lower(), *[v.lower() for v in vs]}
        for x in grp:
            m.setdefault(x, set()).update(grp - {x})
    return m

def canonical_map() -> dict:
    m = {}
    dw = dict_words()
    if ALIASES.exists():
        raw = json.loads(ALIASES.read_text(encoding='utf-8'))
        for k, vs in raw.items():
            if dw.get(k.lower()) not in {'nr', 'ns', 'nz'}:
                continue
            for v in vs:
                m[v.lower()] = k.lower()
    return m

def build_node_layer(con, *, commit=True):
    cmap = canonical_map()
    dw = dict_words()
    register_source_policy(con)
    allowed = {r[0] for r in con.execute('select span_id from spans where word_layer_eligible(source,tier,text,ref)')}
    spans = {sid: (actor, ts, text.lower()) for sid, actor, ts, text in con.execute('select span_id, actor, ts, text from spans') if sid in allowed}
    agg: dict[tuple, dict] = {}
    for w, sid, c in con.execute('select word, span_id, tf from edges'):
        if sid not in allowed:
            continue
        canon = cmap.get(w, w)
        key = (canon, sid)
        e = agg.setdefault(key, {'tf': 0, 'surface': set()})
        e['tf'] += c
        e['surface'].add(w)
    rows = []
    node_df: dict[str, int] = {}
    node_tf: dict[str, int] = {}
    for (canon, sid), e in agg.items():
        actor, ts, low = spans[sid]
        pol = 'pos'
        for s in e['surface']:
            i = low.find(s)
            if i >= 0:
                win = low[max(0, i - 6):i]
                if any((n in win for n in NEG_WORDS)):
                    pol = 'neg'
                    break
                if any((u in win for u in UNCERTAIN_WORDS)):
                    pol = 'uncertain'
        rows.append((canon, sid, e['tf'], actor, ts, pol, 'mech:jieba+dict+cmap'))
        node_df[canon] = node_df.get(canon, 0) + 1
        node_tf[canon] = node_tf.get(canon, 0) + e['tf']
    con.executemany('insert into node_edges values(?,?,?,?,?,?,?)', rows)
    inv: dict[str, list] = {}
    for a, cnon in cmap.items():
        inv.setdefault(cnon, []).append(a)
    con.executemany('insert into nodes values(?,?,?,?,?)', [(cn, ','.join(sorted(inv.get(cn, []))), 'dict:' + dw[cn] if cn in dw else 'topic', node_df[cn], node_tf[cn]) for cn in node_df])
    if commit:
        con.commit()

def fts_query_string(terms: list[str]) -> str:
    safe = [t.replace('"', '""') for t in terms if t]
    return ' OR '.join((f'"{t}"' for t in safe))

def _df(con) -> tuple[dict, int]:
    n = con.execute('select count(*) from spans').fetchone()[0]
    return ({w: d for w, d in con.execute('select word, df from words')}, n)

def rare_terms(con, terms: list[str]) -> tuple[list[str], list[str]]:
    df, n = _df(con)
    keep = [t for t in terms if df.get(t, 0) / max(n, 1) <= STOP_DF_RATIO]
    drop = [t for t in terms if t not in keep]
    if not keep and terms:
        keep = sorted(terms, key=lambda t: df.get(t, 10 ** 9))[:3]
        drop = [t for t in terms if t not in keep]
    return (keep, drop)

def _bm25(con, table: str, terms: list[str], k: int) -> dict:
    if not terms:
        return {}
    qs = fts_query_string(terms)
    return {sid: -s for sid, s in con.execute(f'select rowid, bm25({table}) as s from {table} where {table} match ? order by s limit ?', (qs, k)).fetchall()}

def dict_words() -> dict:
    return {word.lower(): tag or 'n' for _raw, word, _freq, tag in merged_dict_entries()}
