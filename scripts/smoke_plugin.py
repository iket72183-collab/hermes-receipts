"""Exercise native terminal pass, failure and timeout through real dispatch."""
import argparse
import json
from pathlib import Path
from native_runtime import NativeRuntime


def main():
    p=argparse.ArgumentParser();p.add_argument('--hermes-source',type=Path,required=True);p.add_argument('--evidence-dir',type=Path,required=True)
    args=p.parse_args();runtime=NativeRuntime(args.hermes_source,args.evidence_dir)
    results=[]
    scenarios=[('pass','printf Ready',{'contains':'Ready'},'verified',5),('fail','false',{},'failed',5),('timeout','sleep 2',{},'unverified',1)]
    for label,command,extra,want,timeout in scenarios:
        spec={'schema_version':1,'task_id':'smoke-'+label,'workspace_root':str(runtime.workspace),'checks':[{'id':'test','kind':'test','command':command,'workdir':str(runtime.workspace),'timeout':timeout,**extra}]}
        begin=runtime.call('receipts_begin',{'spec':spec});assert begin.get('ok'),begin
        terminal=runtime.call('terminal',{'command':command,'workdir':str(runtime.workspace),'timeout':timeout})
        receipt=runtime.call('receipts_check',{'attempt_id':begin['attempt_id']})
        assert receipt.get('state')==want, {'scenario':label,'terminal':terminal,'receipt':receipt}
        results.append({'scenario':label,'state':receipt['state'],'attempt_id':begin['attempt_id']})
        runtime.exports(begin['attempt_id'])
    summary={'native_dispatch':True,'callbacks_seeded':False,'model_calls':0,'home':str(runtime.home),'scenarios':results}
    (args.evidence_dir/'smoke-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()

