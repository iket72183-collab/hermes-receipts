"""Verify real new-process history and separate-profile isolation (offline)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from native_runtime import ROOT


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--hermes-source',type=Path,required=True);parser.add_argument('--home',type=Path,required=True);parser.add_argument('--evidence-dir',type=Path,required=True);parser.add_argument('--child',action='store_true');args=parser.parse_args()
    if args.child:
        os.environ['HERMES_HOME']=str(args.home.resolve());os.environ.pop('HERMES_PROFILE',None)
        sys.path.insert(0,str(args.hermes_source.resolve()))
        from hermes_cli.plugins import get_plugin_manager
        get_plugin_manager().discover_and_load()
        from model_tools import handle_function_call
        result=handle_function_call('receipts_get',{'task_id':'webpage-delivery'},task_id='history-probe',session_id='history-probe',tool_call_id='history-get')
        print(result);return
    args.evidence_dir.mkdir(parents=True,exist_ok=True)
    other=args.evidence_dir/'other-profile';other.mkdir()
    shutil.copytree(ROOT,other/'plugins'/'hermes-receipts',ignore=shutil.ignore_patterns('.git','.worktrees','output','__pycache__'))
    (other/'config.yaml').write_text('plugins:\n  enabled: [hermes-receipts]\n')
    summaries=[]
    for label,home,want in [('new-process-history',args.home,2),('other-profile',other,0)]:
        cmd=[sys.executable,str(Path(__file__).resolve()),'--child','--hermes-source',str(args.hermes_source),'--home',str(home),'--evidence-dir',str(args.evidence_dir)]
        run=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        (args.evidence_dir/(label+'.log')).write_text(run.stdout+run.stderr)
        assert run.returncode==0,run.stderr
        result=json.loads(run.stdout.strip().splitlines()[-1]);assert result.get('count')==want,result
        if want:self_states={r['state'] for r in result['attempts']};assert self_states=={'failed','verified'},self_states
        summaries.append({'scenario':label,'count':result['count'],'exit_code':run.returncode,'command':cmd})
    (args.evidence_dir/'persistence-summary.json').write_text(json.dumps(summaries,indent=2)+'\n')
    print(json.dumps(summaries,indent=2))


if __name__=='__main__':main()
