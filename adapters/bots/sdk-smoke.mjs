import test from 'node:test';
import assert from 'node:assert/strict';
import * as lark from '@larksuiteoapi/node-sdk';
import * as wecom from '@wecom/aibot-node-sdk';
import {create,loadOfficialSdk} from './index.mjs';
test('实际飞书SDK加载及适配构造，不连接不发送',async()=>{
 assert.equal(typeof lark.WSClient,'function');assert.equal(typeof lark.Client,'function');
 const adapter=await create('feishu',{appId:'synthetic-app',appSecret:'x'},lark,()=>{});
 assert.equal(typeof adapter.start,'function');assert.equal(typeof adapter.send,'function');
});
test('实际企微SDK加载及适配构造，不连接不发送',async()=>{
 assert.equal(typeof wecom.WSClient,'function');
 const adapter=await create('wecom',{botId:'synthetic-bot',secret:'x'},wecom,()=>{});
 assert.equal(typeof adapter.start,'function');assert.equal(typeof adapter.send,'function');
});

test('实际generation路径加载官方双SDK，不连接不发送',async()=>{
 const {fileURLToPath}=await import('node:url');
 const root=fileURLToPath(new URL('.',import.meta.url));
 assert.equal(typeof (await loadOfficialSdk('@larksuiteoapi/node-sdk',root)).WSClient,'function');
 assert.equal(typeof (await loadOfficialSdk('@wecom/aibot-node-sdk',root)).WSClient,'function');
});
