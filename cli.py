"""作者：Wangyuan；日期：2026-10-29；职责：显式文件输入的离线命令行。"""
import argparse,json,sys
from pathlib import Path
from core import run
from stock.services.trading import TradeError
def main():
    parser=argparse.ArgumentParser(description='离线工具：读取显式JSON输入；不自动扫描个人目录。')
    parser.add_argument('input',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        payload=json.loads(args.input.read_text(encoding='utf-8'))
        result=run(payload)
        text=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
        if args.output:
            with args.output.open('x',encoding='utf-8') as target:target.write(text+'\n')
        else:print(text)
    except (ValueError,TypeError,KeyError,OSError,TradeError) as exc:
        parser.exit(2,type(exc).__name__+': '+str(exc)+'\n')
if __name__=='__main__':main()
