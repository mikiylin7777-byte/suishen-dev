import argparse
import json
from pathlib import Path
import sys
import sqlite3
import subprocess
import time
from .core import Blocked, Store, canonical, digest, PROTOCOL
from . import migration as m
from . import tools
from .team import Team, transport

def data(path):
    return json.load(sys.stdin) if path=='-' else json.loads(Path(path).read_text())

def main(argv=None):
    p=argparse.ArgumentParser(description='随身开发：知识、检查点与安全携带；战队唯一执行')
    p.add_argument('--root',default=str(Path.cwd()))
    sub=p.add_subparsers(dest='cmd',required=True)
    for name in ('doctor','finalize-migration','probe','interview','migration-inventory','projects','menu','setup','prepare-update'):
        sub.add_parser(name)
    a=sub.add_parser('profile');a.add_argument('json')
    a=sub.add_parser('revoke-preference');a.add_argument('key')
    a=sub.add_parser('register');a.add_argument('name');a.add_argument('path');a.add_argument('--id')
    a=sub.add_parser('discover');a.add_argument('links');a.add_argument('registry')
    for name in ('checkpoint','material'):
        a=sub.add_parser(name);a.add_argument('json')
    a=sub.add_parser('archive');a.add_argument('source');a.add_argument('summary_file')
    a=sub.add_parser('overview');a.add_argument('expected',nargs='?');a.add_argument('content_file',nargs='?');a.add_argument('--sync',action='store_true')
    a=sub.add_parser('reply');a.add_argument('action',choices=('expect','read','list'));a.add_argument('--thread');a.add_argument('--request');a.add_argument('--project',default='');a.add_argument('--asked');a.add_argument('--summary')
    a=sub.add_parser('wait-check');a.add_argument('json')
    a=sub.add_parser('settings');a.add_argument('action',choices=('show','set','init'));a.add_argument('key',nargs='?');a.add_argument('value',nargs='?',help='JSON 值；文字可直接写')
    a=sub.add_parser('registry-apply');a.add_argument('json',nargs='?');a.add_argument('--remove',action='append',default=[])
    a=sub.add_parser('enroll-medium');a.add_argument('path')
    a=sub.add_parser('snapshot');a.add_argument('source');a.add_argument('medium');a.add_argument('--project',default='fixture')
    a=sub.add_parser('validate-snapshot');a.add_argument('path')
    a=sub.add_parser('restore');a.add_argument('snapshot');a.add_argument('target')
    a=sub.add_parser('changed');a.add_argument('project')
    a=sub.add_parser('watch');a.add_argument('project');a.add_argument('medium')
    a=sub.add_parser('sync');a.add_argument('project');a.add_argument('medium');a.add_argument('--force',action='store_true')
    a=sub.add_parser('reconcile');a.add_argument('base');a.add_argument('left');a.add_argument('right');a.add_argument('output')
    a=sub.add_parser('configure-tools');a.add_argument('python');a.add_argument('server')
    a=sub.add_parser('queue');a.add_argument('intent');a.add_argument('mvp');a.add_argument('--verification-review')
    a=sub.add_parser('review-harness');a.add_argument('path');a.add_argument('--confirm-readonly',action='store_true')
    a=sub.add_parser('review-verification');a.add_argument('mvp');a.add_argument('--commands','--verification-commands',dest='commands',help='JSON文件中的verification_commands，或MVP内同名字段；仅本地工程师显式审阅入口')
    a=sub.add_parser('submit');a.add_argument('key');a.add_argument('workspace')
    a=sub.add_parser('team');a.add_argument('action');a.add_argument('task');a.add_argument('--instruction-id')
    sub.add_parser('team-capabilities')
    a=sub.add_parser('lifecycle');a.add_argument('operation');a.add_argument('json');a.add_argument('--instruction-id')
    a=sub.add_parser('rejection-evidence');a.add_argument('instruction');a.add_argument('audit_sha256')
    a=sub.add_parser('review-rejection');a.add_argument('instruction');a.add_argument('json')
    a=sub.add_parser('new-instruction');a.add_argument('operation');a.add_argument('json')
    a=sub.add_parser('approval');a.add_argument('binding')
    a=sub.add_parser('consume-approval');a.add_argument('id');a.add_argument('binding');a.add_argument('owner')
    a=sub.add_parser('bot-inbox');a.add_argument('json')
    a=sub.add_parser('encrypt');a.add_argument('source');a.add_argument('target');a.add_argument('recipient',nargs='?')
    a=sub.add_parser('decrypt-sensitive');a.add_argument('source');a.add_argument('target')
    a=sub.add_parser('select-project');a.add_argument('id')
    a=sub.add_parser('bind-new-tasks');a.add_argument('project')
    a=sub.add_parser('unbind-new-tasks');a.add_argument('project')
    a=sub.add_parser('open-project');a.add_argument('id')
    a=sub.add_parser('update-preview');a.add_argument('candidate')
    a=sub.add_parser('update-apply');a.add_argument('candidate');a.add_argument('backup')
    a=sub.add_parser('backup');a.add_argument('destination');a.add_argument('--kind',choices=['daily','weekly','upgrade'],default='daily')
    a=sub.add_parser('recover-material');a.add_argument('id')
    a=sub.add_parser('remove-material');a.add_argument('id')
    from . import automation, supervisor, backup_git, bots, onboarding, workflows, knowledge, session_adapters, context_server, portability, git_portable, db_adapters, planner, sensitive, transfer, candidates, component_cli, legacy_cli, move_in, assist, wizard, leave, cost, upgrade_channel, t3_setup, taskline_handoff, ci, review
    direct_modules=(automation,supervisor,backup_git,bots,portability,git_portable,db_adapters,planner,sensitive,transfer,candidates,component_cli,legacy_cli,move_in,assist,wizard,leave,cost,upgrade_channel,t3_setup,taskline_handoff,ci,review)
    store_modules={'onboarding':onboarding,'workflow':workflows,'knowledge':knowledge,'sessions':session_adapters,'context':context_server}
    for module in (*direct_modules,*store_modules.values()):module.register(sub)
    args=p.parse_args(argv)
    try:
        direct=next((getattr(args,key) for key in ('automation_run','runner_run','backup_git_run','bots_run','portability_run','git_portable_run','database_run','planner_run','sensitive_run','transfer_run','candidates_run','components_run','legacy_run','move_in_run','assist_run','wizard_run','leave_run','cost_run','upgrade_run','t3_setup_run','taskline_run','ci_run','review_run') if hasattr(args,key)),None)
        if direct:
            result=direct(args)
            if args.cmd=='t3-setup' and result is None:return 0
        elif args.cmd in store_modules:
            from .project_binding import ReadOnlyStore
            store=ReadOnlyStore(args.root) if args.cmd=='workflow' and args.workflow_action=='binding' else Store(args.root)
            try:result=store_modules[args.cmd].run(store,args)
            finally:store.db.close()
            if args.cmd=='context' and result is None:return 0
        elif args.cmd=='restore': result=m.restore(args.snapshot,args.target)
        elif args.cmd=='validate-snapshot': result=m.verify(args.path)
        elif args.cmd=='reconcile': result=m.reconcile(args.base,args.left,args.right,args.output)
        elif args.cmd=='encrypt': result=tools.encrypt(args.source,args.target,args.recipient)
        elif args.cmd=='decrypt-sensitive':result=tools.decrypt_sensitive(args.source,args.target)
        elif args.cmd=='probe': result=tools.probe()
        else:
            s=Store(args.root)
            c=args.cmd
            if c=='doctor':result=s.doctor()
            elif c=='prepare-update':
                from .distribution import prepare_update
                result=prepare_update(s)
            elif c=='migration-inventory':result=tools.migration_inventory(s)
            elif c=='interview':
                result={'required':['user','goals','projects','workflow','tools','backup','notifications','sensitive_rules_confirmed'],'defaults':PROTOCOL['defaults'],'instructions':'填写 JSON 后 profile 导入；偏好可撤回；敏感规则必须本人确认，凭据不要写入此文件'}
            elif c in ('menu','setup','open-project'):
                from .console import menu,setup,open_project
                result=menu(s) if c=='menu' else setup(s) if c=='setup' else open_project(s,args.id)
            elif c=='projects':result=tools.project_choices(s)
            elif c=='unbind-new-tasks':
                from .team import revoke_new_tasks
                result=revoke_new_tasks(s,args.project)
            elif c=='bind-new-tasks':
                from .team import bind_new_tasks
                result=bind_new_tasks(s,transport(s.get('team_config',{})),args.project)
            elif c=='select-project':result=tools.project_choices(s,args.id)
            elif c=='profile':result=s.profile(data(args.json))
            elif c=='revoke-preference':
                value=s.get('profile',{});value.pop(args.key,None);s.put('profile',value);result={'revoked':args.key}
            elif c=='register':
                from .settings import keep_local_registrations
                result={'id':s.register(args.name,args.path,args.id)}
                added=keep_local_registrations(s)
                if added:result['已写进本机设置']=added
            elif c=='discover':result=tools.discover(s,args.links,args.registry)
            elif c in ('checkpoint','material'):result={'id':s.record(c,data(args.json))}
            elif c=='archive':result={'path':s.archive(args.source,Path(args.summary_file).read_text())}
            elif c=='reply':
                from .board import expect_reply,record_reply,open_replies
                if args.action=='expect':result=expect_reply(s,args.thread,args.request,args.project,args.asked)
                elif args.action=='read':result=record_reply(s,args.request,args.summary)
                else:result={'等回复':open_replies(s)}
            elif c=='wait-check':
                from .board import save_waits
                result=save_waits(s,data(args.json))
            elif c=='overview' and args.expected is None:
                from .board import overview as board_overview
                result=board_overview(s,args.sync)
                if sys.stdout.isatty():print(result['page'])
            elif c=='overview':result={'sha256':s.overview(args.expected,Path(args.content_file).read_text())}
            elif c=='settings':
                from . import settings as st
                if args.action=='show':result=st.load(s)
                elif args.action=='init':result=st.init_from_registry(s)
                else:
                    if not args.key or args.value is None:raise Blocked('用法：settings set <项> <值>')
                    try:value=json.loads(args.value)
                    except ValueError:value=args.value
                    result=st.set_value(s,args.key,value)
            elif c=='registry-apply':
                from .board import apply as registry_apply
                result=registry_apply(s,data(args.json) if args.json else None,args.remove)
            elif c=='enroll-medium':result={'id':m.enroll(s,args.path)}
            elif c=='snapshot':result={'snapshot':m.snapshot(s,args.source,args.medium,args.project)}
            elif c=='changed':
                with s.db:s.db.execute('INSERT OR REPLACE INTO changes VALUES (?,?)',(args.project,time.time()))
                result={'aggregating_seconds':30}
            elif c=='watch':result=m.watch(s,args.project,args.medium)
            elif c=='sync':result=m.sync(s,args.project,args.medium,args.force)
            elif c=='finalize-migration':result=m.finalize(s)
            elif c=='configure-tools':result=tools.configs(s,args.python,args.server)
            elif c=='review-harness':
                from .verification import register_harness
                result=register_harness(s,args.path,args.confirm_readonly)
            elif c=='review-verification':
                from .verification import review
                values=data(args.commands)['verification_commands'] if args.commands else None
                result={'verification_review':review(s,data(args.mvp),values)}
            elif c=='queue':
                key=s.queue(args.intent,data(args.mvp),args.verification_review)
                result={'id':key,'state':s.db.execute('SELECT state FROM outbox WHERE id=?',(key,)).fetchone()[0]}
            elif c=='team-capabilities':result=transport(s.get('team_config',{}))('__capabilities__',{})
            elif c=='rejection-evidence':result=Team(s,transport(s.get('team_config',{}))).rejection_evidence(args.instruction,args.audit_sha256)
            elif c=='review-rejection':result=Team(s,transport(s.get('team_config',{}))).review_rejection(args.instruction,data(args.json))
            elif c=='new-instruction':result=Team(s,transport(s.get('team_config',{}))).new_instruction(args.operation,data(args.json))
            elif c=='lifecycle':result=Team(s,transport(s.get('team_config',{}))).lifecycle(args.operation,data(args.json),args.instruction_id)
            elif c=='submit':result=Team(s,transport(s.get('team_config',{}))).submit(args.key,args.workspace)
            elif c=='team':result=Team(s,transport(s.get('team_config',{}))).action(args.action,args.task,args.instruction_id)
            elif c=='approval':result={'id':bots.issue_approval(s,data(args.binding))}
            elif c=='consume-approval':result={'consumed':s.consume(args.id,data(args.binding),args.owner)}
            elif c=='bot-inbox':
                value=data(args.json);cfg=s.get('bot_allowlist',{})
                result=s.receive(value['channel'],value['id'],value['owner'],cfg.get(value['channel'],[]),value['body'])
            elif c.startswith('update-'):
                from .distribution import update
                result=update(s,args.candidate,getattr(args,'backup',None))
            elif c=='backup':
                from .distribution import backup
                result=backup(s,args.destination,args.kind)
            elif c in ('recover-material','remove-material'):
                from .distribution import material_move
                result=material_move(s,args.id,c=='recover-material')
            else:raise Blocked('未实现的命令')
        print(canonical(result))
        if args.cmd=='taskline' and (result.get('phase') in ('BLOCKED','ARCHIVE_PENDING') or result.get('status') == 'BLOCKED' or result.get('archive_status') == 'BLOCKED'):return 2
        return 0
    except (transfer.sealed_bundle.BundleError,Blocked,OSError,ValueError,KeyError,sqlite3.Error,subprocess.SubprocessError) as e:
        print(canonical({'status':'BLOCKED','reason':str(e)}),file=sys.stderr);return 2

if __name__=='__main__':
    sys.exit(main())
