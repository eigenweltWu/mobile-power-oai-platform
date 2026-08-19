"""Stirrer motor control for the reverberation chamber (RC).

The chamber controller is driven through ``StirrerDll.Stirrers``, matching
the working ``ReverbChamberMeasSys`` job-queue path.  A tiny .NET helper keeps
that vendor DLL and its synchronous serial calls outside the Python process
and answers JSON-line commands on stdin/stdout.

A ``simulated`` mode (no hardware) exists so the full RC campaign flow can
be exercised outside the chamber: positions update virtually, moves finish
immediately.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .config import Settings

STEPS_PER_DEG = 50000 / 360

# The vendor application runs 32-bit; keep the helper in the same runtime mode.
_HELPER_CS = r"""
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using StirrerDll;

public static class StirrerAgent
{
    private const double StepsPerDeg = 50000.0 / 360.0;
    private static readonly Stirrers Motor = new Stirrers();
    private static bool _opened = false;
    private static string _port = "";
    private static double _positionDeg = 0.0;

    // The vendor Stirrers object opens and holds the COM port as soon as
    // PortNameString is assigned, so liveness must be probed through the
    // driver itself — an independent SerialPort open would collide with it.
    private static bool ProbeOk(string result)
    {
        return result != null && result.IndexOf("success", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    private static string Cmd(string name, Dictionary<string, object> args)
    {
        try
        {
            object v;
            switch (name)
            {
                case "open":
                    if (!_opened)
                    {
                        object portValue;
                        string port = args.TryGetValue("port", out portValue) ? Convert.ToString(portValue) : "";
                        if (String.IsNullOrWhiteSpace(port)) return Err("COM port is required");
                        Motor.PortNameString = port;   // vendor driver opens and holds the port here
                        string probe = Motor.TestConnection();
                        if (!ProbeOk(probe)) return Err("open " + port + ": " + probe);
                        _port = port;
                        _opened = true;
                    }
                    var opened = Ok(null); opened["port"] = _port; opened["transport"] = "serial"; return Json(opened);
                case "close":
                    _opened = false;
                    return Json(Ok(null));
                case "check":
                {
                    if (!_opened) return Err("not_open");
                    try
                    {
                        string r = Motor.TestConnection();
                        bool ok = ProbeOk(r);
                        var d = Ok(null); d["check"] = ok; d["rc"] = ok ? 0 : -1; d["detail"] = r; return Json(d);
                    }
                    catch (Exception ex) { var d = Ok(null); d["check"] = false; d["rc"] = -1; d["detail"] = ex.Message; return Json(d); }
                }
                case "position":
                {
                    if (!_opened) return Err("not_open");
                    var d = Ok(null); d["steps"] = (int)Math.Round(_positionDeg * StepsPerDeg); d["deg"] = Math.Round(_positionDeg, 3); return Json(d);
                }
                case "running":
                {
                    if (!_opened) return Err("not_open");
                    var d = Ok(null); d["running"] = false; return Json(d);
                }
                case "set_params":
                {
                    if (!_opened) return Err("not_open");
                    return Json(Ok(null));
                }
                case "move_rel":
                case "move_abs":
                {
                    if (!_opened) return Err("not_open");
                    double requested = Argd(args, "deg", 0.0);
                    double delta = name == "move_rel" ? requested : requested - _positionDeg;
                    int steps = (int)Math.Round(delta * StepsPerDeg);
                    if (steps != 0)
                    {
                        int speed = Math.Min(Math.Abs(steps), 2000);
                        string result = Motor.RotateV(steps, speed, 138, 138);
                        _positionDeg += delta;
                        var moved = Ok(null); moved["steps"] = steps; moved["driver_result"] = result; return Json(moved);
                    }
                    var unchanged = Ok(null); unchanged["steps"] = 0; return Json(unchanged);
                }
                case "stop":
                {
                    if (!_opened) return Err("not_open");
                    string result = Motor.StopVStirrer();
                    var d = Ok(null); d["driver_result"] = result; return Json(d);
                }
                default:
                    return Err("unknown cmd: " + name);
            }
        }
        catch (Exception ex) { return Err(ex.Message); }
    }

    private static double Argd(Dictionary<string, object> a, string k, double def)
    { object v; return (a != null && a.TryGetValue(k, out v) && v != null) ? Convert.ToDouble(v) : def; }

    /** Minimal {"k":v,"k2":v2} parser — csc (C#5) without extra references. */
    private static Dictionary<string, object> ParseArgs(string s)
    {
        var d = new Dictionary<string, object>();
        if (string.IsNullOrEmpty(s)) return d;
        s = s.Trim();
        if (s.StartsWith("{") && s.EndsWith("}")) s = s.Substring(1, s.Length - 2);
        foreach (string part in s.Split(','))
        {
            int c = part.IndexOf(':');
            if (c <= 0) continue;
            string k = part.Substring(0, c).Trim().Trim('"');
            string raw = part.Substring(c + 1).Trim();
            double num;
            if (double.TryParse(raw, System.Globalization.NumberStyles.Any,
                System.Globalization.CultureInfo.InvariantCulture, out num)) d[k] = num;
            else d[k] = raw.Trim('"');
        }
        return d;
    }

    private static Dictionary<string, object> Ok(Dictionary<string, object> extra)
    { var d = new Dictionary<string, object>(); d["ok"] = true; if (extra != null) foreach (var kv in extra) d[kv.Key] = kv.Value; return d; }
    private static string Err(string msg)
    { var d = new Dictionary<string, object>(); d["ok"] = false; d["error"] = msg; return Json(d); }
    private static string Json(Dictionary<string, object> d)
    { var sb = new StringBuilder("{"); bool first = true; foreach (var kv in d) { if (!first) sb.Append(','); first = false; sb.Append("\"").Append(kv.Key).Append("\":"); sb.Append(Serialize(kv.Value)); } sb.Append("}"); return sb.ToString(); }
    private static string Serialize(object v)
    {
        if (v is bool) return (bool)v ? "true" : "false";
        if (v is int || v is long || v is byte) return Convert.ToString(v);
        if (v is double) { double d = (double)v; if (double.IsNaN(d) || double.IsInfinity(d)) return "null"; return d.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture); }
        if (v == null) return "null";
        return "\"" + v.ToString().Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", " ").Replace("\n", " ") + "\"";
    }

    public static int Main()
    {
        try
        {
            Console.InputEncoding = Encoding.UTF8;
            Console.OutputEncoding = Encoding.UTF8;
        }
        catch { }
        string line;
        while ((line = Console.ReadLine()) != null)
        {
            line = line.Trim();
            if (line.Length == 0) continue;
            string name = line; var args = new Dictionary<string, object>();
            int sp = line.IndexOf(' ');
            if (sp > 0)
            {
                name = line.Substring(0, sp);
                args = ParseArgs(line.Substring(sp + 1));
            }
            string resp;
            try { resp = Cmd(name, args); }
            catch (Exception ex) { resp = Err(ex.Message); }
            Console.WriteLine(resp);
            Console.Out.Flush();
        }
        Cmd("close", null);
        return 0;
    }
}
"""

_CSC_CANDIDATES = [
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
]


def find_stirrer_dll() -> Optional[Path]:
    """Locate the vendor StirrerDll.dll used by ReverbChamberMeasSys."""
    root = Path(__file__).resolve().parents[2]
    vendored = root / "experiment_platform" / "backend" / "vendor" / "StirrerDll.dll"
    if vendored.exists():
        return vendored
    reference_tree = root / "measurement system"
    if reference_tree.exists():
        for cand in reference_tree.rglob("StirrerDll.dll"):
            return cand
    return None


class StirrerError(RuntimeError):
    pass


def list_com_ports() -> list[str]:
    """Enumerate Windows COM ports without adding a pyserial dependency."""
    ports: set[str] = set()
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
        index = 0
        while True:
            try:
                _, value, _ = winreg.EnumValue(key, index)
                ports.add(str(value).upper())
                index += 1
            except OSError:
                break
        winreg.CloseKey(key)
    except Exception:
        pass
    return sorted(ports, key=lambda value: (len(value), value))


class StirrerAgent:
    """Thread-safe wrapper over the x86 helper process (or a virtual motor).

