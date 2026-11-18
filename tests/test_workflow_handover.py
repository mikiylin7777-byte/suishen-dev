import unittest
from portable.core import PROTOCOL


from pathlib import Path

def _private_words():
    """本机不想发出去的词：仓库里有 tools/ci/privacy_markers.txt 就读它（见 DECISIONS 2026-11-17），公开版里它是示例名单。"""
    f=Path(__file__).resolve().parents[1]/'tools/ci/privacy_markers.txt'
    return [l.strip() for l in f.read_text(encoding='utf-8').splitlines() if l.strip() and not l.strip().startswith('#')] if f.is_file() else []

class WorkflowHandoverTest(unittest.TestCase):
    def test_public_rules_and_version(self):
        self.assertGreaterEqual(tuple(map(int,PROTOCOL['version'].split('.'))),(0,2,24))  # 0.2.24 起有这三条
        rules=PROTOCOL['public_rules']
        self.assertIn('还没记的',next(r for r in rules if r.startswith('【收工必写】')))
        handoff=next(r for r in rules if r.startswith('【续接】'))  # 0.2.43：【交接包】并入【续接】
        for words in ('近期动态','请求编号','数据/交接/','不放系统临时目录','不携带密钥','重新挂上监视','【交接包·战队】'):self.assertIn(words,handoff)
        self.assertFalse(any(r.startswith('【交接包】') for r in rules))
        self.assertLessEqual(len(rules),30)  # 0.2.43 瘦身：43 条并成 27 条，别再涨回去
        # 0.2.37：【换人不换法】并入【开工先读】、【越做越聪明】并入【收工必写】，原文约束保留。
        self.assertIn('不凭个人习惯改',next(r for r in rules if r.startswith('【开工先读】')))
        self.assertIn('别的 AI 读不到',next(r for r in rules if r.startswith('【收工必写】')))
        self.assertIn('别的 AI 读不到',next(r for r in rules if r.startswith('【唯一来源】')))
        self.assertFalse(any(r.startswith(('【换人不换法】','【越做越聪明】')) for r in rules))
        self.assertIn('docs/工作法.md',next(r for r in rules if r.startswith('【每个仓库的 AGENTS.md】')))

    def test_deploy_windows_only_for_projects_in_formal_use(self):
        import tempfile
        from pathlib import Path
        from portable.distribution import build
        with tempfile.TemporaryDirectory() as d:
            app=Path(d).resolve()/'release';build(Path(__file__).resolve().parents[1],app)
            text=(app/'AGENTS.md').read_text()
            self.assertIn('已宣布「正式使用」的项目，才避开',text);self.assertIn('直接手动触发验证、当场读回',text)


class CiEvidenceRuleTest(unittest.TestCase):
    def test_ci_evidence_and_auto_merge_rules(self):
        import json
        from pathlib import Path
        r=json.loads((Path(__file__).resolve().parents[1]/'portable/protocol.json').read_text(encoding='utf-8'))['public_rules']
        done=[x for x in r if x.startswith('【退出码不等于完成】')][0]
        flow=[x for x in r if x.startswith('【标准套路】')][0]
        self.assertIn('CI 结果链接',done);self.assertIn('全量测试',done);self.assertIn('仍要本地全量跑',done)
        self.assertIn('CI 通过后自动合并',flow);self.assertIn('高风险没点头不开',flow)
        # 0.2.43：每项 success、有执行记录、非跳过；不看耗时；CI 没触发不空等；需求稿要定稿
        for words in ('有执行记录','不是跳过','不看耗时','CI 没触发','把目标分支合进来','不借别的窗口或服务器上的令牌','CI 只查测试和密钥扫描'):self.assertIn(words,flow)
        risk=[x for x in r if x.startswith('【按风险分级】')][0]
        for words in ('需求稿和高风险的方案稿除外','docs/ 下文档的 PR','CI 绿即可开自动合并','按块合','整件做完','从 development 合进 main 按高风险','--approval','合进 main 不等于上线'):self.assertIn(words,risk)
        # 0.2.42：自动合并走 ci merge --wait，不用平台自带的（它把全部跳过当成通过）
        self.assertIn('ci merge <项目> <PR号> --confirm --risk 低|高 --wait',flow);self.assertIn('没跑完、没有结果都不算',flow)
        self.assertIn('不用平台自带的自动合并',flow)

    def test_docs_auto_merge_and_standing_deploy(self):
        import json
        from pathlib import Path
        r=json.loads((Path(__file__).resolve().parents[1]/'portable/protocol.json').read_text(encoding='utf-8'))['public_rules']
        flow=[x for x in r if x.startswith('【标准套路】')][0]
        # 0.2.37：【长期部署授权】【上线放行】【联络表】挪进本机附加规则，公共协议不再出现本机专属的角色和工具。
        self.assertFalse(any(x.startswith(('【长期部署授权】','【上线放行】','【联络表】')) for x in r))
        text=''.join(r)
        for bad in ('北京','ECS','CN-Servers','门禁','部署禁区','codex-task','OWNER',*_private_words()):self.assertNotIn(bad,text)
        self.assertIn('有总管窗口时经总管放行',flow);self.assertIn('只有一个 AI 时用户说「上线」才上线',flow)


