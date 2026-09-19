#!/usr/bin/env python3
"""Install/remove a local macOS LaunchAgent. Never uses remote CI for private data."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

LABEL='local.moze-rs.sync'
p=argparse.ArgumentParser()
p.add_argument('action',choices=['install','uninstall'])
p.add_argument('--hour',type=int,default=14)
p.add_argument('--minute',type=int,default=0)
p.add_argument('--mirror',type=Path)
a=p.parse_args()
if not 0<=a.hour<=23 or not 0<=a.minute<=59: p.error('Invalid time')
root=Path(__file__).resolve().parents[1]
plist=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
domain=f'gui/{os.getuid()}'
if a.action=='uninstall':
    subprocess.run(['launchctl','bootout',domain,str(plist)],check=False,capture_output=True)
    plist.unlink(missing_ok=True)
else:
    binary=root/'target/release/moze-rs'
    if not binary.is_file(): p.error('Run cargo build --release first')
    python=shutil.which('python3');node=shutil.which('node')
    if not python or not node: p.error('python3 and node must be installed')
    args=[str(binary),'sync']
    if a.mirror: args+=['--mirror',str(a.mirror.expanduser().resolve())]
    config={'Label':LABEL,'ProgramArguments':args,'WorkingDirectory':str(root),
        'EnvironmentVariables':{'HOME':str(Path.home()),'MOZE_RS_HOME':str(root),'MOZE_PYTHON':python,'MOZE_NODE':node,'REALM_DISABLE_ANALYTICS':'1'},
        'StartCalendarInterval':{'Hour':a.hour,'Minute':a.minute},'RunAtLoad':True,'Umask':63,
        'ProcessType':'Background'}
    os.umask(0o077);plist.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['launchctl','bootout',domain,str(plist)],check=False,capture_output=True)
    with plist.open('wb') as f: plistlib.dump(config,f)
    subprocess.run(['launchctl','bootstrap',domain,str(plist)],check=True)
    print(f'Installed {LABEL} at {a.hour:02}:{a.minute:02} local time; also runs at login. No transaction logs.')
