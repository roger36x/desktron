/** Shared, version-pinned Claude Agent SDK option normalization. */
import {lstat, readlink, readdir, realpath} from 'node:fs/promises';
import {homedir} from 'node:os';
import {basename, dirname, isAbsolute, parse, relative, resolve, sep} from 'node:path';
import {fileURLToPath} from 'node:url';

// Unified archive has a turn-scoped Writer, not a durable native task mailbox.
// Force this after caller env (including bypass mode); do not change global CLI settings.
// Non-essential traffic (auto-update check/install, telemetry) must not run inside
// a turn: a slow npm update check can hold a fresh CLI before it reads its input.
export const claudeTurnEnv = {CLAUDE_CODE_DISABLE_BACKGROUND_TASKS:'1',
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:'1'};
export const claudeBashEnvBody = `# Desktron: keep Claude parent identity out of Bash tool subprocesses.
for __desk_name in $(env | sed -n 's/^\\(CLAUDE[A-Za-z0-9_]*\\)=.*/\\1/p'); do
  case "$__desk_name" in
    CLAUDECODE|CLAUDE_CONFIG_DIR|CLAUDE_EFFORT|CLAUDE_PID|CLAUDE_AGENT_SDK|CLAUDE_AGENT_SDK_*|\\
CLAUDE_CODE_CHILD_SESSION|CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING|\\
CLAUDE_CODE_ENTRYPOINT|CLAUDE_CODE_EXECPATH|CLAUDE_CODE_BRIDGE_*|\\
CLAUDE_CODE_HOST_*|CLAUDE_CODE_MESSAGING_*|CLAUDE_CODE_RELAUNCH_*|\\
CLAUDE_CODE_SDK_*|CLAUDE_CODE_SESSION_*|CLAUDE_CODE_OAUTH_TOKEN) unset "$__desk_name" ;;
  esac
done
unset __desk_name BASH_ENV CLAUDE_ENV_FILE
`;
export const claudeDisallowedTools = ['Agent', 'Task', 'Monitor'];
export const CLAUDE_PROTECTED_PATH_MESSAGE =
  'This macOS protected directory is unavailable in this session. Continue without it, use workspace data, or explain why that specific file is required.';
export const CLAUDE_BACKGROUND_BASH_MESSAGE =
  'Background Bash is unavailable in this session. Run the command in the foreground so it remains owned and cancellable.';
export const CLAUDE_PROJECT_CONFIG_MESSAGE =
  'This Desk-managed Claude project configuration cannot be changed from this session. Edit the Desktron source and sync it through the approved deployment flow.';

function comparisonPath(value) {
  // Default macOS and Windows filesystems are case-insensitive. A lexical guard
  // must compare the same way or a case variant can bypass a protected path.
  return ['darwin','win32'].includes(process.platform)
    ? value.normalize('NFC').toLowerCase() : value;
}

function within(path, root) {
  path=comparisonPath(stablePath(path));root=comparisonPath(stablePath(root));
  return path === root || path.startsWith(root.endsWith(sep) ? root : root + sep);
}

function windowsNamespacePath(value) {
  const raw=String(value||'');
  if (process.platform!=='win32') return raw;
  const path=raw.replaceAll('/','\\');
  if (/^\\\\\?\\UNC\\/i.test(path)) return `\\\\${path.slice(8)}`;
  if (/^\\\\\?\\[A-Za-z]:\\/.test(path)) return path.slice(4);
  return raw;
}

function unsafeWindowsPath(value) {
  if (process.platform!=='win32') return false;
  let raw=String(value||'');
  if (raw.startsWith('file://')) {
    try {raw=fileURLToPath(raw);} catch {return true;}
  }
  const namespace=raw.replaceAll('/','\\');
  if (/^\\\\\.\\/.test(namespace)) return true;
  if (/^\\\\\?\\/.test(namespace)
      && !/^\\\\\?\\(?:UNC\\|[A-Za-z]:\\)/i.test(namespace)) return true;
  const path=windowsNamespacePath(raw),root=parse(path).root;
  return path.slice(root.length).includes(':');
}

function stablePath(value) {
  const path=resolve(windowsNamespacePath(value));
  if (process.platform==='darwin') {
    for (const alias of ['/var','/tmp','/etc']) {
      const prefixed='/private'+alias;
      if (path===prefixed || path.startsWith(prefixed+sep))
        return path.slice('/private'.length);
    }
  }
  return path;
}