class KeepGoingRuleTest(unittest.TestCase):
    def test_keep_going_rule(self):
        import json
        from pathlib import Path
        r=json.loads((Path(__file__).resolve().parents[1]/'portable/protocol.json').read_text(encoding='utf-8'))['public_rules']
        rule=[x for x in r if x.startswith('【一口气做完】')][0]
        self.assertIn('不问「要不要继续」',rule);self.assertIn('同一回合里动手做掉',rule);self.assertIn('【四件事要点头】',rule)
        # 方案《做到一半自动接着做》：停下前写清在等什么，只加在末尾（见 DECISIONS 2026-11-18）
        self.assertTrue(rule.endswith('还有没做完的块时，停之前把『在等谁、等什么、大概多久、等到后怎么接上』写进 STATUS.md 的「卡在」「下一步」，回复最后一句也写这四样。'))


class WaitWakeRuleTest(unittest.TestCase):
    def test_long_tasks_run_in_background_with_scheduled_fallback(self):
        rule=next(r for r in PROTOCOL['public_rules'] if r.startswith('【等不无限】'))
        self.assertIn('放后台运行',rule);self.assertIn('兜底检查',rule);self.assertIn('两样都没有时写明未接通',rule)
        self.assertIn('宿主还没交出的真实权限、原子结束和持久唤醒不写成已经接通',rule)
        # 审核 11-14：瘦身时删掉的执行前提补回
        for words in ('先核实有没有回传','未结束就再约一次'):self.assertIn(words,rule)
        # 被后台完成通知叫醒的那一轮不许空着结束（见 DECISIONS 2026-11-18）
        self.assertTrue(rule.endswith('宿主还没交出的真实权限、原子结束和持久唤醒不写成已经接通。被后台完成通知叫醒时，这一轮必须先读结果、验收，再做下一步或写清为什么停；不许不回话就结束这一轮。'))
        handoff=next(r for r in PROTOCOL['public_rules'] if r.startswith('【续接】'))
        for words in ('原任务及运行回传已接上','仅回复已接手不算确认','保留可恢复记录','留后继链接'):self.assertIn(words,handoff)
        helper=next(r for r in PROTOCOL['public_rules'] if r.startswith('【帮手命令】'))
        for words in ('用新版程序跑 upgrade apply','让用户勾选后 adopt','--show-questions'):self.assertIn(words,helper)
        self.assertIn('当场写进',next(r for r in PROTOCOL['public_rules'] if r.startswith('【收工必写】')))


class RetentionRuleTest(unittest.TestCase):
    def test_only_project_things_are_move_only_and_backups_expire(self):
        # 见 DECISIONS 2026-11-14：只移不删只管项目里的东西；程序生成的备份按保留期删
        rules=PROTOCOL['public_rules']
        keep=next(r for r in rules if r.startswith('【只移不删】'))
        for words in ('清理项目里的东西','~/归档/<年-月>/','不永久删除','按保留期直接删','不进归档','放满 30 天','用户点头后统一删除'):self.assertIn(words,keep)
        back=next(r for r in rules if r.startswith('【后台】'))
        for words in ('在线仓库里没有的东西','每周备一次各项目完整的 Git 历史','最近 7 天、4 周、2 次升级','更早的自动删除'):self.assertIn(words,back)

