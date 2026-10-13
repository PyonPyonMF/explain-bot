"""Restart the private speech worker and SSH tunnel; run with pythonw in a logon task."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--config",required=True);args=parser.parse_args()
    path=Path(args.config);config=json.loads(path.read_text(encoding="utf-8-sig"))
    commands=[
        [sys.executable,str(path.parent/"speech_worker.py"),"--config",str(path)],
        ["ssh","-N","-T","-o","BatchMode=yes","-o","ExitOnForwardFailure=yes","-o","ServerAliveInterval=15",
         "-o","ServerAliveCountMax=3","-o","StreamLocalBindUnlink=yes","-o","StreamLocalBindMask=0177",
         "-i",config["ssh_key"],"-p",str(config.get("ssh_port",2223)),"-o","IdentitiesOnly=yes",
         "-o","StrictHostKeyChecking=yes","-o","UserKnownHostsFile="+config["known_hosts"],"-R",
         config["remote_socket"]+":127.0.0.1:"+str(config.get("port",8766)),config["ssh_host"]]]
    processes=[None,None]
    try:
        while True:
            for i,command in enumerate(commands):
                if processes[i] is None or processes[i].poll() is not None:
                    processes[i]=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            time.sleep(10)
    finally:
        for process in processes:
            if process and process.poll() is None: process.terminate()


if __name__=="__main__":main()