function normalizeRoots(values) {
  return [...new Set((Array.isArray(values) ? values : [])
    .filter(value=>typeof value==='string' && isAbsolute(value)).map(stablePath))];
}

function absoluteToolPath(value, cwd) {
  if (typeof value!=='string' || !value || value.includes('\0')) return '';
  let raw=value;
  if (raw.startsWith('file://')) {
    try { raw=fileURLToPath(raw); } catch { return ''; }
  }
  if (raw==='~' || raw.startsWith('~/')) raw=resolve(homedir(),raw.slice(raw==='~' ? 1 : 2));
  return stablePath(resolve(cwd,raw));
}

function lexicallyProtected(path,allowedRoots,roots,{traversal=false}={}) {
  if (!path || allowedRoots.some(root=>within(path,root))) return false;
  // Traversal may pass through ancestors of an explicitly allowed nested root,
  // but the parent itself is never an allowed final target.
  if (traversal && allowedRoots.some(root=>within(root,path))) return false;
  return roots.some(root=>within(path,root));
}

const DEFAULT_PATH_CHECK_MS=500;
const PROJECT_WRITE_TOOLS=new Set(['Write','Edit','MultiEdit','NotebookEdit']);

function missingPath(error) {
  return ['ENOENT','ENOTDIR'].includes(error?.code);
}

async function boundedFilesystemCall(operation,deadline) {
  const remaining=deadline-performance.now();
  if (remaining<=0) throw Object.assign(new Error('path check timed out'),{code:'ETIMEDOUT'});
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(
        Object.assign(new Error('path check timed out'),{code:'ETIMEDOUT'})),remaining);}),
    ]);
  } finally {clearTimeout(timer);}
}

async function canonicalWindowsPath(path,filesystem,deadline) {
  if (process.platform!=='win32') return {path,uncertain:false};
  let cursor=path;
  const suffix=[];
  for (let depth=0;depth<64;depth++) {
    try {
      const physical=await boundedFilesystemCall(()=>filesystem.realpath(cursor),deadline);
      return {path:stablePath(resolve(physical,...suffix)),uncertain:false};
    } catch (error) {
      if (!missingPath(error)) return {path,uncertain:true};
      const parent=dirname(cursor);
      if (parent===cursor) return {path,uncertain:false};
      suffix.unshift(basename(cursor));
      cursor=parent;
    }
  }
  return {path,uncertain:true};
}

async function finalizeResolvedPath(path,allowedRoots,roots,filesystem,deadline) {
  const canonical=await canonicalWindowsPath(path,filesystem,deadline);
  if (canonical.uncertain) return {path,protected:false,uncertain:true};
  return {path:canonical.path,
    protected:lexicallyProtected(canonical.path,allowedRoots,roots),uncertain:false};
}

async function resolveLinkComponents(target,allowedRoots,roots,filesystem,deadline) {
  for (let hops=0;hops<24;hops++) {
    if (lexicallyProtected(target,allowedRoots,roots))
      return {path:target,protected:true,uncertain:false};
    const parsed=parse(target),parts=target.slice(parsed.root.length).split(sep).filter(Boolean);
    let current=parsed.root,followed=false;
    for (let index=0;index<parts.length;index++) {
      current=resolve(current,parts[index]);
      if (lexicallyProtected(current,allowedRoots,roots,{traversal:true}))
        return {path:current,protected:true,uncertain:false};
      let info;
      try {info=await boundedFilesystemCall(()=>filesystem.lstat(current),deadline);}
      catch (error) {
        if (missingPath(error))
          return finalizeResolvedPath(target,allowedRoots,roots,filesystem,deadline);
        return {path:target,protected:false,uncertain:true};
      }
      if (!info.isSymbolicLink()) continue;
      let link;
      try {link=await boundedFilesystemCall(()=>filesystem.readlink(current),deadline);}
      catch {return {path:target,protected:false,uncertain:true};}
      // Keep the physical target here. Re-applying the /private alias rewrite
      // would turn /var back into the same /var symlink on every hop.
      target=resolve(dirname(current),link,...parts.slice(index+1));
      followed=true;break;
    }
    if (!followed)
      return finalizeResolvedPath(target,allowedRoots,roots,filesystem,deadline);
  }
  // Exhausting the bounded link chain is uncertainty: deny instead of exposing
  // a target which the platform may still resolve differently.
  return {path:target,protected:false,uncertain:true};
}

