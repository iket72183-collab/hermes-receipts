"""Real failed -> corrected -> verified demo, including observed native tests.

--interactive pauses at visible stages for honest continuous video capture.
The wrong headline is deliberate, disclosed fault injection.
"""
import argparse
import functools
import http.server
import json
import threading
from pathlib import Path
from native_runtime import NativeRuntime, default_source


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


def main():
    p=argparse.ArgumentParser();p.add_argument('--hermes-source',type=Path);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--interactive',action='store_true')
    args=p.parse_args();runtime=NativeRuntime(args.hermes_source or default_source(),args.output_dir)
    page=runtime.workspace/'index.html'
    page.write_text('<!doctype html><h1>Wrong headline</h1>')
    (runtime.workspace/'verify_page.py').write_text('from pathlib import Path\nassert "Hermes Receipts is ready" in Path("index.html").read_text()\nprint("PAGE_TEST_PASSED")\n')
    handler=functools.partial(QuietHandler,directory=str(runtime.workspace))
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    command='python3 verify_page.py'
    spec={'schema_version':1,'task_id':'webpage-delivery','workspace_root':str(runtime.workspace),'allow_loopback_http':True,'checks':[
        {'id':'file','kind':'file','path':'index.html','contains':'Hermes Receipts is ready'},
        {'id':'test','kind':'test','command':command,'workdir':str(runtime.workspace),'contains':'PAGE_TEST_PASSED','timeout':5},
        {'id':'website','kind':'website','url':'http://127.0.0.1:'+str(server.server_port)+'/','expected_status':200,'contains':'Hermes Receipts is ready'}]}
    (args.output_dir/'criteria.json').write_text(json.dumps(spec,indent=2)+'\n')
    print('CONTROLLED DEMO: wrong headline deliberately served; no model output is fabricated.',flush=True)
    print('Criteria saved before verification. Local page: '+spec['checks'][2]['url'],flush=True)
    stages=[]
    try:
        for phase,want in [('wrong-headline','failed'),('corrected-headline','verified')]:
            if phase=='corrected-headline':
                if args.interactive:input('Press Enter to correct the actual webpage and rerun: ')
                page.write_text('<!doctype html><h1>Hermes Receipts is ready</h1>')
                print('Corrected the actual file; creating a fresh attempt.',flush=True)
            elif args.interactive:input('Press Enter to run checks against the deliberately wrong page: ')
            begin=runtime.call('receipts_begin',{'spec':spec});assert begin.get('ok'),begin
            terminal=runtime.call('terminal',{'command':command,'workdir':str(runtime.workspace),'timeout':5})
            receipt=runtime.call('receipts_check',{'attempt_id':begin['attempt_id']})
            assert receipt.get('state')==want,{'phase':phase,'terminal':terminal,'receipt':receipt}
            assert all(c['state'] == ('failed' if phase == 'wrong-headline' else 'passed') for c in receipt['checks']), receipt
            exported=runtime.exports(begin['attempt_id'])
            stages.append({'phase':phase,'state':receipt['state'],'attempt_id':begin['attempt_id'],'exports':exported,'checks':[{'id':c['id'],'state':c['state']} for c in receipt['checks']]})
            print(json.dumps(stages[-1],indent=2),flush=True)
        history=runtime.call('receipts_get',{'task_id':'webpage-delivery'})
        assert history['count']==2,history
        summary={'fault_injection_disclosed':True,'native_dispatch':True,'model_calls':0,'home':str(runtime.home),'stages':stages,'history_count':history['count']}
        (args.output_dir/'demo-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        print('Both attempts preserved. This certifies only the explicit checks, at their recorded times.',flush=True)
        if args.interactive:input('Press Enter to stop this demo server: ')
    finally:
        server.shutdown();server.server_close();thread.join()


if __name__=='__main__':main()
