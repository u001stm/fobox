"""Per-user SD arrival launcher. No cloud activity, no writes to the card."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


def enabled_file(db):return Path(db).parent/'card-watch.enabled'


def configure(db,app,enabled=True,port=8766):
    if os.name!='nt':raise ValueError('Автооткрытие при входе поддерживается в Windows')
    marker=enabled_file(db)
    filename='LUMIO Card Watch.vbs' if port==8766 else 'LUMIO Card Watch '+str(port)+'.vbs'
    startup=Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup'/filename
    if not enabled:
        marker.unlink(missing_ok=True)
        if startup.exists() and 'LUMIO_CARD_WATCH' in startup.read_text('utf-8'):startup.unlink()
        return {'enabled':False}
    marker.parent.mkdir(parents=True,exist_ok=True);marker.write_text('LUMIO_CARD_WATCH','utf-8')
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    command=subprocess.list2cmdline([str(pythonw),str(Path(app)/'card_watch.py'),'--db',str(db),'--port',str(port)])
    script="' LUMIO_CARD_WATCH\nCreateObject(\"WScript.Shell\").Run \""+command.replace('"','""')+'", 0, False\n'
    startup.parent.mkdir(parents=True,exist_ok=True);startup.write_text(script,'utf-8')
    subprocess.Popen([str(pythonw),str(Path(app)/'card_watch.py'),'--db',str(db),'--port',str(port)],cwd=app,creationflags=subprocess.CREATE_NO_WINDOW)
    return {'enabled':True}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True);parser.add_argument('--port',type=int,default=8766);args=parser.parse_args()
    if os.name=='nt':
        import ctypes
        mutex=ctypes.windll.kernel32.CreateMutexW(None,False,'Local\\LUMIO.CardWatch.'+str(args.port))
        if ctypes.windll.kernel32.GetLastError()==183:return
    root=Path(__file__).parent;url='http://127.0.0.1:'+str(args.port);last=0;launched=0
    while enabled_file(args.db).exists():
        try:
            with urllib.request.urlopen(url+'/api/media-state',timeout=5) as response:data=json.load(response)
            fresh=[e for e in data.get('events',[]) if e['id']>last]
            if fresh:
                last=fresh[-1]['id']
                if not data.get('ui_active'):
                    event=fresh[-1]
                    webbrowser.open(url+'/?'+urllib.parse.urlencode({'card':'open','event':str(event['id'])}))
        except Exception:
            if time.time()-launched>30:
                subprocess.Popen([sys.executable,str(root/'lumio.py'),'--db',args.db,'--port',str(args.port),'--no-browser'],cwd=root,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                launched=time.time()
        time.sleep(3)


if __name__=='__main__':main()