function pathCheckOptions(options={}) {
  const filesystem=options.filesystem || {};
  const timeout=Number(options.timeoutMs);
  return {filesystem:{lstat:filesystem.lstat || lstat,readlink:filesystem.readlink || readlink,
      readdir:filesystem.readdir || readdir,realpath:filesystem.realpath || realpath},
    deadline:performance.now()+(Number.isFinite(timeout) && timeout>0 ? timeout : DEFAULT_PATH_CHECK_MS)};
}

async function inspectToolPath(value,base,roots,allowedRoots,check) {
  if (unsafeWindowsPath(value)) return {path:'',protected:false,uncertain:true};
  const target=absoluteToolPath(value,base);
  if (!target) return {path:'',protected:false,uncertain:false};
  return resolveLinkComponents(target,allowedRoots,roots,check.filesystem,check.deadline);
}

export async function claudePathIsProtected(value,cwd,protectedPaths,allowedPaths=[],options={}) {
  const roots=normalizeRoots(protectedPaths),base=stablePath(cwd || process.cwd());
  if (!roots.length) return false;
  const result=await inspectToolPath(value,base,roots,[base,...normalizeRoots(allowedPaths)],
    pathCheckOptions(options));
  return result.protected || result.uncertain;
}

function toolPaths(input,context={},toolName='',cwd='') {
  const values=[];
  if (typeof context.blockedPath==='string') values.push(context.blockedPath);
  if (!input || typeof input!=='object' || Array.isArray(input)) return values;
  for (const key of ['file_path','path','notebook_path'])
    if (typeof input[key]==='string') values.push(input[key]);
  if (Array.isArray(input.paths))
    values.push(...input.paths.filter(value=>typeof value==='string'));
  // Glob combines path and pattern. Validate the effective target too, so an
  // allowed Downloads/project root cannot use ../* to enumerate its siblings.
  if (toolName==='Glob' && typeof input.pattern==='string') {
    if (typeof input.path==='string' && input.path) {
      const globBase=absoluteToolPath(input.path,cwd || process.cwd());
      if (globBase) values.push(isAbsolute(input.pattern) || input.pattern.startsWith('~/')
        ? input.pattern : resolve(globBase,input.pattern));
    } else if (isAbsolute(input.pattern) || input.pattern.startsWith('~/')
        || /^(?:\.\.?(?:[\\/]|$))/.test(input.pattern)) values.push(input.pattern);
  }
  return values;
}

function projectConfigPath(path,controls) {
  const compared=comparisonPath(stablePath(path));
  if (controls.configFiles.some(file=>comparisonPath(stablePath(file))===compared)) return true;
  if (controls.resourceRoots.some(root=>within(compared,root))) return true;
  for (const root of controls.claudeRoots) {
    const comparedRoot=comparisonPath(stablePath(root));
    if (!within(compared,comparedRoot)) continue;
    const tail=relative(comparedRoot,compared).split(sep).join('/');
    if ([comparisonPath('CLAUDE.md'),comparisonPath('CLAUDE.local.md'),
          comparisonPath('AGENTS.md')].includes(tail) || /^settings.*\.json$/.test(tail)
        || ['skills','rules'].includes(tail)
        || tail.startsWith('skills/') || tail.startsWith('rules/')) return true;
  }
  return false;
}

async function listDirectory(path,check) {
  try {return await boundedFilesystemCall(
    ()=>check.filesystem.readdir(path,{withFileTypes:true}),check.deadline);}
  catch (error) {
    if (missingPath(error)) return [];
    throw error;
  }
}