    ``simulate=True`` keeps a virtual position and instant moves so the RC
    campaign flow is testable without the chamber hardware attached.
    """

    def __init__(self, settings: Settings, simulate: bool = False,
                 com_port: Optional[str] = None,
                 speed_deg_s: float = 20.0, accel_deg_s2: float = 40.0):
        self.s = settings
        self.simulate = simulate
        self.speed_deg_s = speed_deg_s
        self.accel_deg_s2 = accel_deg_s2
        saved_port = settings.data_dir / "stirrer_port.txt"
        self.com_port = (com_port or (saved_port.read_text(encoding="utf-8").strip()
                                     if saved_port.exists() else "")) or None
        self.tools_dir = settings.data_dir / "tools"
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._resp_q: "queue.SimpleQueue[str]" = None  # type: ignore[assignment]
        self._sim_deg: float = 0.0
        self._opened = False
        self.last_error: Optional[str] = None

    # ---- helper lifecycle -------------------------------------------------- #
    def _exe_path(self) -> Path:
        return self.tools_dir / "StirrerAgent.exe"

    def _stage_stirrer_dll(self) -> Path:
        """Copy StirrerDll.dll next to the helper and return the staged path."""
        dll = find_stirrer_dll()
        if not dll:
            raise StirrerError("StirrerDll.dll not found")
        dst = self.tools_dir / "StirrerDll.dll"
        if not dst.exists() or dst.stat().st_size != dll.stat().st_size:
            shutil.copy2(dll, dst)
        return dst

    def ensure_helper(self, force: bool = False) -> Path:
        """Compile the helper once and stage its vendor assembly."""
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        dll = self._stage_stirrer_dll()
        exe = self._exe_path()
        src_hash = hashlib.sha256(_HELPER_CS.encode("utf-8")).hexdigest()[:16]
        hash_file = self.tools_dir / "StirrerAgent.srcsha"
        if (not force and exe.exists() and hash_file.exists()
                and hash_file.read_text(encoding="utf-8").strip() == src_hash):
            return exe
        csc = next((c for c in _CSC_CANDIDATES if Path(c).exists()), None)
        if not csc:
            raise StirrerError(".NET Framework csc.exe not found — cannot build the stirrer helper")
        cs = self.tools_dir / "StirrerAgent.cs"
        cs.write_text(_HELPER_CS, encoding="utf-8")
        cmd = [csc, "/nologo", "/platform:x86", "/optimize+",
               "/reference:" + str(dll), "/out:" + str(exe), str(cs)]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
        if r.returncode != 0:
            raise StirrerError(f"csc failed: {r.stderr[:500]}")
        hash_file.write_text(src_hash, encoding="utf-8")
        return exe

    def _spawn(self) -> None:
        import queue as _queue
        exe = self.ensure_helper()
        self._resp_q = _queue.SimpleQueue()
        self._proc = subprocess.Popen(
            [str(exe)], cwd=str(self.tools_dir),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def _read():
            try:
                for line in self._proc.stdout:  # type: ignore[union-attr]
                    line = line.strip()
                    if line:
                        self._resp_q.put(line)
            except Exception:
                pass
            finally:
                try:
                    self._resp_q.put("")
                except Exception:
                    pass

        self._reader = threading.Thread(target=_read, daemon=True, name="stirrer-reader")
        self._reader.start()

    def _request(self, cmd: str, payload: Optional[dict] = None,
                 timeout: float = 15.0) -> dict:
        with self._lock:
            if self.simulate:
                return self._simulate(cmd, payload or {})
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
            line = cmd if payload is None else f"{cmd} {json.dumps(payload, separators=(',', ':'))}"
            try:
                assert self._proc and self._proc.stdin
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                # helper died — respawn once and retry
                self._kill_proc()
                self._spawn()
                assert self._proc and self._proc.stdin
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            try:
                resp = self._resp_q.get(timeout=timeout)
            except Exception:
                raise StirrerError(f"stirrer helper timeout on '{cmd}'")
            if not resp:
                raise StirrerError("stirrer helper exited unexpectedly")
            try:
                return json.loads(resp)
            except json.JSONDecodeError:
                raise StirrerError(f"stirrer helper bad response: {resp[:200]}")

    def _kill_proc(self) -> None:
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def _simulate(self, cmd: str, payload: dict) -> dict:
        if cmd == "open":
            self._opened = True
            return {"ok": True}
        if cmd == "close":
            self._opened = False
            return {"ok": True}
        if not self._opened:
            return {"ok": False, "error": "not_open"}
        if cmd == "check":
            return {"ok": True, "check": True, "rc": 0}
        if cmd == "position":
            steps = int(round(self._sim_deg * STEPS_PER_DEG))
            return {"ok": True, "steps": steps, "deg": round(self._sim_deg, 3)}
        if cmd == "running":
            return {"ok": True, "running": False}
        if cmd == "set_params":
            if "speed_deg_s" in payload:
                self.speed_deg_s = float(payload["speed_deg_s"])
            return {"ok": True}
        if cmd == "move_rel":
            self._sim_deg += float(payload.get("deg", 0.0))
            steps = int(round(float(payload.get("deg", 0.0)) * STEPS_PER_DEG))
            return {"ok": True, "steps": steps}
        if cmd == "move_abs":
            deg = float(payload.get("deg", 0.0))
            steps = int(round((deg - self._sim_deg) * STEPS_PER_DEG))
            self._sim_deg = deg
            return {"ok": True, "steps": steps}
        if cmd == "stop":
            return {"ok": True}
        return {"ok": False, "error": f"unknown cmd: {cmd}"}

    # ---- public API --------------------------------------------------------- #
    def open(self) -> dict:
        r = self._request("open", {"port": self.com_port or ""}, timeout=30.0)
        if not r.get("ok"):
            self.last_error = r.get("error")
            return r
        chk = self._request("check")
        if not chk.get("ok") or not chk.get("check"):
            self.last_error = chk.get("detail") or "stirrer COM port is unavailable"
            self.close()
            return {"ok": False, "error": self.last_error}
        self._opened = True
        try:
            self._request("set_params", {"speed_deg_s": self.speed_deg_s,
                                         "accel_deg_s2": self.accel_deg_s2,
                                         "dec_deg_s2": self.accel_deg_s2})
        except StirrerError:
            pass
        return r

    def close(self) -> None:
        if self.simulate:
            self._opened = False
            return
        try:
            self._request("close", timeout=5.0)
        except Exception:
            pass
        self._kill_proc()
        self._opened = False

    def check(self) -> dict:
        return self._request("check")

    def position_deg(self) -> Optional[float]:
        r = self._request("position")
        return float(r["deg"]) if r.get("ok") else None

    def is_running(self) -> bool:
        r = self._request("running")
        return bool(r.get("running")) if r.get("ok") else False

    def move_rel(self, deg: float) -> dict:
        return self._request("move_rel", {"deg": deg})

    def move_abs(self, deg: float) -> dict:
        return self._request("move_abs", {"deg": deg})

    def stop(self) -> dict:
        return self._request("stop")

    def move_rel_and_wait(self, deg: float, timeout_s: float = 180.0) -> dict:
        """Relative move that blocks until the motor reports standstill."""
        r = self.move_rel(deg)
        if not r.get("ok"):
            return r
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                if not self.is_running():
                    return {"ok": True, "deg": self.position_deg()}
            except StirrerError:
                continue
        return {"ok": False, "error": "stirrer move timeout"}

    def status(self) -> dict:
        st: dict[str, Any] = {"simulated": self.simulate, "opened": self._opened,
                              "com_port": self.com_port,
                              "dll_found": find_stirrer_dll() is not None}
        try:
            exe = self._exe_path()
            st["exe_ready"] = exe.exists()
        except Exception:
            st["exe_ready"] = False
        if self._opened:
            try:
                st["position_deg"] = self.position_deg()
                st["running"] = self.is_running()
                st["connected"] = True
            except StirrerError as e:
                st["connected"] = False
                st["error"] = str(e)
        else:
            st["connected"] = False
        if self.last_error:
            st["last_error"] = self.last_error
        return st
