import test from 'node:test';
import assert from 'node:assert/strict';
import {create,normalize,bodyCommand,bridge} from './index.mjs';
test('官方事件形状归一化，无普通文本自动批准',()=>{
 assert.equal(normalize('feishu',{message:{message_type:'text',message_id:'m',content:'{"text":"hi"}'},sender:{sender_id:{open_id:'u'}}}).identity,'u');
 assert.equal(normalize('wecom',{body:{msgid:'m',from:{userid:'u'},text:{content:'hi'}}}).identity,'u');
 assert.equal(bodyCommand({body:'approve this'}).body,'approve this');
 assert.equal(bodyCommand({body:'{"command":"approve"}'}).body.command,'approve');
});
test('飞书适配长连接及发送参数，使用注入SDK无网络',async()=>{
 let received,dispatch,request;
 class Client {im={message:{create:async p=>{request=p;}}};}
 class WSClient {start(p){dispatch=p.eventDispatcher;}}
 class EventDispatcher {register(p){this.handlers=p;return this;}}
 const adapter=await create('feishu',{}, {Client,WSClient,EventDispatcher},v=>{received=v;});
 adapter.start();await dispatch.handlers['im.message.receive_v1']({message:{message_type:'text',message_id:'m',content:'{"text":"hi"}'},sender:{sender_id:{open_id:'u'}}});
 assert.equal(received.id,'m');await adapter.send('u','done');assert.equal(request.data.receive_id,'u');
});
test('企微官方事件和主动发送适配，无网络',async()=>{
 let events={},sent;
 class WSClient {on(k,cb){events[k]=cb;}connect(){}sendMessage(...v){sent=v;}}
 const adapter=await create('wecom',{}, {WSClient},()=>{});adapter.start();await adapter.send('u','done');
 assert.ok(events['message.text']);assert.equal(sent[1].msgtype,'markdown');
});
test('未知渠道和非文本事件失败关闭',()=>{
 assert.throws(()=>normalize('feishu',{message:{message_type:'image'}}));
 assert.throws(()=>normalize('unknown',{}));
});
test('企微回调先等待持久接收再回复，拒绝接收不回复',async()=>{
 let events={},sent=0,resolve;
 class WSClient {on(k,cb){events[k]=cb;}connect(){}async sendMessage(){sent++;}}
 const adapter=await create('wecom',{}, {WSClient},()=>new Promise(r=>{resolve=r;}));
 adapter.start();
 events['message.text']({body:{msgid:'m',from:{userid:'u'},text:{content:'hi'}}});
 await new Promise(r=>setImmediate(r));assert.equal(sent,0);
 resolve({reply:'durable'});await new Promise(r=>setImmediate(r));assert.equal(sent,1);
});

const feishuSdk=sent=>({Client:class{im={message:{create:async p=>{sent.push(p);}}};},WSClient:class{start(p){this.d=p.eventDispatcher;feishuSdk.d=p.eventDispatcher;}},EventDispatcher:class{register(p){this.handlers=p;return this;}}});
const fsMsg=(id,type='text')=>({message:{message_type:type,message_id:id,content:'{"text":"hi"}'},sender:{sender_id:{open_id:'u'}}});
const tick=()=>new Promise(r=>setImmediate(r));
test('飞书先快速确认；3 秒后同一 message_id 重推只回复一次',async()=>{
 const sent=[],logs=[];let calls=0,release;
 const adapter=await create('feishu',{},feishuSdk(sent),()=>{calls++;return new Promise(r=>{release=r;});},()=>{},(c,w)=>logs.push(w));
 adapter.start();const h=feishuSdk.d.handlers['im.message.receive_v1'];
 const t0=Date.now();await h(fsMsg('m1'));assert.ok(Date.now()-t0<100,'处理函数要立刻返回');
 await new Promise(r=>setTimeout(r,3000));
 await h(fsMsg('m1'));
 release({reply:'ok'});await tick();await tick();
 await h(fsMsg('m1'));await tick();
 assert.equal(calls,1);assert.equal(sent.length,1);assert.ok(logs.includes('重复推送已忽略'));
});
test('飞书桥慢时，企微消息照常处理',async()=>{
 const fsent=[];let wsent=0,events={};
 const fs=await create('feishu',{},feishuSdk(fsent),()=>new Promise(()=>{}),()=>{},()=>{});fs.start();
 class WSClient{on(k,cb){events[k]=cb;}connect(){}async sendMessage(){wsent++;}}
 const wc=await create('wecom',{},{WSClient},async()=>({reply:'ok'}),()=>{},()=>{});wc.start();
 await feishuSdk.d.handlers['im.message.receive_v1'](fsMsg('slow'));
 events['message.text']({body:{msgid:'w',from:{userid:'u'},text:{content:'hi'}}});
 await tick();await tick();assert.equal(wsent,1);assert.equal(fsent.length,0);
});
test('真实子进程桥慢 2 秒不卡事件循环',async()=>{
 const {writeFileSync,mkdtempSync}=await import('node:fs');const {tmpdir}=await import('node:os');
 const dir=mkdtempSync(tmpdir()+'/bots-');writeFileSync(dir+'/slow.mjs','setTimeout(()=>process.stdout.write(JSON.stringify({ok:1})),2000)');
 let ticks=0;const iv=setInterval(()=>ticks++,100);
 const r=await bridge({python:process.execPath,bridge:dir+'/slow.mjs',root:'r',privatePath:'p'},'event',{});
 clearInterval(iv);assert.equal(r.ok,1);assert.ok(ticks>=10,'等待期间事件循环仍在跑：'+ticks);
});
test('非文本消息和桥失败都不崩，并记日志；失败后重推可再试',async()=>{
 const sent=[],logs=[];let n=0;
 const adapter=await create('feishu',{},feishuSdk(sent),async()=>{if(n++===0)throw Error('x');return {reply:'ok'};},()=>{},(c,w)=>logs.push(w));
 adapter.start();const h=feishuSdk.d.handlers['im.message.receive_v1'];
 await h(fsMsg('i','image'));await h({});await h(fsMsg('f'));await tick();await tick();
 assert.ok(logs.includes('忽略非文本消息'));assert.ok(logs.includes('本地接收失败，等待重推'));assert.equal(sent.length,0);
 await h(fsMsg('f'));await tick();await tick();assert.equal(sent.length,1);
});
test('真实子进程桥失败返回拒绝而不是抛到进程',async()=>{
 await assert.rejects(bridge({python:process.execPath,bridge:'/nonexistent.mjs',root:'r',privatePath:'p'},'event',{}));
 await assert.rejects(bridge({python:'/nonexistent-bin',bridge:'x',root:'r',privatePath:'p'},'event',{}));
});
