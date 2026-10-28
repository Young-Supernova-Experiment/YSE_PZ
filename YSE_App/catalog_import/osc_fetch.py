"""
Find Open Supernova Catalog entry files: in local clones of the data repositories, or on GitHub.

The OSC keeps one JSON file per object in repositories split by discovery year
(https://github.com/astrocatalogs/supernovae#readme). For a full import, clone them::

  for r in sne-pre-1990 sne-1990-1999 sne-2000-2004 sne-2005-2009 sne-2010-2014 sne-2015-2019; do
    git clone --depth 1 https://github.com/astrocatalogs/$r.git
  done

and pass the directories to ``manage.py import_osc``. For a few objects, ``--fetch SN2011fe``
downloads them: the index of every repository's files comes from the GitHub API (one request per
repository, cached on disk), the files from raw.githubusercontent.com.

``sne-boneyard`` holds entries the OSC set aside (mostly not supernovae); it is only used when
asked for.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

GITHUB_ORG = 'astrocatalogs'
DATA_REPOSITORIES = (
    'sne-pre-1990', 'sne-1990-1999', 'sne-2000-2004', 'sne-2005-2009', 'sne-2010-2014', 'sne-2015-2019',
    'sne-2020-2024',
)
BONEYARD_REPOSITORY = 'sne-boneyard'
BRANCH = 'master'
TREE_URL = 'https://api.github.com/repos/{org}/{repo}/git/trees/{branch}'
RAW_URL = 'https://raw.githubusercontent.com/{org}/{repo}/{branch}/{path}'
USER_AGENT = 'YSE_PZ-osc-import'
TIMEOUT = 120

_HTML_TAG_RE = re.compile(r'<[^>]*>')


def entry_files(paths: Iterable[str]) -> Iterator[str]:
    """The ``.json`` / ``.json.gz`` files given, and those in the directories given (sorted)."""
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
                for name in sorted(files):
                    if name.endswith('.json') or name.endswith('.json.gz'):
                        yield os.path.join(root, name)
        else:
            yield path


def entry_key(filename: str) -> str:
    """Index key of an entry file: its name without extension or HTML (``<a id="x">x<_a>.json`` -> ``x``)."""
    base = os.path.basename(filename)
    for ext in ('.json.gz', '.json'):
        if base.endswith(ext):
            base = base[:-len(ext)]
            break
    return _HTML_TAG_RE.sub('', base.replace('<_a>', '</a>')).strip()


def _get(url: str, token: Optional[str] = None) -> bytes:
    headers = {'User-Agent': USER_AGENT}
    if token:
        headers['Authorization'] = 'token %s' % token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 (fixed https hosts)
        return response.read()


def build_index(repositories: Sequence[str] = DATA_REPOSITORIES, token: Optional[str] = None,
                fetch=_get) -> Dict[str, Tuple[str, str]]:
    """``{entry key: (repository, path)}`` from the GitHub trees API. Missing repositories are skipped."""
    index: Dict[str, Tuple[str, str]] = {}
    for repo in repositories:
        url = TREE_URL.format(org=GITHUB_ORG, repo=repo, branch=BRANCH)
        try:
            tree = json.loads(fetch(url, token))
        except Exception:  # noqa: BLE001 - an empty or missing repository is not fatal
            continue
        for item in tree.get('tree', []):
            path = item.get('path', '')
            if item.get('type') == 'blob' and (path.endswith('.json') or path.endswith('.json.gz')):
                index.setdefault(entry_key(path), (repo, path))
    return index


def load_index(cache_path: str, repositories: Sequence[str] = DATA_REPOSITORIES, token: Optional[str] = None,
               refresh: bool = False) -> Dict[str, Tuple[str, str]]:
    if not refresh and os.path.exists(cache_path):
        with open(cache_path) as handle:
            return {k: tuple(v) for k, v in json.load(handle).items()}
    index = build_index(repositories, token)
    os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
    with open(cache_path, 'w') as handle:
        json.dump(index, handle)
    return index


def lookup(index: Dict[str, Tuple[str, str]], name: str) -> Optional[Tuple[str, str]]:
    """The entry for ``name``; ``2011fe`` also finds ``SN2011fe`` (and ``AT2011fe``)."""
    name = name.strip()
    for key in (name, 'SN' + name, 'AT' + name, name.replace(' ', '')):
        if key in index:
            return index[key]
    return None


def fetch_entries(names: Iterable[str], dest_dir: str, index: Dict[str, Tuple[str, str]],
                  fetch=_get) -> Tuple[List[str], List[str]]:
    """Download the named entries into ``dest_dir``; returns (local paths, names not found)."""
    os.makedirs(dest_dir, exist_ok=True)
    paths, missing = [], []
    for name in names:
        hit = lookup(index, name)
        if hit is None:
            missing.append(name)
            continue
        repo, path = hit
        local = os.path.join(dest_dir, repo, re.sub(r'[^A-Za-z0-9._+-]', '_', os.path.basename(path)))
        if not os.path.exists(local):
            os.makedirs(os.path.dirname(local), exist_ok=True)
            url = RAW_URL.format(org=GITHUB_ORG, repo=repo, branch=BRANCH, path=urllib.parse.quote(path))
            data = fetch(url)
            with open(local + '.part', 'wb') as handle:
                handle.write(data)
            os.replace(local + '.part', local)
        paths.append(local)
    return paths, missing


def origin_of(path: str) -> str:
    """``<repository>/<file>`` for a file under a data-repository directory, else the path."""
    parts = os.path.abspath(path).split(os.sep)
    for i, part in enumerate(parts[:-1]):
        if part in DATA_REPOSITORIES or part == BONEYARD_REPOSITORY:
            return '/'.join(parts[i:])
    return path
