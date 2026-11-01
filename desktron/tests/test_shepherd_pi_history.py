"""Legacy Pi sources stay readable with no runtime and fail on corrupt evidence."""
import json
import os
from _platform import symlink
from unittest import mock

import pytest

from shepherd.pi_history import read_entries, source_range
from shepherd.store import Conflict


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'session.jsonl'
    header = {'type':'session','version':3,'id':'session','cwd':str(tmp_path)}
    rows = [{'type':'custom','id':'a','parentId':None,'customType':'shepherd-round-start',
             'data':{'roundId':'turn','entryHash':'hash','codec':'codec'}},
            {'type':'message','id':'b','parentId':'a','message':{'role':'user','content':'hello'}},
            {'type':'custom','id':'c','parentId':'b','customType':'shepherd-round-commit',
             'data':{'roundId':'turn','entryHash':'hash','codec':'codec'}}]
    path.write_text(''.join(json.dumps(row)+'\n' for row in [header,*rows])); path.chmod(0o600)
    return path, header, rows


def test_no_runtime_read_preserves_raw_ranges_and_markers(source):
    path, header, rows = source
    before = path.read_bytes()
    with mock.patch('subprocess.Popen', side_effect=AssertionError('no SDK process')):
        entries = read_entries(path, 'session', str(path.parent))
    assert entries == rows and path.read_bytes() == before
    assert source_range(entries, {'roundId':'turn'}, 'codec') == rows
    assert source_range(entries, {'start':1,'end':2}, 'codec') == rows[1:2]


@pytest.mark.parametrize('change', [{'version':4}, {'version':True}, {'cwd':'.'},
    {'cwd':None}, {'cwd':''}, {'id':'other'}, {'type':'future'}])
def test_header_identity_and_format_remain_strict(source, change):
    path, header, rows = source
    path.write_text(''.join(json.dumps(row)+'\n' for row in [{**header,**change},*rows]))
    with pytest.raises(Conflict): read_entries(path, 'session', str(path.parent))


@pytest.mark.parametrize('change', [{'parentId':[]}, {'parentId':'foreign'}, {'id':'a'}])
def test_corrupt_tree_rejected(source, change):
    path, header, rows = source
    rows[1].update(change)
    path.write_text(''.join(json.dumps(row)+'\n' for row in [header,*rows]))
    with pytest.raises(Conflict): read_entries(path, 'session', str(path.parent))


def test_real_symlink_rejected(source):
    path, _, _ = source
    alias = path.with_name('link'); symlink(path, alias)
    with pytest.raises(Conflict): read_entries(alias, 'session', str(path.parent))


def test_truncation_permissions_and_marker_corruption_rejected(source):
    path, header, rows = source
    if os.name != 'nt':
        path.chmod(0o644)
        with pytest.raises(Conflict): read_entries(path, 'session', str(path.parent))
    path.chmod(0o600); path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(Conflict): read_entries(path, 'session', str(path.parent))
    rows[-1]['data']['entryHash'] = 'tampered'
    with pytest.raises(Conflict): source_range(rows, {'roundId':'turn'}, 'codec')
    for start,end in [(True,2),(0,True),(-1,1),(0,9),(1,1)]:
        with pytest.raises(Conflict): source_range(rows, {'start':start,'end':end}, 'codec')
