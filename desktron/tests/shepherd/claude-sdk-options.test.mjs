import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {existsSync,mkdtempSync,mkdirSync,rmSync,symlinkSync as nativeSymlinkSync,writeFileSync} from 'node:fs';
import {homedir,tmpdir} from 'node:os';
import {join} from 'node:path';
import {claudeBashEnvBody,claudeExtraArgs,claudeFilesystemOptions,claudePathIsProtected,
  claudeProtectedToolDecision} from '../../lib/shepherd/claude_sdk_options.mjs';

assert.deepEqual(claudeExtraArgs('summarized','400000'),{
  'thinking-display':'summarized',autocompact:'400000'});
assert.deepEqual(claudeExtraArgs('omitted',1000000),{
  'thinking-display':'omitted',autocompact:'1000000'});
assert.deepEqual(claudeExtraArgs('off','auto'),undefined);
for(const value of ['99999','1000001','bad',''])
  assert.equal(claudeExtraArgs('off',value),undefined,value);

// Windows junctions exercise real redirected paths without developer-mode privilege.
const symlinkSync=(source,target)=>nativeSymlinkSync(source,target,process.platform==='win32'?'junction':undefined);
const fixtureFile=(path,text)=>process.platform==='win32'?mkdirSync(path):writeFileSync(path,text);
const root=mkdtempSync(join(tmpdir(),'claude-filesystem-options-'));
try {
  const envFile=join(root,'claude-env.sh');writeFileSync(envFile,claudeBashEnvBody,{mode:0o600});
  for(const shell of ['/bin/bash','/bin/zsh']) {
    if(!existsSync(shell)) continue;
    const probe=spawnSync(shell,['-c',`. "$CLAUDE_ENV_FILE"; printf '%s|%s|%s' "\${CLAUDE_CONFIG_DIR-unset}" "\${CLAUDE_CODE_SESSION_ID-unset}" "\${CLAUDE_PROJECT_DIR-unset}"`],{
      encoding:'utf8',env:{PATH:process.env.PATH,CLAUDE_ENV_FILE:envFile,
        CLAUDE_CONFIG_DIR:'private',CLAUDE_CODE_SESSION_ID:'private',CLAUDE_PROJECT_DIR:'kept'}});
    assert.equal(probe.status,0,probe.stderr);
    assert.equal(probe.stdout,'unset|unset|kept',shell);
  }

  const home=join(root,'home'),work=join(root,'work'),downloads=join(home,'Downloads');
  mkdirSync(downloads,{recursive:true});mkdirSync(work);
  const managedClaude=join(root,'managed-claude');mkdirSync(join(managedClaude,'skills'),{recursive:true});
  symlinkSync(managedClaude,join(work,'.claude'));
  const protectedPaths=[downloads,join(home,'Library')];
  const options=claudeFilesystemOptions(work,protectedPaths);
  assert.equal(options.sandbox,undefined);
  assert.equal(options.settings,undefined);
  assert.equal(options.additionalDirectories,undefined);
  assert.equal(JSON.stringify(options).includes(downloads),false);
  assert.equal(options.hooks.PreToolUse[0].timeout,1);
  const hook=options.hooks.PreToolUse[0].hooks[0];
  const denied=await hook({tool_name:'Read',tool_input:{file_path:join(downloads,'optional.txt')}});
  assert.equal(denied.hookSpecificOutput.permissionDecision,'deny');
  const caseVariant=join(home,'downloads','case-variant.txt');
  assert.equal(await claudePathIsProtected(caseVariant,work,protectedPaths),
    ['darwin','win32'].includes(process.platform));
  assert.equal((await hook({tool_name:'Glob',tool_input:{pattern:join(downloads,'**','*')}}))
    .hookSpecificOutput.permissionDecision,'deny');
  assert.deepEqual(await hook({tool_name:'Glob',tool_input:{pattern:'**/*.mjs'}}),{});
  const background=await hook({tool_name:'Bash',tool_input:{command:'sleep 10',run_in_background:true}});
  assert.equal(background.hookSpecificOutput.permissionDecision,'deny');
  assert.match(background.hookSpecificOutput.permissionDecisionReason,/Background Bash/);
  assert.deepEqual(await hook({tool_name:'Read',tool_input:{file_path:join(work,'needed.txt')}}),{});
  assert.equal((await claudeProtectedToolDecision({}, {blockedPath:join(downloads,'from-bash')},
    work,protectedPaths,[],'Bash')).behavior,'deny');
  assert.equal(await claudeProtectedToolDecision({file_path:join(work,'ok')},{},
    work,protectedPaths,[],'Read'),null);

  symlinkSync(downloads,join(work,'download-link'));
  assert.equal(await claudePathIsProtected(
    join(work,'download-link','secret.txt'),work,protectedPaths),true);
  const outside=join(root,'outside');mkdirSync(outside);
  symlinkSync(downloads,join(outside,'download-link'));
  assert.equal(await claudePathIsProtected(
    join(outside,'download-link','missing.txt'),work,protectedPaths),true);
  for(let index=30;index>=0;index--)
    symlinkSync(index===30 ? downloads : join(outside,`chain-${index+1}`),
      join(outside,`chain-${index}`));
  assert.equal(await claudePathIsProtected(
    join(outside,'chain-0','deep.txt'),work,protectedPaths),true);

  const enoent=Object.assign(new Error('missing'),{code:'ENOENT'});
  assert.equal(await claudePathIsProtected(join(outside,'missing'),work,protectedPaths,[],{
    filesystem:{lstat:async()=>{throw enoent;}},timeoutMs:50}),false);
  const deniedIo=Object.assign(new Error('denied'),{code:'EACCES'});
  assert.equal(await claudePathIsProtected(join(outside,'opaque'),work,protectedPaths,[],{
    filesystem:{lstat:async()=>{throw deniedIo;}},timeoutMs:50}),true);
  let timerFired=false;setTimeout(()=>{timerFired=true;},5);
  assert.equal(await claudePathIsProtected(join(outside,'slow'),work,protectedPaths,[],{
    filesystem:{lstat:()=>new Promise(()=>{})},timeoutMs:30}),true);
  assert.equal(timerFired,true,'filesystem timeout must not block the Node event loop');

  const projectWrites=[
    ['Write',join(work,'.claude','settings.json')],
    ['Edit',join(work,'.claude','settings.local.json')],
    ['MultiEdit',join(work,'.claude','skills','fixture','SKILL.md')],
    ['MultiEdit',join(work,'.claude','rules','safety.md')],
    ['NotebookEdit',join(work,'.claude','CLAUDE.md')],
    ['Write',join(work,'CLAUDE.md')],
    ['Write',join(work,'CLAUDE.local.md')],
    ['Edit',join(work,'AGENTS.md')],
    ['Edit',join(root,'CLAUDE.md')],
    ['Edit',join(homedir(),'.claude','CLAUDE.md')],
    ['Write',join(managedClaude,'settings.json')],
  ];
  for(const [tool,file_path] of projectWrites) {
    const tool_input=tool==='NotebookEdit' ? {notebook_path:file_path} : {file_path};
    const decision=await hook({tool_name:tool,tool_input});
    assert.ok(decision.hookSpecificOutput,`${tool}:${file_path}`);
    assert.equal(decision.hookSpecificOutput.permissionDecision,'deny',`${tool}:${file_path}`);
    assert.match(decision.hookSpecificOutput.permissionDecisionReason,/project configuration/);
  }
  assert.deepEqual(await hook({tool_name:'Read',tool_input:{file_path:join(work,'.claude','settings.json')}}),{});
  assert.deepEqual(await hook({tool_name:'Write',tool_input:{file_path:join(work,'src','normal.txt')}}),{});
  if(['darwin','win32'].includes(process.platform)) {
    const caseDecision=await hook({tool_name:'Write',
      tool_input:{file_path:join(work,'.CLAUDE','SETTINGS.JSON')}});
    assert.equal(caseDecision.hookSpecificOutput.permissionDecision,'deny');
  }
  if(process.platform==='win32') {
    const settings=join(work,'.claude','settings.json');
    for(const alias of [`\\\\?\\${settings}`,`\\\\.\\${settings}`,`${settings}::$DATA`,
      '\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\workspace\\.claude\\settings.json',
      '\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\.claude\\settings.json',
      '//?/GLOBALROOT/Device/HarddiskVolume1/workspace/.claude/settings.json']) {
      const decision=await hook({tool_name:'Write',tool_input:{file_path:alias}});
      assert.equal(decision.hookSpecificOutput.permissionDecision,'deny',alias);
    }
  }

  const work2=join(root,'work2'),claude2=join(work2,'.claude'),skills2=join(claude2,'skills'),
    rules2=join(claude2,'rules');
  mkdirSync(skills2,{recursive:true});mkdirSync(rules2);
  const settingsTarget=join(outside,'settings-target.json');fixtureFile(settingsTarget,'{}');
  const claudeTarget=join(outside,'claude-target.md');fixtureFile(claudeTarget,'rules');
  const skillTarget=join(outside,'skill-target.md');fixtureFile(skillTarget,'skill');
  const ruleTarget=join(outside,'rule-target.md');fixtureFile(ruleTarget,'rule');
  const skillDirectory=join(outside,'skill-directory');mkdirSync(skillDirectory);
  symlinkSync(settingsTarget,join(claude2,'settings.json'));
  symlinkSync(claudeTarget,join(claude2,'CLAUDE.md'));
  symlinkSync(skillTarget,join(skills2,'linked.md'));
  symlinkSync(skillDirectory,join(skills2,'linked-directory'));
  symlinkSync(ruleTarget,join(rules2,'linked.md'));
  const internalLinkHook=claudeFilesystemOptions(work2,protectedPaths).hooks.PreToolUse[0].hooks[0];
  for(const target of [settingsTarget,claudeTarget,skillTarget,ruleTarget,
    join(skillDirectory,'new.md')]) {
    const decision=await internalLinkHook({tool_name:'Write',tool_input:{file_path:target}});
    assert.equal(decision.hookSpecificOutput.permissionDecision,'deny',target);
    assert.match(decision.hookSpecificOutput.permissionDecisionReason,/project configuration/);
  }

  const nested=join(downloads,'project');mkdirSync(nested);
  const nestedOptions=claudeFilesystemOptions(nested,protectedPaths);
  assert.equal(nestedOptions.sandbox,undefined);
  assert.equal(nestedOptions.additionalDirectories,undefined);
  const nestedHook=nestedOptions.hooks.PreToolUse[0].hooks[0];
  assert.deepEqual(await nestedHook({tool_name:'Read',tool_input:{path:join(nested,'src')}}),{});
  for(const request of [
    {tool_name:'Read',tool_input:{path:downloads}},
    {tool_name:'Glob',tool_input:{path:downloads,pattern:'**/*'}},
    {tool_name:'Glob',tool_input:{path:nested,pattern:'../*'}},
    {tool_name:'Glob',tool_input:{path:nested,pattern:'{../*,src/*}'}},
    {tool_name:'Read',tool_input:{path:join(downloads,'sibling')}},
  ]) assert.equal((await nestedHook(request)).hookSpecificOutput.permissionDecision,'deny');

  const extra=join(downloads,'registered-repo');mkdirSync(extra);
  const extraOptions=claudeFilesystemOptions(work,protectedPaths,[extra]);
  assert.equal(extraOptions.sandbox,undefined);
  assert.deepEqual(extraOptions.additionalDirectories,[extra]);
  const extraHook=extraOptions.hooks.PreToolUse[0].hooks[0];
  assert.deepEqual(await extraHook({tool_name:'Read',tool_input:{file_path:join(extra,'README.md')}}),{});
  assert.equal((await extraHook({tool_name:'Read',tool_input:{file_path:join(downloads,'other.txt')}}))
    .hookSpecificOutput.permissionDecision,'deny');
} finally {rmSync(root,{recursive:true,force:true});}
console.log(JSON.stringify({ok:true}));
