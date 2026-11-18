"""Private structured stdio boundary, executed with the configured SDK interpreter."""
import asyncio
import contextlib
import json
import sys
from .core import canonical
from .team import mcp_call,RejectedBeforeExecution,rejection_from

def main():
    try:
        request=json.load(sys.stdin)
        config=request['config']
        args=config.get('server_args') or ['-I','-B',config['server'],'--transport','stdio','--caller',config['caller']]
        with contextlib.redirect_stdout(sys.stderr):
            result=asyncio.run(mcp_call(config['python'],args,request['tool'],request['arguments'],config.get('environment')))
        rejected=rejection_from(result,request['tool'],request['arguments'])
        if rejected:raise RejectedBeforeExecution(rejected)
        if not isinstance(result,dict):raise ValueError('invalid result')
        print(canonical({'ok':True,'result':result}))
        return 0
    except RejectedBeforeExecution as exc:
        print(canonical({'ok':False,'response':{'ok':False,'error':{'code':exc.evidence['code'],'details':exc.evidence}}}))
        return 2
    except BaseException:
        print(canonical({'ok':False,'reason':'战队连接或能力不可用；写入结果未知时仅可查询，不得重发'}))
        return 2

if __name__=='__main__':sys.exit(main())
