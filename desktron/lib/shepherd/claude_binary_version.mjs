import {spawnSync} from 'node:child_process';
import {readFileSync, existsSync} from 'node:fs';
import {dirname, resolve} from 'node:path';

/** Probe the exact executable selected for this native request. */
export function claudeBinaryVersion(binary, env=process.env) {
  if (typeof binary !== 'string' || !binary || binary.length > 4096)
    throw new Error('Claude executable path missing');
  let executable=binary, args=['--version'];
  if (process.platform==='win32' && /\.cmd$/i.test(binary)) {
    // Resolve only the npm JS shim; never invoke a shell or reparse user input.
    const shim=readFileSync(binary,'utf8');
    const match=shim.match(/"(?:%~dp0|%dp0%)[\\/]*([^"\r\n]+?\.(?:js|cjs|mjs))"/i);
    if (!match) throw new Error('Claude executable is not a supported npm shim');
    const entry=resolve(dirname(binary),match[1].replaceAll('\\','/'));
    if (!existsSync(entry)) throw new Error('Claude npm entry missing');
    executable=process.execPath; args=[entry,'--version'];
  }
  const result=spawnSync(executable,args,{
    encoding:'utf8',env,timeout:5000,maxBuffer:4096,windowsHide:true,
    stdio:['ignore','pipe','pipe'],
  });
  if (result.error) throw new Error(`Claude version probe failed: ${result.error.message}`);
  if (result.status!==0) throw new Error(`Claude version probe exited ${result.status}`);
  const output=String(result.stdout || result.stderr || '').trim();
  const match=output.match(/(?:^|\s)(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)(?:\s|$)/);
  if (!match) throw new Error('Claude version probe returned an unsupported value');
  return match[1];
}

/** Verify the already-started query process without releasing its user input. */
export async function verifyClaudeProcessVersion(query, expected, timeoutMs=5000) {
  if (!query || typeof query.request!=='function')
    throw new Error('Claude SDK does not support predispatch process version verification');
  let timer;
  try {
    const result=await Promise.race([
      query.request({subtype:'get_binary_version'}),
      new Promise((_,reject)=>{timer=setTimeout(
        ()=>reject(new Error('Claude process version verification timed out')),timeoutMs);}),
    ]);
    const version=result?.subtype==='success' && result.response
      && typeof result.response.version==='string' ? result.response.version : '';
    if (!/^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$/.test(version))
      throw new Error('Claude process version response is unsupported');
    if (version!==expected)
      throw new Error(`Claude executable/process version mismatch: ${expected} != ${version}`);
    return version;
  } finally {
    if (timer) clearTimeout(timer);
  }
}
