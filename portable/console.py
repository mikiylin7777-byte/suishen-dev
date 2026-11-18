import platform
import subprocess
import sys
from .core import Blocked,PROTOCOL,canonical
from .tools import project_choices

def setup(store):
    if not sys.stdin.isatty():raise Blocked('首次访谈需要本人交互终端；无人值守可先用interview模板')
    prompts={'user':'你希望如何称呼你','goals':'你要解决什么问题，最希望省掉什么工作','projects':'本轮关注哪些项目（逗号分隔，不搬动原仓库）','workflow':'描述核心工作流程、首版范围和验收标准','tools':'常用工具（自行选择Codex、Claude、T3或通用MCP）','backup':'包外备份目录与携带习惯（不填写密码或凭据）','notifications':'通知偏好（默认不启用；机器人可选，未绑定不启用）'}
    value={}
    for key,prompt in prompts.items():
        value[key]=input(prompt+'：').strip()
        if not value[key]:raise Blocked('访谈字段不能为空，可稍后继续')
    print('敏感规则：不自动修改生产数据、凭据、权限或发送外部消息；迁移未知文件阻断；不永久删除。')
    if input('确认以上敏感规则请输入 yes：').strip().lower()!='yes':raise Blocked('敏感规则尚未本人确认，未启用执行')
    value['sensitive_rules_confirmed']=True
    store.profile(value)
    return {'profile_saved':True,'execution_enabled':False,'preferences_revocable':True}

def open_project(store,project):
    value=project_choices(store,project)['selected']
    if platform.system()!='Darwin':return {'path':value['path'],'opened':False,'reason':'此系统请在文件管理器打开所示目录'}
    result=subprocess.run(['open',value['path']],capture_output=True)
    if result.returncode:raise Blocked('无法打开项目目录，原项目未修改')
    return {'path':value['path'],'opened':True,'execution_enabled':False}

def menu(store):
    from .onboarding import menu as onboarding_menu
    if not sys.stdin.isatty():return {**project_choices(store),**onboarding_menu(store)}
    return onboarding_menu(store)
