"""Explicit local configuration, with no host discovery or remote defaults."""
import os
import pathlib
import tomllib
from . import utils
from .bucket_manager import BucketManager


def load(path):
    path = pathlib.Path(path).resolve(strict=True)
    cfg = tomllib.loads(path.read_text(encoding='utf-8'))
    def local(value):
        p = pathlib.Path(value).expanduser()
        return (path.parent / p).resolve()
    cfg['data_dir'] = local(cfg.get('data_dir', './data'))
    actor = cfg.get('actor')
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError('actor 必须是非空字符串')
    audiences = cfg.get('audiences', [actor])
    if not isinstance(audiences, list) or not audiences or any(not isinstance(x, str) or not x.strip() for x in audiences):
        raise ValueError('audiences 必须是非空字符串数组')
    cfg['audiences'] = audiences
    cfg['dictionary_files'] = [local(x) for x in cfg.get('dictionary_files', [])]
    cfg['aliases_file'] = local(cfg['aliases_file']) if cfg.get('aliases_file') else None
    return cfg


def manager(cfg):
    # Configure one root per process. No automatic legacy identity remapping.
    utils._EXTRA_AUDIENCE_PARTS.clear()
    utils.register_audience_parts(cfg['audiences'])
    root = cfg['data_dir'] / 'buckets'
    for name in ('permanent', 'dynamic', 'archive', 'feel', 'quarantine'):
        (root / name).mkdir(parents=True, exist_ok=True)
    # Entry point has an explicit default, without changing the caller's environment.
    engine_cfg = {**cfg, 'buckets_dir': str(root)}
    return BucketManager(engine_cfg)


async def write(cfg, text, name=None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('正文不能为空')
    return await manager(cfg).create(text, name=name, actor=cfg['actor'], domain=['notes'], source_type='self', trust_level='trusted', digestion_state='owned')


async def search(cfg, query, limit=5):
    # This adapter is a private local store; imported peer records stay out of results.
    if not 1 <= limit <= 50:
        raise ValueError('limit 必须为 1..50')
    actor = cfg['actor']
    private = cfg.get('private_author', 'human')
    return await manager(cfg).search(query, limit=limit, bucket_filter=lambda m: m.get('actor') == actor and not (m.get('type') == 'feel' and m.get('actor') == private))