async function collectResourceLinks(folder,controls,check,state,depth=0) {
  if (depth>24 || state.count>4096) {controls.uncertain=true;return;}
  let entries;
  try {entries=await listDirectory(folder,check);}
  catch {controls.uncertain=true;return;}
  for (const entry of entries) {
    if (++state.count>4096) {controls.uncertain=true;return;}
    const child=resolve(folder,entry.name);
    if (entry.isDirectory()) {
      await collectResourceLinks(child,controls,check,state,depth+1);
      continue;
    }
    if (!entry.isSymbolicLink()) continue;
    const resolved=await resolveLinkComponents(child,[],[],check.filesystem,check.deadline);
    if (resolved.uncertain) {controls.uncertain=true;return;}
    let info;
    try {info=await boundedFilesystemCall(()=>check.filesystem.lstat(resolved.path),check.deadline);}
    catch (error) {
      if (!missingPath(error)) controls.uncertain=true;
      continue;
    }
    if (info.isDirectory()) {
      controls.resourceRoots.push(resolved.path);
      await collectResourceLinks(child,controls,check,state,depth+1);
    } else controls.configFiles.push(resolved.path);
  }
}

function loadedInstructionPaths(base) {
  const paths=[];
  for (let folder=base,depth=0;depth<64;depth++) {
    for (const name of ['CLAUDE.md','CLAUDE.local.md','AGENTS.md'])
      paths.push(resolve(folder,name));
    const parent=dirname(folder);
    if (parent===folder) break;
    folder=parent;
  }
  for (const name of ['CLAUDE.md','CLAUDE.local.md'])
    paths.push(resolve(homedir(),'.claude',name));
  return [...new Set(paths.map(stablePath))];
}

async function projectControls(base,check) {
  const lexicalClaude=resolve(base,'.claude');
  const instructionFiles=loadedInstructionPaths(base);
  const controls={claudeRoots:[lexicalClaude],configFiles:[...instructionFiles],
    resourceRoots:[],uncertain:false};
  const resources=[
    [lexicalClaude,'claudeRoot'],
    ...instructionFiles.map(path=>[path,'file']),
    [resolve(lexicalClaude,'CLAUDE.md'),'file'],
    [resolve(lexicalClaude,'CLAUDE.local.md'),'file'],
    [resolve(lexicalClaude,'skills'),'resourceRoot'],
    [resolve(lexicalClaude,'rules'),'resourceRoot'],
  ];
  for (const [path,kind] of resources) {
    const result=await resolveLinkComponents(path,[],[],check.filesystem,check.deadline);
    if (result.uncertain) controls.uncertain=true;
    if (kind==='claudeRoot') controls.claudeRoots.push(result.path);
    else if (kind==='resourceRoot') controls.resourceRoots.push(result.path);
    else controls.configFiles.push(result.path);
  }
  let rootEntries=[];
  try {rootEntries=await listDirectory(lexicalClaude,check);}
  catch {controls.uncertain=true;}
  for (const entry of rootEntries) {
    if (!/^settings.*\.json$/.test(comparisonPath(entry.name))) continue;
    const result=await resolveLinkComponents(
      resolve(lexicalClaude,entry.name),[],[],check.filesystem,check.deadline);
    if (result.uncertain) controls.uncertain=true;
    controls.configFiles.push(result.path);
  }
  const resourceState={count:0};
  await collectResourceLinks(resolve(lexicalClaude,'skills'),controls,check,resourceState);
  await collectResourceLinks(resolve(lexicalClaude,'rules'),controls,check,resourceState);
  return controls;
}

async function protectedToolDecision(input,context,toolName,base,roots,extraRoots,options={}) {
  if (toolName==='Glob' && typeof input?.pattern==='string' && input.pattern.includes('..')) {
    const globBase=absoluteToolPath(input.path || '.',base);
    if (roots.some(root=>within(globBase,root)))
      return {behavior:'deny',message:CLAUDE_PROTECTED_PATH_MESSAGE};
  }
  const paths=toolPaths(input,context,toolName,base);
  if (!paths.length) return null;
  const check=pathCheckOptions(options),allowedRoots=[base,...extraRoots];
  const writesProjectConfig=PROJECT_WRITE_TOOLS.has(toolName);
  const controls=writesProjectConfig ? await projectControls(base,check) : null;
  if (controls?.uncertain)
    return {behavior:'deny',message:CLAUDE_PROJECT_CONFIG_MESSAGE};
  for (const value of paths) {
    if (unsafeWindowsPath(value))
      return {behavior:'deny',message:writesProjectConfig
        ? CLAUDE_PROJECT_CONFIG_MESSAGE : CLAUDE_PROTECTED_PATH_MESSAGE};
    const lexical=absoluteToolPath(value,base);
    if (writesProjectConfig && lexical && projectConfigPath(lexical,controls))
      return {behavior:'deny',message:CLAUDE_PROJECT_CONFIG_MESSAGE};
    const inspected=await inspectToolPath(value,base,roots,allowedRoots,check);
    if (inspected.uncertain || inspected.protected)
      return {behavior:'deny',message:CLAUDE_PROTECTED_PATH_MESSAGE};
    if (writesProjectConfig && inspected.path && projectConfigPath(inspected.path,controls))
      return {behavior:'deny',message:CLAUDE_PROJECT_CONFIG_MESSAGE};
  }
  return null;
}

