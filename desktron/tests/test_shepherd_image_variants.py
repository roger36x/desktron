"""image_variants private immutable storage and failure boundaries."""
import base64
import json
import os
import stat
from _platform import assert_private_file, assert_private_directory, symlink
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from shepherd.image_transport import ClaudeImageTransport as Transport, ImageInputError
from shepherd.image_variants import ImageVariants, MAX_RECORD_BYTES
from test_shepherd_image_transport import png, native


def ClaudeImageTransport(root):
    return Transport(root, 'test')


@pytest.fixture
def large():
    return png((2100, 20))


def test_durable_variant_readback_reuses_exact_bytes_without_reencoding(tmp_path, large):
    sent = ClaudeImageTransport(tmp_path).image(native(large))
    f = next((tmp_path/'image-variants'/'test').glob('*.json')); saved=f.read_bytes()
    assert_private_file(f)
    assert_private_directory(f.parent)
    t=ClaudeImageTransport(tmp_path)
    with mock.patch.object(t,'_compress',side_effect=AssertionError('must reuse durable variant')):
        assert t.image(native(large))==sent
    assert f.read_bytes()==saved
    assert t.receipt()['derivations'][0]['variant_key']==f.stem


@pytest.mark.parametrize('damage', ['hash','data','identity','permissions','symlink','oversize','directory'])
def test_corrupt_or_unsafe_objects_fail_closed_not_recompressed(tmp_path, large, damage):
    ClaudeImageTransport(tmp_path).image(native(large))
    f=next((tmp_path/'image-variants'/'test').glob('*.json'))
    record=json.loads(f.read_bytes())
    if damage=='hash':
        record['receipt']['variant_sha256']='0'*64; f.write_text(json.dumps(record))
    elif damage=='data':
        record['data']=base64.b64encode(png((1,1))).decode(); f.write_text(json.dumps(record))
    elif damage=='identity':
        record['receipt']['source_sha256']='0'*64; f.write_text(json.dumps(record))
    elif damage=='permissions':
        if os.name == 'nt':
            pytest.skip('POSIX 权限位拒绝不适用于 Windows ACL')
        f.chmod(0o644)
    elif damage=='symlink':
        other=tmp_path/'other'; f.rename(other); symlink(other, f)
    elif damage=='oversize':
        with f.open('wb') as stream: stream.truncate(MAX_RECORD_BYTES+1)
    else:
        if os.name == 'nt':
            pytest.skip('POSIX 目录权限位拒绝不适用于 Windows ACL')
        f.parent.chmod(0o755)
    t=ClaudeImageTransport(tmp_path)
    with mock.patch.object(t,'_compress',side_effect=AssertionError('do not hide corruption')):
        with pytest.raises(ImageInputError): t.image(native(large))


def test_symlink_directory_is_not_followed(tmp_path, large):
    other=tmp_path/'other'; other.mkdir()
    (tmp_path/'image-variants').symlink_to(other)
    with pytest.raises(ImageInputError): ClaudeImageTransport(tmp_path).image(native(large))
    assert not list(other.iterdir())


def test_wrong_owner_is_rejected(tmp_path, large):
    ClaudeImageTransport(tmp_path).image(native(large))
    old=os.fstat
    def wrong(fd):
        info=old(fd)
        if stat.S_ISREG(info.st_mode):
            return type('Info',(),{'st_mode':info.st_mode,'st_uid':os.getuid()+1,'st_size':info.st_size})()
        return info
    with mock.patch('shepherd.image_variants.os.fstat',side_effect=wrong):
        with pytest.raises(ImageInputError,match='权限'):
            ClaudeImageTransport(tmp_path).image(native(large))


def test_file_growth_after_fstat_is_bounded(tmp_path, large):
    ClaudeImageTransport(tmp_path).image(native(large))
    f=next((tmp_path/'image-variants'/'test').glob('*.json')); old=os.fstat; grew=False
    def grow(fd):
        nonlocal grew
        info=old(fd)
        if stat.S_ISREG(info.st_mode) and not grew:
            with f.open('wb') as stream: stream.truncate(MAX_RECORD_BYTES+1)
            grew=True
        return info
    with mock.patch('shepherd.image_variants.os.fstat',side_effect=grow):
        with pytest.raises(ImageInputError,match='上限'):
            ClaudeImageTransport(tmp_path).image(native(large))


def test_publish_failure_does_not_return_unpersisted_variant(tmp_path, large):
    with mock.patch('shepherd.image_variants.os.link',side_effect=OSError('disk full')):
        with pytest.raises(ImageInputError): ClaudeImageTransport(tmp_path).image(native(large))
    assert not list((tmp_path/'image-variants'/'test').glob('*'))


def test_variants_are_scoped_and_removed_with_conversation(tmp_path, large):
    from shepherd.physical_delete import purge, mark_deleted
    Transport(tmp_path,'first').image(native(large))
    Transport(tmp_path,'second').image(native(large))
    first=tmp_path/'image-variants'/'first'; second=tmp_path/'image-variants'/'second'
    assert first.exists() and second.exists()
    mark_deleted(tmp_path,'first')
    purge(tmp_path,'first',{'bindings':{}},remove_history=False)
    assert not first.exists() and second.exists()


def test_concurrent_identical_derivations_have_one_immutable_record(tmp_path, large):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:ClaudeImageTransport(tmp_path).image(native(large)),range(3)))
    assert results[0]==results[1]==results[2]
    assert len(list((tmp_path/'image-variants'/'test').glob('*.json')))==1
    assert not list((tmp_path/'image-variants'/'test').glob('.variant-*'))
