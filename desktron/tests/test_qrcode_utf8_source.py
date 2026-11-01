"""Local UTF-8 adapter removes an unexpanded secondary source from QR vendor code."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / 'frontend/vendor'


def test_modified_vendor_digest_and_complete_original_notice():
    row = json.loads((FOLDER / 'qrcode-source.json').read_text(encoding='utf-8'))
    source = (FOLDER / 'qrcode.js').read_bytes()
    assert hashlib.sha256(source).hexdigest() == row['sha256']
    assert b'stackoverflow.com' not in source and b'function toUTF8Array' not in source
    assert b'Array.from(new TextEncoder().encode(s))' in source
    license_data = (FOLDER / row['license_file']).read_bytes()
    assert hashlib.sha256(license_data).hexdigest() == row['license_sha256']
    assert b'Copyright (c) 2009 Kazuhiko Arase' in license_data
    assert b'Permission is hereby granted' in license_data
    assert b'THE SOFTWARE IS PROVIDED' in license_data
    assert b'locally modified qrcode.js' in license_data


def test_utf8_and_qr_matrices_preserve_valid_input_and_normalize_lone_surrogates():
    script = r'''
const assert=require('node:assert/strict');const crypto=require('node:crypto');
const qr=require(process.argv[1]);const utf8=qr.stringToBytesFuncs['UTF-8'];
const cases=[
 ['https://example.com/',25,'594e97f69a9e4e8a3479c245825e08f6deab08f52a243fecc0bd3867c5a8882c'],
 ['中文🙂',21,'3c43306f7a93119277254c5001c30c44a290a2645e20f0c8399ec140d8996b3d'],
 ['a\0b',21,'d57dff2036f4b1ac3fcc0d45676702bb48241ab834a9ed62c9821dfd89cf6d31'],
 ['',21,'4242a2086c2c4c2b9aa3f8238465c59c6d5170e4c8279d9886ae8b6dcd1573f5']];
qr.stringToBytes=utf8;
for(const [s,size,hash] of cases){
 assert.deepEqual(utf8(s),Array.from(Buffer.from(s,'utf8')));
 const q=qr(0,'M');q.addData(s);q.make();assert.equal(q.getModuleCount(),size);
 let bits='';for(let y=0;y<size;y++)for(let x=0;x<size;x++)bits+=q.isDark(y,x)?'1':'0';
 assert.equal(crypto.createHash('sha256').update(bits).digest('hex'),hash);
 assert.match(q.createSvgTag(),/<svg/);
}
for(const s of ['\ud800','\udfff','x\ud800y'])assert.deepEqual(utf8(s),Array.from(Buffer.from(s,'utf8')));
'''
    subprocess.run(['node', '-e', script, str(FOLDER / 'qrcode.js')], check=True,
                   capture_output=True, text=True, encoding='utf-8')
