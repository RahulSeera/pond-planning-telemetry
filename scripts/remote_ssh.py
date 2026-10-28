#!/usr/bin/env python3
import sys
import time
import paramiko

HOST = "10.1.75.53"
PORT = 2246
USER = "student"
PASS = "Rahul@2006"

def get_client():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=15)
    return ssh

def run(cmd, timeout=300):
    ssh = get_client()
    try:
        print(f"--> [REMOTE RUN]: {cmd}")
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        exit_code = stdout.channel.recv_exit_status()
        if out:
            print("[STDOUT]:\n" + out)
        if err:
            print("[STDERR]:\n" + err)
        print(f"<-- Exit Code: {exit_code}")
        return exit_code, out, err
    finally:
        ssh.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = " ".join(sys.argv[1:])
        code, out, err = run(cmd)
        sys.exit(code)
    else:
        print("Usage: remote_ssh.py <command>")
