#!/usr/bin/env python3
"""打朋友版压缩包：不带 AI 战队后台，打包时查个人信息，有就停。

用法：python3 scripts/package_friend.py <输出.zip> [--marker 词 ...]
--marker 填打包人的名字、项目名、内网地址等不想外发的词；家目录路径和用户名会自动查。
"""
import argparse
from pathlib import Path
import sys
import tempfile
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portable.distribution import build
from portable.core import Blocked,canonical

def package(output,markers=()):
    output=Path(output).absolute()
    if output.exists():raise Blocked('输出文件已存在，不覆盖：'+str(output))
    with tempfile.TemporaryDirectory() as d:
        root=Path(d).resolve()/'随身开发'
        info=build(Path(__file__).resolve().parents[1],root,'friend',markers)
        output.parent.mkdir(parents=True,exist_ok=True)
        tmp=output.with_name('.'+output.name+'.part')
        with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
            for p in sorted(root.rglob('*')):
                arc='随身开发/'+p.relative_to(root).as_posix()+('/' if p.is_dir() else '')
                zi=zipfile.ZipInfo(arc);zi.external_attr=(p.stat().st_mode&0xFFFF)<<16
                if p.is_dir():z.writestr(zi,b'')
                else:zi.compress_type=zipfile.ZIP_DEFLATED;z.writestr(zi,p.read_bytes())
        tmp.rename(output)
    return {'朋友版':str(output),'版本':info['version'],'个人信息检查':'通过'}

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('output');a.add_argument('--marker',action='append',default=[]);args=a.parse_args()
    try:print(canonical(package(args.output,args.marker)))
    except (Blocked,OSError) as e:
        print(canonical({'status':'BLOCKED','reason':str(e)}),file=sys.stderr);sys.exit(2)
