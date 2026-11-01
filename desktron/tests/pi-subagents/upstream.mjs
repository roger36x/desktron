import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const sdk=await import(pathToFileURL(join(process.env.PI_SDK,'dist/index.js')));
const services=await sdk.createAgentSessionServices({cwd:process.cwd(),settingsManager:sdk.SettingsManager.inMemory({}),
 resourceLoaderOptions:{noExtensions:true,noSkills:true,noContextFiles:true,
 additionalExtensionPaths:[process.env.PROVIDER,process.env.WRAPPER]}});
assert.deepEqual(services.resourceLoader.getExtensions().errors,[]);
const model=services.modelRuntime.getModels().find(x=>x.provider==='example-gw'&&(!process.env.MODEL_ID||x.id===process.env.MODEL_ID));
assert.ok(model,'requested example-gw model is available');
const manager=sdk.SessionManager.inMemory(process.cwd());
manager.appendMessage({role:'user',content:'upstream-parent-context',timestamp:Date.now()});
const {session}=await sdk.createAgentSessionFromServices({services,model,thinkingLevel:'off',sessionManager:manager});
const errors=[];
try{
 await session.bindExtensions({mode:'print',onError:e=>errors.push(String(e.error))});
 const tool=session.extensionRunner.getToolDefinition('subagent');
 const ctx=session.extensionRunner.createContext();
 if(process.env.STALE_REGISTRY==='1'){
  const get=ctx.modelRegistry.getAvailable.bind(ctx.modelRegistry);
  const refresh=ctx.modelRegistry.refresh.bind(ctx.modelRegistry);
  let ready=false;
  ctx.modelRegistry.getAvailable=()=>ready?get():[];
  ctx.modelRegistry.refresh=async(options)=>{await refresh(options);ready=true;};
 }
 const params=JSON.parse(process.env.PARAMS);
 const results=[];
 for(let i=0;i<Number(process.env.COUNT || 1);i++){
  const controller=new AbortController();
  const cancel=process.env.CANCEL==='1'?setTimeout(()=>controller.abort(),1200):undefined;
  let result;
  if(process.env.MODEL_DRIVEN==='1'){
   await session.prompt('Delegate the fixture task now. '+i);
   result=session.messages.filter(m=>m.role==='toolResult'&&m.toolName==='subagent').at(-1);
   assert.ok(result);
  }else{
   try {result=await tool.execute('native-fixture-'+i,params,controller.signal,()=>{},ctx);}
   catch(error){if(!controller.signal.aborted)throw error;result={isError:true,details:{cancelled:true}};}
   if(process.env.DUPLICATE==='1'){
    const replay=await tool.execute('native-fixture-'+i,params,controller.signal,()=>{},ctx);
    assert.deepEqual(replay,result);
    await assert.rejects(tool.execute('native-fixture-'+i,{...params,task:'different'},controller.signal,()=>{},ctx));
   }
  }
  clearTimeout(cancel);
  if(!process.env.EXPECT_ERROR)assert.notEqual(result.isError,true,JSON.stringify(result));
  else assert.equal(result.isError,true,JSON.stringify(result));
  results.push(result.details);
 }
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({results}));
}finally{await session.dispose();}
process.exit(0);