export async function claudeProtectedToolDecision(input,context,cwd,protectedPaths,
                                                   allowedPaths=[],toolName='',options={}) {
  const base=stablePath(cwd || process.cwd());
  return protectedToolDecision(input,context,toolName,base,normalizeRoots(protectedPaths),
    normalizeRoots(allowedPaths).filter(root=>root!==base),options);
}

export function claudeFilesystemOptions(cwd,protectedPaths,allowedPaths=[],pathOptions={}) {
  const roots=normalizeRoots(protectedPaths),base=stablePath(cwd || process.cwd());
  const extraRoots=normalizeRoots(allowedPaths).filter(root=>root!==base);
  const hook=async input=>{
    if (input?.tool_name==='Bash' && input?.tool_input?.run_in_background===true)
      return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',
        permissionDecisionReason:CLAUDE_BACKGROUND_BASH_MESSAGE}};
    const decision=await protectedToolDecision(input?.tool_input,input,input?.tool_name,
      base,roots,extraRoots,pathOptions);
    return decision ? {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',
      permissionDecisionReason:decision.message}} : {};
  };
  // Do not enable Claude's filesystem sandbox or pass TCC-sensitive directories
  // to it. Main turns use the native Bash tool; macOS owns Bash authorization.
  // Explicit-path tools still fail before access, and registered work roots stay
  // available without broadening the protected parent directory.
  return {
    hooks:{PreToolUse:[{timeout:1,hooks:[hook]}]},
    ...(extraRoots.length ? {additionalDirectories:extraRoots} : {}),
  };
}

export function isClaudeTaskNotificationResult(message, sessionId, terminalUuid='') {
  if (message.type!=='result' || message.origin?.kind!=='task-notification') return false;
  // A cold resume reports orphaned Bash tasks with a zero-turn result before
  // consuming the queued human frame. It is neither that frame's completion nor
  // permission to accept arbitrary autonomous model/tool execution.
  if (message.session_id!==sessionId || message.subtype!=='success'
      || message.is_error!==false || message.num_turns!==0 || message.result!==''
      || message.terminal_reason || terminalUuid)
    throw new Error('Claude task notification is not a verified no-op result');
  return true;
}

export function claudeExtraArgs(thinkingDisplay='summarized', autocompact='') {
  const args={};
  if(['summarized','omitted'].includes(thinkingDisplay ?? 'summarized'))
    args['thinking-display']=thinkingDisplay ?? 'summarized';
  const raw=String(autocompact || '').trim(), tokens=Number(raw);
  if(raw && raw!=='auto' && Number.isSafeInteger(tokens)
      && tokens>=100000 && tokens<=1000000)
    args.autocompact=String(tokens);
  return Object.keys(args).length ? args : undefined;
}

// Claude Code 2.1.291 resumes from the newest `last-prompt.leafUuid` when one exists.
// Desk appends portable rounds after native records without rewriting that metadata,
// so a stale hint branches the resumed turn away from the stored tail (the archive then
// rejects it as an anchor parent mismatch). Only the SDK's view drops hints that no
// longer name the tail; storage, revisions and Desk's own load() callers are untouched.
export function withoutStaleLeafHints(entries) {
  const tail=entries.findLast(entry=>typeof entry?.uuid==='string' && entry.uuid)?.uuid;
  return entries.filter(entry=>entry?.type!=='last-prompt' || entry.leafUuid===tail);
}

export function claudeResumeStore(store) {
  return new Proxy(store, {get(target, prop) {
    if (prop==='load') return async key=>withoutStaleLeafHints(await target.load(key));
    const value=Reflect.get(target, prop, target);
    return typeof value==='function' ? value.bind(target) : value;
  }});
}
