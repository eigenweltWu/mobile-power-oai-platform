#!/usr/bin/env python3
"""Add GET /api/ue/ip (max-IP) + POST /api/shake to the OAI control center.

/api/shake performs the timestamped downlink/uplink handshake with the phone
over the 5G PDU link (USB-free): it resolves the current UE IP from the UPF
session table, sends N /agent/downlink pings carrying the control-center clock,
and returns the UE IP + RTT/offset + phone timestamps so the platform can
complete sync-confirm without ever touching USB.
"""
import io

P = '/home/usrp2/桌面/OAI(1)/oai-control-center/backend/server.py'
s = io.open(P, encoding='utf-8').read()

# 1. import urllib.request (HTTP client for the on-phone agent)
anchor1 = 'from urllib.parse import parse_qs, urlparse'
assert s.count(anchor1) == 1, 'anchor1=%d' % s.count(anchor1)
s = s.replace(anchor1, anchor1 + '\nimport urllib.request', 1)

# 2. module-level UE IP resolver (USB-free, max host id from UPF session table)
anchor2 = 'def container_state(name: str) -> dict:'
helper = (
    'def resolve_ue_ip() -> str | None:\n'
    '    """Current UE PDU IPv4 from the UPF session table (USB-free)."""\n'
    '    host_ids = set()\n'
    '    for line in docker_logs("oai-upf", 3000):\n'
    '        for m in re.finditer(r"10\\.0\\.1\\.(\\d+)", line):\n'
    '            host_ids.add(int(m.group(1)))\n'
    '    return ("10.0.1.%d" % max(host_ids)) if host_ids else None\n\n\n'
)
assert s.count(anchor2) == 1, 'anchor2=%d' % s.count(anchor2)
s = s.replace(anchor2, helper + anchor2, 1)

# 3. /api/shake endpoint in do_POST (after /api/gnb/service block)
anchor3 = (
    '                labels = {"start": "基站已启动", "stop": "基站已停止", "restart": "基站已重新启动"}\n'
    '                return self.json({"ok": True, "message": labels[action]})\n'
)
shake = (
    '            if self.path == "/api/shake":\n'
    '                body = self.body()\n'
    '                n_exchanges = max(1, min(10, int(body.get("n_exchanges", 3))))\n'
    '                ue_ip = resolve_ue_ip()\n'
    '                if not ue_ip:\n'
    '                    return self.json({"ok": False, "error": "no UE PDU session"}, HTTPStatus.SERVICE_UNAVAILABLE)\n'
    '                exchanges = []\n'
    '                for i in range(n_exchanges):\n'
    '                    t_send = time.time() * 1000.0\n'
    '                    try:\n'
    '                        req = urllib.request.Request(\n'
    '                            "http://%s:8420/agent/downlink" % ue_ip,\n'
    '                            data=json.dumps({"seq": i + 1, "pcSendMs": t_send}).encode(),\n'
    '                            headers={"Content-Type": "application/json"},\n'
    '                        )\n'
    '                        resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())\n'
    '                    except Exception as e:\n'
    '                        return self.json({"ok": False, "ue_ip": ue_ip, "error": "downlink failed: %s" % e}, HTTPStatus.BAD_GATEWAY)\n'
    '                    t_recv = time.time() * 1000.0\n'
    '                    exchanges.append({\n'
    '                        "seq": i + 1, "pc_send_ms": t_send, "pc_recv_ms": t_recv,\n'
    '                        "phone_recv_ms": resp.get("phoneRecvMs"),\n'
    '                        "phone_send_ms": resp.get("phoneSendMs"),\n'
    '                        "phone_elapsed_ns": resp.get("phoneElapsedNs"),\n'
    '                        "rtt_ms": t_recv - t_send,\n'
    '                        "monitoring": resp.get("monitoring", True),\n'
    '                    })\n'
    '                best = min(exchanges, key=lambda e: e["rtt_ms"])\n'
    '                pr = best["phone_recv_ms"]; ps = best["phone_send_ms"]\n'
    '                t2_utc = ((pr + ps) / 2.0) if (pr and ps) else None\n'
    '                offset = ((best["pc_send_ms"] + best["rtt_ms"] / 2.0) - t2_utc) if t2_utc is not None else None\n'
    '                return self.json({\n'
    '                    "ok": True, "ue_ip": ue_ip,\n'
    '                    "rtt_ms": best["rtt_ms"], "offset_ms": offset,\n'
    '                    "phone_recv_ms": pr, "phone_send_ms": ps,\n'
    '                    "phone_elapsed_ns": best["phone_elapsed_ns"],\n'
    '                    "monitoring": best["monitoring"],\n'
    '                    "exchanges": exchanges,\n'
    '                })\n'
)
assert s.count(anchor3) == 1, 'anchor3=%d' % s.count(anchor3)
s = s.replace(anchor3, anchor3 + shake, 1)

io.open(P, 'w', encoding='utf-8').write(s)
print('OK patched server.py: urllib + resolve_ue_ip + /api/shake')
