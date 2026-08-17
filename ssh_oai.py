"""Shared paramiko runner + ad-hoc batch executor for the OAI host."""
import sys
from pathlib import Path

import paramiko

HOST, USER, PW = "192.168.31.119", "usrp2", "12345678"


def connect():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PW, timeout=10,
                allow_agent=False, look_for_keys=False)
    return cli


def run(cli, cmd: str, timeout: int = 60) -> str:
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    return f"[rc={rc}]\n{o}{(chr(10) + '[STDERR]' + chr(10) + e) if e.strip() else ''}".strip()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cli = connect()
    try:
        cmds = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else None
        timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        print(run(cli, cmds, timeout))
    finally:
        cli.close()
