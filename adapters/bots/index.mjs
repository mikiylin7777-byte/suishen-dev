import {readFileSync,statSync,lstatSync} from 'node:fs';
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

export function normalize(channel,raw) {
  if(channel==='feishu') {
    const message=raw.message;
    if(message.message_type!=='text') throw Error('仅接收文本');
    return {channel,id:message.message_id,identity:raw.sender?.sender_id?.open_id,body:JSON.parse(message.content).text};
  }
  if(channel==='wecom') return {channel,id:raw.body.msgid,identity:raw.body.from?.userid,body:raw.body.text?.content};
  throw Error('未知渠道');
}
export function bodyCommand(value) {
  // Only explicit JSON command envelopes can consume an approval; ordinary messages are inert material.
  try { const parsed=JSON.parse(value.body); if(parsed.command==='approve') return {...value,body:parsed}; } catch {}
  return value;
}
// Async so a slow bridge never blocks the event loop (Feishu long connection must ack within 3s; WeCom shares the process).
export function bridge(config,operation,value) {
  return new Promise((resolve,reject)=>{
    const p=spawn(config.python,[config.bridge,config.root,config.privatePath],{stdio:['pipe','pipe','ignore']});
    let out='',done=false;
    const fail=()=>{if(!done){done=true;reject(Error('本地持久化/身份校验失败'));}};
    const timer=setTimeout(()=>{p.kill('SIGKILL');fail();},15000);
    p.stdout.setEncoding('utf8');
    p.stdout.on('data',d=>{out+=d;if(out.length>1024*1024){p.kill('SIGKILL');fail();}});
    p.on('error',()=>{clearTimeout(timer);fail();});
    p.on('close',code=>{clearTimeout(timer);if(done)return;if(code!==0)return fail();try{done=true;resolve(JSON.parse(out));}catch{done=false;fail();}});
    p.stdin.on('error',()=>{});
    p.stdin.end(JSON.stringify({operation,value}));
  });
}
// Errors are logged without message content or secrets.
export const stderrLog=(channel,what)=>process.stderr.write(`[${channel}] ${what}\n`);
export async function create(channel,cfg,sdk,receive,connection=()=>{},log=stderrLog) {
  const quiet={trace(){},debug(){},info(){},warn(){},error(){}};
  if(channel==='feishu') {
    const client=new sdk.Client({appId:cfg.appId,appSecret:cfg.appSecret,logger:quiet});
    const ws=new sdk.WSClient({appId:cfg.appId,appSecret:cfg.appSecret,logger:quiet});
    // Ack fast, work in background: Feishu re-pushes events not handled within 3s.
    // In-flight/seen message_ids are skipped here; the bridge's persistent inbox dedups across restarts.
    const seen=new Map();
    const handle=async raw=>{
      const id=raw?.message?.message_id;
      if(raw?.message?.message_type!=='text'){log(channel,'忽略非文本消息');return;}
      let value;
      try{value=bodyCommand(normalize(channel,raw));}catch{log(channel,'消息解析失败，已忽略');return;}
      let result;
      try{result=await receive(value);}catch{seen.delete(id);log(channel,'本地接收失败，等待重推');return;}
      if(result?.duplicate)return;
      try{if(result?.reply) await client.im.message.create({params:{receive_id_type:'open_id'},data:{receive_id:value.identity,msg_type:'text',content:JSON.stringify({text:result.reply})}});}
      catch{log(channel,'回复发送失败或结果未知；不自动重试');}
    };
    const dispatcher=new sdk.EventDispatcher({logger:quiet}).register({'im.message.receive_v1':raw=>{
      const id=raw?.message?.message_id;
      if(id){if(seen.has(id)){log(channel,'重复推送已忽略');return;}seen.set(id,Date.now());if(seen.size>1000)seen.delete(seen.keys().next().value);}
      handle(raw).catch(()=>log(channel,'处理异常'));
    }});
    return {start:()=>{connection('connecting');return ws.start({eventDispatcher:dispatcher});},send:(target,text)=>client.im.message.create({params:{receive_id_type:'open_id'},data:{receive_id:target,msg_type:'text',content:JSON.stringify({text})}})};
  }
  if(channel==='wecom') {
    const ws=new sdk.WSClient({botId:cfg.botId,secret:cfg.secret,logger:quiet});
    ws.on('message.text',raw=>Promise.resolve().then(async()=>{
      const value=bodyCommand(normalize(channel,raw));const result=await receive(value);
      if(result?.reply) await ws.sendMessage(value.identity,{msgtype:'markdown',markdown:{content:result.reply}});
    }).catch(()=>log(channel,'处理失败，已忽略')));
    ws.on('authenticated',()=>connection('authenticated'));
    ws.on('disconnected',()=>connection('disconnected'));
    ws.on('reconnecting',()=>connection('reconnecting'));
    ws.on('error',()=>connection('connection_error'));
    return {start:()=>ws.connect(),send:(target,text)=>ws.sendMessage(target,{msgtype:'markdown',markdown:{content:text}})};
  }
  throw Error('未知渠道');
}
export async function loadOfficialSdk(name,moduleRoot) {
  return import(moduleRoot ? pathToFileURL(createRequire(moduleRoot+'/package.json').resolve(name)).href : name);
}
export async function run(configPath,channel) {
  const st=statSync(configPath);
  if(lstatSync(configPath).isSymbolicLink() || (st.mode&0o077)!==0 || st.uid!==process.getuid()) throw Error('设置文件需本人所有且600');
  const config=JSON.parse(readFileSync(configPath,'utf8'));
  if(config.enabled!==true || !Object.keys(config.owners?.[channel]??{}).length) throw Error('EXTERNAL_REQUIRED：本人尚未绑定');
  config.privatePath=configPath;
  const sdkName=channel==='feishu'?'@larksuiteoapi/node-sdk':'@wecom/aibot-node-sdk';
  const sdk=await loadOfficialSdk(sdkName,config.moduleRoot);
  const adapter=await create(channel,config[channel],sdk,value=>bridge(config,'event',value),status=>{bridge(config,'connection',{channel,status}).catch(()=>{});});
  await adapter.start();
  // SDKs handle transport reconnects; persistent inbox IDs handle callback redelivery.
  // Local durable events are reconciled on every poll, including after reconnect/reboot.
  let polling=false;
  const poll=async()=>{
    if(polling)return;
    polling=true;
    try {
      const pending=await bridge(config,'pending',{channel});
      for(const value of pending) {
        await bridge(config,'notification',value); // persistent uncertainty BEFORE network
        await adapter.send(value.target,value.text);
        await bridge(config,'notification',{...value,phase:'ack'});
      }
    } catch {} finally {polling=false;}
  };
  const timer=setInterval(poll,5000);
  await poll();
  // Explicit local stdin notifications; no greeting or arbitrary inbox-triggered send.
  let pending='';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data',async data=>{
    pending+=data;
    while(pending.includes('\n')) {
      const index=pending.indexOf('\n');const line=pending.slice(0,index);pending=pending.slice(index+1);
      try {
        const value={...JSON.parse(line),channel};
        await bridge(config,'notification',value);
        await adapter.send(value.target,value.text);
        await bridge(config,'notification',{...value,phase:'ack'});
      } catch { process.stderr.write('通知未发送或结果未知；禁止自动重试\n'); }
    }
  });
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  (process.argv[3]==='both' ? Promise.all(['feishu','wecom'].filter(c=>Object.keys(JSON.parse(readFileSync(process.argv[2],'utf8')).owners?.[c]??{}).length).map(c=>run(process.argv[2],c))) : run(process.argv[2],process.argv[3])).catch(()=>{process.stderr.write('BLOCKED：私密配置、SDK或连接不可用；未输出敏感详情\n');process.exit(2);});
}
