#!/usr/bin/env python3
"""Fix GET /api/ue/ip: return the numerically-highest UE IP (the SMF assigns
IPs incrementally, so the highest recent 10.0.1.x is the current session)."""
import io

P = '/home/usrp2/桌面/OAI(1)/oai-control-center/backend/server.py'

OLD = (
    '            if parsed.path == "/api/ue/ip":\n'
    '                ue_ip = None\n'
    '                for line in docker_logs("oai-upf", 2000):\n'
    '                    m = re.search(r"10\\.0\\.1\\.\\d+", line)\n'
    '                    if m:\n'
    '                        ue_ip = m.group(0)\n'
    '                return self.json({"ue_ip": ue_ip, "timestamp": datetime.now(timezone.utc).isoformat()})\n'
)

NEW = (
    '            if parsed.path == "/api/ue/ip":\n'
    '                host_ids = set()\n'
    '                for line in docker_logs("oai-upf", 3000):\n'
    '                    for m in re.finditer(r"10\\.0\\.1\\.(\\d+)", line):\n'
    '                        host_ids.add(int(m.group(1)))\n'
    '                ue_ip = ("10.0.1.%d" % max(host_ids)) if host_ids else None\n'
    '                return self.json({"ue_ip": ue_ip, "timestamp": datetime.now(timezone.utc).isoformat()})\n'
)

s = io.open(P, encoding='utf-8').read()
n = s.count(OLD)
assert n == 1, 'OLD count=%d' % n
s = s.replace(OLD, NEW, 1)
io.open(P, 'w', encoding='utf-8').write(s)
print('OK patched /api/ue/ip (max-IP)')
