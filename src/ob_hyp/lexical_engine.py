# Adapted from the running OB source; see SOURCE.md.
import logging
import math
import re
from collections import Counter
import jieba
logger = logging.getLogger('ombre.lexical')
_TOKEN_OK = re.compile('[\\w一-鿿\u3040-ヿ]', re.UNICODE)

def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in jieba.cut_for_search(text) if t.strip() and _TOKEN_OK.search(t)]

class LexicalEngine:

    def __init__(self, config: dict):
        hybrid = config.get('hybrid_search', {})
        self.k1 = float(hybrid.get('bm25_k1', 1.5))
        self.b = float(hybrid.get('bm25_b', 0.75))
        fw = hybrid.get('field_weights', {}) or {}
        self.w_name = float(fw.get('name', 4.0))
        self.w_tags = float(fw.get('tags', 3.0))
        self.w_domain = float(fw.get('domain', 2.0))
        self.w_content = float(fw.get('content', 1.0))
        self._docs: dict[str, dict] = {}
        self._df: Counter = Counter()
        self._total_len: float = 0.0

    @staticmethod
    def _signature(bucket: dict) -> int:
        meta = bucket.get('metadata', {})
        return hash((meta.get('name', ''), tuple(meta.get('domain', []) or []), tuple(meta.get('tags', []) or []), bucket.get('content', '')))

    def _weighted_tf(self, bucket: dict) -> Counter:
        meta = bucket.get('metadata', {})
        tf: Counter = Counter()
        for text, weight in ((meta.get('name', ''), self.w_name), (' '.join(meta.get('tags', []) or []), self.w_tags), (' '.join(meta.get('domain', []) or []), self.w_domain), (bucket.get('content', ''), self.w_content)):
            if not text or weight <= 0:
                continue
            for tok in _tokenize(text):
                tf[tok] += weight
        return tf

    def _remove_doc(self, bucket_id: str) -> None:
        doc = self._docs.pop(bucket_id, None)
        if not doc:
            return
        for term in doc['tf']:
            self._df[term] -= 1
            if self._df[term] <= 0:
                del self._df[term]
        self._total_len -= doc['len']

    def _add_doc(self, bucket_id: str, sig: int, tf: Counter) -> None:
        doc_len = float(sum(tf.values()))
        self._docs[bucket_id] = {'sig': sig, 'tf': tf, 'len': doc_len}
        for term in tf:
            self._df[term] += 1
        self._total_len += doc_len

    def ensure_index(self, buckets: list[dict]) -> None:
        seen: set[str] = set()
        changed = 0
        for b in buckets:
            bid = b.get('id')
            if not bid:
                continue
            seen.add(bid)
            sig = self._signature(b)
            old = self._docs.get(bid)
            if old is not None and old['sig'] == sig:
                continue
            if old is not None:
                self._remove_doc(bid)
            self._add_doc(bid, sig, self._weighted_tf(b))
            changed += 1
        for bid in list(self._docs.keys() - seen):
            self._remove_doc(bid)
            changed += 1
        if changed:
            logger.info(f'Lexical index refreshed: {changed} docs updated, {len(self._docs)} total')

    def search(self, query: str, top_k: int=100) -> list[tuple[str, float]]:
        q_terms = _tokenize(query)
        if not q_terms or not self._docs:
            return []
        n = len(self._docs)
        avgdl = self._total_len / n if n else 1.0
        scores: dict[str, float] = {}
        for term in set(q_terms):
            df = self._df.get(term)
            if not df:
                continue
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
            for bid, doc in self._docs.items():
                tf = doc['tf'].get(term)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * doc['len'] / avgdl)
                scores[bid] = scores.get(bid, 0.0) + idf * tf * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]
