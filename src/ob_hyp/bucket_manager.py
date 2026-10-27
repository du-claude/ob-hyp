# Adapted from the running OB source; see SOURCE.md.
import os
import math
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
import hashlib
import json
import frontmatter
from rapidfuzz import fuzz
import asyncio
from .utils import generate_bucket_id, sanitize_name, safe_path, now_iso, now_cst, parse_ts, effective_status, effective_surface, VALID_STATUS, VALID_SURFACE, normalize_audience, bucket_audience, VALID_AUDIENCE_PARTS, valid_audience_parts
logger = logging.getLogger('ombre_brain.bucket')

def atomic_write_text(file_path: str, text: str):
    import tempfile as _tempfile
    import stat as _stat
    dir_ = os.path.dirname(file_path) or '.'
    try:
        ownership_source = os.stat(file_path)
        target_mode = _stat.S_IMODE(ownership_source.st_mode)
    except FileNotFoundError:
        ownership_source = os.stat(dir_)
        target_mode = 384
    fd, tmp = _tempfile.mkstemp(dir=dir_, prefix='.ob-tmp-', suffix='.part')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, target_mode)
        os.chown(tmp, ownership_source.st_uid, ownership_source.st_gid)
        os.replace(tmp, file_path)
        try:
            dfd = os.open(dir_, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

class BucketManager:

    def __init__(self, config: dict, embedding_engine=None):
        self.base_dir = config['buckets_dir']
        self.private_author = config.get('private_author', 'human')
        self.permanent_dir = os.path.join(self.base_dir, 'permanent')
        self.dynamic_dir = os.path.join(self.base_dir, 'dynamic')
        self.archive_dir = os.path.join(self.base_dir, 'archive')
        self.feel_dir = os.path.join(self.base_dir, 'feel')
        self.meta_dir = os.path.join(self.base_dir, 'meta')
        os.makedirs(self.meta_dir, exist_ok=True)
        self.changelog_path = os.path.join(self.meta_dir, 'memory_changelog.jsonl')
        self.fuzzy_threshold = config.get('matching', {}).get('fuzzy_threshold', 50)
        self.max_results = config.get('matching', {}).get('max_results', 5)
        wikilink_cfg = config.get('wikilink', {})
        self.wikilink_enabled = wikilink_cfg.get('enabled', True)
        self.wikilink_use_tags = wikilink_cfg.get('use_tags', False)
        self.wikilink_use_domain = wikilink_cfg.get('use_domain', True)
        self.wikilink_use_auto_keywords = wikilink_cfg.get('use_auto_keywords', True)
        self.wikilink_auto_top_k = wikilink_cfg.get('auto_top_k', 8)
        self.wikilink_min_len = wikilink_cfg.get('min_keyword_len', 2)
        self.wikilink_exclude_keywords = set(wikilink_cfg.get('exclude_keywords', []))
        self.wikilink_stopwords = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一个', '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好', '自己', '这', '他', '她', '我们', '你们', '他们', '然后', '今天', '昨天', '明天', '一下', 'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'had', 'her', 'was', 'one', 'our', 'out', 'has', 'have', 'with', 'this', 'that', 'from', 'they', 'been', 'said', 'will', 'each'}
        self.wikilink_stopwords |= {w.lower() for w in self.wikilink_exclude_keywords}
        scoring = config.get('scoring_weights', {})
        self.w_topic = scoring.get('topic_relevance', 4.0)
        self.w_emotion = scoring.get('emotion_resonance', 2.0)
        self.w_time = scoring.get('time_proximity', 1.5)
        self.w_importance = scoring.get('importance', 1.0)
        self.content_weight = scoring.get('content_weight', 1.0)
        self.embedding_engine = embedding_engine
        self.always_max = int(os.environ.get('OMBRE_ALWAYS_MAX', '10') or '10')
        self._core_gate = asyncio.Lock()
        _enforce_raw = os.environ.get('OMBRE_AUDIENCE_ENFORCE')
        if _enforce_raw is None:
            logger.warning('OMBRE_AUDIENCE_ENFORCE 未设置，按 off 兼容启动；生产 P0 前请在 env 显式写 off')
            self.audience_enforce = False
        else:
            _v = _enforce_raw.strip().lower()
            if _v not in ('off', 'on'):
                raise RuntimeError(f'OMBRE_AUDIENCE_ENFORCE 只接受 off/on，收到 {_enforce_raw!r}；服务拒绝启动')
            self.audience_enforce = _v == 'on'
        logger.info(f"audience_enforce={('on' if self.audience_enforce else 'off')}")
        hybrid = config.get('hybrid_search', {})
        self.hybrid_enabled = bool(hybrid.get('enabled', False))
        self.rrf_k = int(hybrid.get('rrf_k', 60))
        self.channel_top = int(hybrid.get('channel_top', 100))
        self.fusion_top = int(hybrid.get('fusion_top', 100))
        self.strong_gate = bool(hybrid.get('strong_gate', False))
        self.bm25_strong_ratio = float(hybrid.get('bm25_strong_ratio', 0.5))
        self.fuzz_strong = float(hybrid.get('fuzz_strong', 0.5))
        self.vector_min_sim = float(hybrid.get('vector_min_sim', 0.35))
        self.vector_only_min_sim = float(hybrid.get('vector_only_min_sim', 0.5))
        self.fuzz_min = float(hybrid.get('fuzz_min', 0.35))
        self.lexical_engine = None
        if self.hybrid_enabled:
            try:
                from .lexical_engine import LexicalEngine
                self.lexical_engine = LexicalEngine(config)
            except Exception as e:
                logger.warning(f'LexicalEngine init failed, BM25 channel off / 词面引擎初始化失败: {e}')

    async def _audience_always_counts(self, exclude_id: str=None) -> dict:
        counts = {p: 0 for p in valid_audience_parts()}
        for type_dir in (self.permanent_dir, self.dynamic_dir, self.feel_dir):
            if not os.path.isdir(type_dir):
                continue
            for root, _dirs, files in os.walk(type_dir):
                for fn in files:
                    if not fn.endswith('.md'):
                        continue
                    fp = os.path.join(root, fn)
                    try:
                        meta = frontmatter.load(fp).metadata
                    except Exception as e:
                        raise ValueError(f'核心层计数扫描遇坏桶 {os.path.relpath(fp, self.base_dir)}（{type(e).__name__}: {e}）——fail-closed，拒绝本次核心写。请先修复或归档该文件。') from e
                    if exclude_id and meta.get('id') == exclude_id:
                        continue
                    if effective_surface(meta) != 'always':
                        continue
                    for p in bucket_audience(meta) or valid_audience_parts():
                        counts[p] += 1
        return counts

    def _gate_check_always(self, counts: dict, audience: list):
        for p in audience:
            if counts.get(p, 0) + 1 > self.always_max:
                raise ValueError(f'受众 {p} 的 always 名额已满（上限 {self.always_max}，现有 {counts.get(p, 0)}）。先用 trace(bucket_id, surface="rotation"/"time_fade") 降一条旧桶再钉。')

    async def create(self, content: str, tags: list[str]=None, importance: int=5, domain: list[str]=None, valence: float=0.5, arousal: float=0.3, bucket_type: str='dynamic', name: str=None, pinned: bool=False, protected: bool=False, source_type: str='self', trust_level: str='trusted', digestion_state: str='synthesized', actor: str='', manual_tags: list[str]=None, engine: str='', audience=None) -> str:
        norm_audience = normalize_audience(audience)
        will_always = bool(pinned or protected)
        if will_always and norm_audience is None:
            raise ValueError('pinned/protected 桶必须携带 audience（reader-a / reader-b / family）——audience 是主动浮现路由，缺失会跨声部注入。')
        if (pinned or protected) and bucket_type != 'permanent':
            bucket_type = 'permanent'
        bucket_id = generate_bucket_id()
        bucket_name = sanitize_name(name) if name else bucket_id
        if bucket_type == 'feel':
            domain = domain if domain is not None else []
        else:
            domain = domain or ['未分类']
        tags = tags or []
        linked_content = content
        if pinned or protected:
            importance = 10
        metadata = {'id': bucket_id, 'name': bucket_name, 'tags': tags, 'domain': domain, 'valence': max(0.0, min(1.0, valence)), 'arousal': max(0.0, min(1.0, arousal)), 'importance': max(1, min(10, importance)), 'type': bucket_type, 'created': now_iso(), 'last_active': now_iso(), 'content_modified': now_iso(), 'activation_count': 0, 'source_type': source_type, 'trust_level': trust_level, 'digestion_state': digestion_state, 'status': 'open', 'surface': 'always' if pinned or protected else 'hidden' if bucket_type == 'feel' else 'time_fade'}
        if norm_audience:
            metadata['audience'] = norm_audience
        if actor:
            metadata['actor'] = actor
        if engine and engine.strip():
            metadata['engine'] = engine.strip()
        if manual_tags:
            metadata['manual_tags'] = [t for t in manual_tags if t]
        if pinned:
            metadata['pinned'] = True
        if protected:
            metadata['protected'] = True
        post = frontmatter.Post(linked_content, **metadata)
        if bucket_type == 'permanent':
            type_dir = self.permanent_dir
        elif bucket_type == 'feel':
            type_dir = self.feel_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == 'feel':
            primary_domain = '沉淀物'
        else:
            primary_domain = sanitize_name(domain[0]) if domain else '未分类'
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        if bucket_name and bucket_name != bucket_id:
            filename = f'{bucket_name}_{bucket_id}.md'
        else:
            filename = f'{bucket_id}.md'
        file_path = safe_path(target_dir, filename)
        try:
            if will_always:
                async with self._core_gate:
                    counts = await self._audience_always_counts()
                    self._gate_check_always(counts, norm_audience)
                    atomic_write_text(file_path, frontmatter.dumps(post))
            else:
                atomic_write_text(file_path, frontmatter.dumps(post))
        except OSError as e:
            logger.error(f'Failed to write bucket file / 写入桶文件失败: {file_path}: {e}')
            raise
        logger.info(f'Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/' + (' [PINNED]' if pinned else '') + (' [PROTECTED]' if protected else ''))
        return bucket_id

    async def get(self, bucket_id: str) -> Optional[dict]:
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        return self._load_bucket(file_path)

    def _move_bucket(self, file_path: str, target_type_dir: str, domain: list[str]=None) -> str:
        primary_domain = sanitize_name(domain[0]) if domain else '未分类'
        target_dir = os.path.join(target_type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        filename = os.path.basename(file_path)
        new_path = safe_path(target_dir, filename)
        if os.path.normpath(file_path) != os.path.normpath(new_path):
            if os.path.exists(new_path):
                import time as _time
                _base, _ext = os.path.splitext(filename)
                new_path = safe_path(target_dir, f'{_base}-dup{int(_time.time())}{_ext}')
            os.rename(file_path, new_path)
            logger.info(f'Moved bucket / 移动记忆桶: {filename} → {target_dir}/')
        return new_path

    async def update(self, bucket_id: str, **kwargs) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f'Failed to load bucket for update / 加载桶失败: {file_path}: {e}')
            return False
        old_fm_hash = self._compute_hash(dict(post.metadata))
        old_body_hash = self._compute_hash(post.content)
        if 'anchor' in kwargs:
            raise ValueError('anchor 参数已退役：轮换请改 surface="rotation"，退出轮换用 surface="time_fade"。')
        if 'pinned' in kwargs and 'surface' not in kwargs:
            kwargs['surface'] = 'always' if kwargs['pinned'] else 'time_fade'
        preserve_timestamps = bool(kwargs.pop('preserve_timestamps', False))
        new_audience = None
        if 'audience' in kwargs:
            new_audience = normalize_audience(kwargs.pop('audience'))
        cur_meta = dict(post.metadata)
        sim = dict(cur_meta)
        if 'pinned' in kwargs:
            sim['pinned'] = bool(kwargs['pinned'])
        if 'protected' in kwargs:
            sim['protected'] = bool(kwargs['protected'])
        if 'surface' in kwargs and kwargs['surface'] in VALID_SURFACE:
            sim['surface'] = kwargs['surface']
        elif 'digested' in kwargs:
            sim['surface'] = 'hidden' if kwargs['digested'] else 'time_fade'
        elif 'resolved' in kwargs and 'status' not in kwargs:
            sim['surface'] = 'time_fade'
        prospective_surface = effective_surface(sim)
        effective_aud = new_audience if new_audience is not None else bucket_audience(cur_meta)
        touching_core = any((k in kwargs for k in ('pinned', 'protected', 'surface'))) or new_audience is not None
        if touching_core and prospective_surface in ('always', 'rotation') and (effective_aud is None):
            raise ValueError('surface=always/rotation 的桶必须有 audience（reader-a / reader-b / family）；请随本次修改一并传入 audience。')
        was_always = effective_surface(cur_meta) == 'always'
        needs_quota = prospective_surface == 'always' and (not was_always or (new_audience is not None and new_audience != bucket_audience(cur_meta)))
        is_pinned = post.get('pinned', False) or post.get('protected', False)
        if is_pinned:
            kwargs.pop('importance', None)
        if 'content' in kwargs:
            post.content = kwargs['content']
        if 'tags' in kwargs:
            post['tags'] = kwargs['tags']
        if 'importance' in kwargs:
            post['importance'] = max(1, min(10, int(kwargs['importance'])))
        if 'domain' in kwargs:
            post['domain'] = kwargs['domain']
        if 'valence' in kwargs:
            post['valence'] = max(0.0, min(1.0, float(kwargs['valence'])))
        if 'arousal' in kwargs:
            post['arousal'] = max(0.0, min(1.0, float(kwargs['arousal'])))
        if 'name' in kwargs:
            post['name'] = sanitize_name(kwargs['name'])
        if 'resolved' in kwargs:
            post['resolved'] = bool(kwargs['resolved'])
        if 'pinned' in kwargs:
            post['pinned'] = bool(kwargs['pinned'])
            if kwargs['pinned']:
                post['importance'] = 10
        if 'digested' in kwargs:
            post['digested'] = bool(kwargs['digested'])
        if 'model_valence' in kwargs:
            post['model_valence'] = max(0.0, min(1.0, float(kwargs['model_valence'])))
        if new_audience is not None:
            post['audience'] = new_audience
        if 'no_merge' in kwargs:
            post['no_merge'] = bool(kwargs['no_merge'])
        if 'source_type' in kwargs:
            post['source_type'] = kwargs['source_type']
        if 'trust_level' in kwargs:
            post['trust_level'] = kwargs['trust_level']
        if 'digestion_state' in kwargs:
            post['digestion_state'] = kwargs['digestion_state']
        old_status = post.get('status') if post.get('status') in VALID_STATUS else 'closed' if post.get('resolved') else 'open'
        if 'status' in kwargs and kwargs['status'] in VALID_STATUS:
            post['status'] = kwargs['status']
        if 'surface' in kwargs and kwargs['surface'] in VALID_SURFACE:
            post['surface'] = kwargs['surface']
        if 'resolved' in kwargs and 'status' not in kwargs:
            post['status'] = 'closed' if kwargs['resolved'] else 'open'
            if 'surface' not in kwargs:
                post['surface'] = 'time_fade'
        if 'digested' in kwargs and 'surface' not in kwargs:
            post['surface'] = 'hidden' if kwargs['digested'] else 'time_fade'
        new_status = post.get('status') if post.get('status') in VALID_STATUS else old_status
        if new_status in ('closed', 'superseded') and old_status not in ('closed', 'superseded'):
            post['closed_at'] = now_iso()
        elif new_status == 'open' and 'closed_at' in post.metadata:
            del post.metadata['closed_at']
        if post.get('status') in VALID_STATUS:
            post['resolved'] = post['status'] in ('closed', 'superseded')
        if post.get('surface') in VALID_SURFACE:
            post['digested'] = post['surface'] == 'hidden'
        if not preserve_timestamps:
            post['last_active'] = now_iso()
        if 'content' in kwargs:
            post['content_modified'] = now_iso()
        try:
            if needs_quota:
                async with self._core_gate:
                    counts = await self._audience_always_counts(exclude_id=bucket_id)
                    self._gate_check_always(counts, effective_aud)
                    atomic_write_text(file_path, frontmatter.dumps(post))
            else:
                atomic_write_text(file_path, frontmatter.dumps(post))
        except OSError as e:
            logger.error(f'Failed to write bucket update / 写入桶更新失败: {file_path}: {e}')
            return False
        final_path = file_path
        domain = post.get('domain', ['未分类'])
        _revive = 'resolved' in kwargs and (not kwargs['resolved']) or kwargs.get('status') == 'open'
        if _revive and post.get('type') == 'archived':
            post['type'] = 'dynamic'
            atomic_write_text(file_path, frontmatter.dumps(post))
            final_path = self._move_bucket(file_path, self.dynamic_dir, domain)
            logger.info(f'Unarchived bucket / 复活归档桶: {bucket_id} → dynamic/')
        elif kwargs.get('pinned') and post.get('type') != 'permanent':
            post['type'] = 'permanent'
            atomic_write_text(file_path, frontmatter.dumps(post))
            final_path = self._move_bucket(file_path, self.permanent_dir, domain)
        elif 'pinned' in kwargs and (not kwargs['pinned']) and (post.get('type') == 'permanent'):
            post['type'] = 'dynamic'
            atomic_write_text(file_path, frontmatter.dumps(post))
            final_path = self._move_bucket(file_path, self.dynamic_dir, domain)
            logger.info(f'Unpinned -> dynamic: {bucket_id}')
        new_fm_hash = self._compute_hash(dict(post.metadata))
        new_body_hash = self._compute_hash(post.content)
        fields_changed = [k for k in kwargs if k not in ('content', 'reason')]
        if new_audience is not None:
            fields_changed.append('audience')
        if 'content' in kwargs:
            fields_changed.append('content')
        self._append_changelog(bucket_id=bucket_id, bucket_path=os.path.relpath(final_path or file_path, self.base_dir), action='trace', fields_changed=fields_changed, old_fm_hash=old_fm_hash, old_body_hash=old_body_hash, new_fm_hash=new_fm_hash, new_body_hash=new_body_hash, reason=kwargs.get('reason'))
        logger.info(f'Updated bucket / 更新记忆桶: {bucket_id}')
        return True

    async def add_comment(self, bucket_id: str, text: str, author: str='Claude') -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f'Failed to load bucket for ring / 年轮加载失败: {file_path}: {e}')
            return False
        comments = post.get('comments', [])
        if not isinstance(comments, list):
            comments = []
        comments.append({'time': now_iso(), 'author': author, 'text': text})
        post['comments'] = comments
        try:
            atomic_write_text(file_path, frontmatter.dumps(post))
        except OSError as e:
            logger.error(f'Failed to write ring / 年轮写入失败: {file_path}: {e}')
            return False
        logger.info(f'Ring comment added / 年轮+1: {bucket_id} ({len(comments)} rings)')
        return True

    async def delete(self, bucket_id: str) -> bool:
        return await self.archive(bucket_id)

    async def purge(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        try:
            os.remove(file_path)
        except OSError as e:
            logger.error(f'Failed to purge bucket file / 物理删除桶文件失败: {file_path}: {e}')
            return False
        logger.warning(f'PURGED bucket (irreversible) / 已物理删除记忆桶（不可恢复）: {bucket_id}')
        return True

    async def touch(self, bucket_id: str) -> None:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return
        try:
            post = frontmatter.load(file_path)
            post['last_active'] = now_iso()
            post['activation_count'] = post.get('activation_count', 0) + 1
            atomic_write_text(file_path, frontmatter.dumps(post))
            current_time = parse_ts(post.get('created', post.get('last_active', '')))
            await self._time_ripple(bucket_id, current_time)
        except Exception as e:
            logger.warning(f'Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}')

    async def _time_ripple(self, source_id: str, reference_time: datetime, hours: float=48.0) -> None:
        try:
            all_buckets = await self.list_all(include_archive=False)
        except Exception:
            return
        rippled = 0
        max_ripple = 5
        for bucket in all_buckets:
            if rippled >= max_ripple:
                break
            if bucket['id'] == source_id:
                continue
            meta = bucket.get('metadata', {})
            if meta.get('pinned') or meta.get('protected') or meta.get('type') in ('permanent', 'feel'):
                continue
            created_str = meta.get('created', meta.get('last_active', ''))
            try:
                created = parse_ts(created_str)
                delta_hours = abs((reference_time - created).total_seconds()) / 3600
            except (ValueError, TypeError):
                continue
            if delta_hours <= hours:
                file_path = self._find_bucket_file(bucket['id'])
                if not file_path:
                    continue
                try:
                    post = frontmatter.load(file_path)
                    current_count = post.get('activation_count', 1)
                    post['activation_count'] = round(current_count + 0.3, 1)
                    atomic_write_text(file_path, frontmatter.dumps(post))
                    rippled += 1
                except Exception:
                    continue

    async def search(self, query: str, limit: int=None, domain_filter: list[str]=None, query_valence: float=None, query_arousal: float=None, bucket_filter=None, strict_domain: bool=False) -> list[dict]:
        if not query or not query.strip():
            return []
        limit = limit or self.max_results
        all_buckets = await self.list_all(include_archive=False)
        if not all_buckets:
            return []
        all_buckets = [b for b in all_buckets if b['metadata'].get('digestion_state', 'synthesized') != 'raw']
        all_buckets = [b for b in all_buckets if not (b['metadata'].get('type') == 'feel' and b['metadata'].get('actor') == self.private_author)]
        if bucket_filter is not None:
            all_buckets = [b for b in all_buckets if bucket_filter(b['metadata'])]
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [b for b in all_buckets if {d.lower() for d in b['metadata'].get('domain', [])} & filter_set]
            if not candidates:
                if strict_domain:
                    return []
                candidates = all_buckets
        else:
            candidates = all_buckets
        if self.hybrid_enabled and self.lexical_engine is not None:
            return await self._search_hybrid(query, candidates, limit, query_valence, query_arousal)
        if self.embedding_engine and self.embedding_engine.enabled:
            try:
                vector_results = await self.embedding_engine.search_similar(query, top_k=50)
                if vector_results:
                    vector_ids = {bid for bid, _ in vector_results}
                    emb_candidates = [b for b in candidates if b['id'] in vector_ids]
                    if emb_candidates:
                        candidates = emb_candidates
            except Exception as e:
                logger.warning(f'Embedding pre-filter failed, using fuzzy only / embedding 预筛失败: {e}')
        scored = []
        for bucket in candidates:
            meta = bucket.get('metadata', {})
            try:
                topic_score = self._calc_topic_score(query, bucket)
                emotion_score = self._calc_emotion_score(query_valence, query_arousal, meta)
                time_score = self._calc_time_score(meta)
                importance_score = max(1, min(10, int(meta.get('importance', 5)))) / 10.0
                total = topic_score * self.w_topic + emotion_score * self.w_emotion + time_score * self.w_time + importance_score * self.w_importance
                weight_sum = self.w_topic + self.w_emotion + self.w_time + self.w_importance
                normalized = total / weight_sum * 100 if weight_sum > 0 else 0
                if normalized >= self.fuzzy_threshold:
                    if effective_status(meta) in ('closed', 'superseded'):
                        normalized *= 0.3
                    bucket['score'] = round(normalized, 2)
                    scored.append(bucket)
            except Exception as e:
                logger.warning(f"Scoring failed for bucket {bucket.get('id', '?')} / 桶评分失败: {e}")
                continue
        scored.sort(key=lambda x: x['score'], reverse=True)
        return scored[:limit]

    async def _search_hybrid(self, query: str, candidates: list[dict], limit: int, query_valence: float, query_arousal: float) -> list[dict]:
        candidate_ids = {b['id'] for b in candidates}
        by_id = {b['id']: b for b in candidates}
        bm25_ranks: dict[str, int] = {}
        bm25_scores: dict[str, float] = {}
        try:
            import asyncio
            self.lexical_engine.ensure_index(candidates)
            bm25_hits = await asyncio.to_thread(self.lexical_engine.search, query, self.channel_top)
            rank = 0
            for bid, _score in bm25_hits:
                if bid in candidate_ids:
                    rank += 1
                    bm25_ranks[bid] = rank
                    bm25_scores[bid] = _score
        except Exception as e:
            logger.warning(f'BM25 channel failed, fusing without it / 词面路失败: {e}')
        vector_ranks: dict[str, int] = {}
        vector_sims: dict[str, float] = {}
        if self.embedding_engine and self.embedding_engine.enabled:
            try:
                vector_results = await self.embedding_engine.search_similar(query, top_k=self.channel_top)
                rank = 0
                for bid, sim in vector_results:
                    if bid in candidate_ids and sim >= self.vector_min_sim:
                        rank += 1
                        vector_ranks[bid] = rank
                        vector_sims[bid] = sim
            except Exception as e:
                logger.warning(f'Vector channel failed, fusing without it / 语义路失败: {e}')
        fuzz_scores: dict[str, float] = {}
        for bucket in candidates:
            try:
                s = self._calc_topic_score(query, bucket)
                if s >= self.fuzz_min:
                    fuzz_scores[bucket['id']] = s
            except Exception:
                continue
        fuzz_ranks = {bid: i + 1 for i, (bid, _s) in enumerate(sorted(fuzz_scores.items(), key=lambda x: x[1], reverse=True)[:self.channel_top])}
        hit_ids = set(bm25_ranks) | set(vector_ranks) | set(fuzz_ranks)
        if not hit_ids:
            return []
        rrf_max = 3.0 / (self.rrf_k + 1)
        fused: list[tuple[str, float, float]] = []
        for bid in hit_ids:
            rrf = 0.0
            for ranks in (bm25_ranks, vector_ranks, fuzz_ranks):
                r = ranks.get(bid)
                if r:
                    rrf += 1.0 / (self.rrf_k + r)
            fused.append((bid, rrf, min(1.0, rrf / rrf_max)))
        fused.sort(key=lambda x: x[1], reverse=True)
        fused = fused[:self.fusion_top]
        bm25_top = max(bm25_scores.values()) if bm25_scores else 0.0

        def _passes_strong_gate(bid: str) -> bool:
            if not self.strong_gate:
                return True
            n_channels = sum((1 for ranks in (bm25_ranks, vector_ranks, fuzz_ranks) if bid in ranks))
            if n_channels >= 2:
                return True
            if bm25_top > 0 and bm25_scores.get(bid, 0.0) >= self.bm25_strong_ratio * bm25_top:
                return True
            if vector_sims.get(bid, 0.0) >= self.vector_only_min_sim:
                return True
            if fuzz_scores.get(bid, 0.0) >= self.fuzz_strong:
                return True
            return False
        scored = []
        for bid, rrf, topic_score in fused:
            if not _passes_strong_gate(bid):
                continue
            bucket = by_id[bid]
            meta = bucket.get('metadata', {})
            try:
                emotion_score = self._calc_emotion_score(query_valence, query_arousal, meta)
                time_score = self._calc_time_score(meta)
                importance_score = max(1, min(10, int(meta.get('importance', 5)))) / 10.0
                total = topic_score * self.w_topic + emotion_score * self.w_emotion + time_score * self.w_time + importance_score * self.w_importance
                weight_sum = self.w_topic + self.w_emotion + self.w_time + self.w_importance
                normalized = total / weight_sum * 100 if weight_sum > 0 else 0
                if effective_status(meta) in ('closed', 'superseded'):
                    normalized *= 0.3
                if bid in vector_ranks and bid not in bm25_ranks and (bid not in fuzz_ranks):
                    if vector_sims.get(bid, 0.0) < self.vector_only_min_sim:
                        continue
                    bucket['vector_match'] = True
                bucket['score'] = round(normalized, 2)
                bucket['debug_ranks'] = {'bm25_rank': bm25_ranks.get(bid), 'fuzz_rank': fuzz_ranks.get(bid), 'vector_rank': vector_ranks.get(bid), 'vector_sim': round(vector_sims[bid], 4) if bid in vector_sims else None, 'rrf': round(rrf, 6), 'topic_score': round(topic_score, 4), 'emotion': round(emotion_score, 4), 'time': round(time_score, 4), 'importance': round(importance_score, 4)}
                scored.append(bucket)
            except Exception as e:
                logger.warning(f'Hybrid scoring failed for bucket {bid} / 混合评分失败: {e}')
                continue
        scored.sort(key=lambda x: x['score'], reverse=True)
        return scored[:limit]

    def _calc_topic_score(self, query: str, bucket: dict) -> float:
        meta = bucket.get('metadata', {})
        name_score = fuzz.partial_ratio(query, meta.get('name', '')) * 3
        domain_score = max((fuzz.partial_ratio(query, d) for d in meta.get('domain', [])), default=0) * 2.5
        tag_score = max((fuzz.partial_ratio(query, tag) for tag in meta.get('tags', [])), default=0) * 2
        content_score = fuzz.partial_ratio(query, bucket.get('content', '')[:1000]) * self.content_weight
        return (name_score + domain_score + tag_score + content_score) / (100 * (3 + 2.5 + 2 + self.content_weight))

    def _calc_emotion_score(self, q_valence: float, q_arousal: float, meta: dict) -> float:
        if q_valence is None or q_arousal is None:
            return 0.5
        try:
            b_valence = float(meta.get('valence', 0.5))
            b_arousal = float(meta.get('arousal', 0.3))
        except (ValueError, TypeError):
            return 0.5
        dist = math.sqrt((q_valence - b_valence) ** 2 + (q_arousal - b_arousal) ** 2)
        return max(0.0, 1.0 - dist / 1.414)

    def _calc_time_score(self, meta: dict) -> float:
        last_active_str = meta.get('last_active', meta.get('created', ''))
        try:
            last_active = parse_ts(last_active_str)
            days = max(0.0, (now_cst() - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days = 30
        return math.exp(-0.02 * days)

    async def list_all(self, include_archive: bool=False, include_quarantine: bool=True) -> list[dict]:
        buckets = []
        dirs = [self.permanent_dir, self.dynamic_dir, self.feel_dir]
        if include_archive:
            dirs.append(self.archive_dir)
        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for filename in files:
                    if not filename.endswith('.md'):
                        continue
                    file_path = os.path.join(root, filename)
                    bucket = self._load_bucket(file_path)
                    if bucket:
                        buckets.append(bucket)
        if not include_quarantine:
            buckets = [b for b in buckets if b['metadata'].get('digestion_state', 'synthesized') != 'raw']
        return buckets

    async def list_since(self, since_iso: str, include_archive: bool=False) -> list[dict]:
        since_dt = parse_ts(since_iso)
        since_ts = since_dt.timestamp()
        result = []
        dirs = [self.permanent_dir, self.dynamic_dir, self.feel_dir]
        if include_archive:
            dirs.append(self.archive_dir)
        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for filename in files:
                    if not filename.endswith('.md'):
                        continue
                    file_path = os.path.join(root, filename)
                    try:
                        if os.path.getmtime(file_path) < since_ts:
                            continue
                    except OSError:
                        continue
                    bucket = self._load_bucket(file_path)
                    if not bucket:
                        continue
                    meta = bucket['metadata']
                    created = str(meta.get('created', ''))
                    last_active = str(meta.get('last_active', created))
                    try:
                        is_new = created and parse_ts(created) > since_dt
                        is_modified = last_active and parse_ts(last_active) > since_dt
                        if is_new or is_modified:
                            bucket['_change_type'] = 'created' if is_new else 'modified'
                            result.append(bucket)
                    except (ValueError, TypeError):
                        continue
        return result

    async def get_stats(self) -> dict:
        stats = {'permanent_count': 0, 'dynamic_count': 0, 'archive_count': 0, 'feel_count': 0, 'total_size_kb': 0.0, 'domains': {}}
        for subdir, key in [(self.permanent_dir, 'permanent_count'), (self.dynamic_dir, 'dynamic_count'), (self.archive_dir, 'archive_count'), (self.feel_dir, 'feel_count')]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith('.md'):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats['total_size_kb'] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats['domains'][domain_name] = stats['domains'].get(domain_name, 0) + 1
        return stats

    async def archive(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        try:
            post = frontmatter.load(file_path)
            domain = post.get('domain', ['未分类'])
            primary_domain = sanitize_name(domain[0]) if domain else '未分类'
            archive_subdir = os.path.join(self.archive_dir, primary_domain)
            os.makedirs(archive_subdir, exist_ok=True)
            dest = safe_path(archive_subdir, os.path.basename(file_path))
            if os.path.exists(dest):
                import time as _time
                _b, _e = os.path.splitext(os.path.basename(file_path))
                dest = safe_path(archive_subdir, f'{_b}-dup{int(_time.time())}{_e}')
            post['type'] = 'archived'
            atomic_write_text(file_path, frontmatter.dumps(post))
            shutil.move(file_path, str(dest))
        except Exception as e:
            logger.error(f'Failed to archive bucket / 归档桶失败: {bucket_id}: {e}')
            return False
        self._append_changelog(bucket_id=bucket_id, bucket_path=os.path.relpath(file_path, self.base_dir), action='archive', fields_changed=[], old_fm_hash='', old_body_hash='', new_fm_hash='', new_body_hash='')
        logger.info(f'Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/')
        return True

    @staticmethod
    def _compute_hash(data) -> str:
        if isinstance(data, dict):
            raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        else:
            raw = str(data) if data else ''
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]

    def _append_changelog(self, bucket_id, bucket_path, action, fields_changed, old_fm_hash, old_body_hash, new_fm_hash, new_body_hash, reason=None, actor='local'):
        entry = {'ts': now_iso(), 'bucket_id': bucket_id, 'bucket_path': bucket_path, 'action': action, 'actor': actor, 'fields_changed': fields_changed, 'old_frontmatter_hash': old_fm_hash, 'old_body_hash': old_body_hash, 'new_frontmatter_hash': new_fm_hash, 'new_body_hash': new_body_hash, 'reason': reason}
        try:
            with open(self.changelog_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except OSError as e:
            logger.warning(f'Failed to write changelog / 变更日志写入失败: {e}')

    def _find_bucket_file(self, bucket_id: str) -> Optional[str]:
        if not bucket_id:
            return None
        for dir_path in [self.permanent_dir, self.dynamic_dir, self.archive_dir, self.feel_dir]:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith('.md'):
                        continue
                    name_part = fname[:-3]
                    if name_part == bucket_id or name_part.endswith(f'_{bucket_id}'):
                        return os.path.join(root, fname)
        return None

    def _load_bucket(self, file_path: str) -> Optional[dict]:
        try:
            post = frontmatter.load(file_path)
            return {'id': post.get('id', Path(file_path).stem), 'metadata': dict(post.metadata), 'content': post.content, 'path': file_path}
        except Exception as e:
            logger.warning(f'Failed to load bucket file / 加载桶文件失败: {file_path}: {e}')
            return None
