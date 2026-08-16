"""Stirrer motor control for the reverberation chamber (RC).

The chamber's stirrer controller speaks the MT_API.dll protocol (USB,
position mode, 3200 steps per degree — see ``measurement system/
AntennaTurntableController/ASimpleDemo``). That DLL is 32-BIT, so a 64-bit
Python cannot ctypes it directly; instead we compile (once, with the
Windows-built-in .NET Framework csc) a tiny x86 helper EXE that P/Invokes
MT_API.dll and answers JSON-line commands on stdin/stdout.

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

STEPS_PER_DEG = 3200

# 32-bit x86 helper — the MT_API.dll it loads is 32-bit (PE machine 0x014C).
_HELPER_CS = r"""
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

public class MT_API
{
    public const Int32 R_OK = 0;

    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Init();
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_DeInit();
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Open_USB();
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Close_USB();
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Check();
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Get_Axis_Num(ref Int32 pValue);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Set_Axis_Mode_Position(UInt16 AObj);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Set_Axis_Acc(UInt16 AObj, Int32 Value);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Set_Axis_Dec(UInt16 AObj, Int32 Value);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Set_Axis_Position_V_Max(UInt16 AObj, Int32 Value);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Set_Axis_Position_P_Target_Rel(UInt16 AObj, Int32 Value);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Set_Axis_Position_P_Target_Abs(UInt16 AObj, Int32 Value);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Set_Axis_Position_Stop(UInt16 AObj);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Set_Axis_Halt(UInt16 AObj);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Get_Axis_Software_P_Now(UInt16 AObj, ref Int32 pValue);
    [DllImport("MT_API.dll", CharSet = CharSet.Ansi, CallingConvention = CallingConvention.StdCall)]
    public static extern Int32 MT_Get_Axis_Status_Run(UInt16 AObj, ref Byte pRun);
}

public static class StirrerAgent
{
    private const double StepsPerDeg = 3200.0;
    private static bool _opened = false;

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
                        int rc = MT_API.MT_Init();
                        if (rc != MT_API.R_OK) return Err("MT_Init rc=" + rc);
                        rc = MT_API.MT_Open_USB();
                        if (rc != MT_API.R_OK) { MT_API.MT_DeInit(); return Err("MT_Open_USB rc=" + rc + " (controller attached?)"); }
                        _opened = true;
                        int axes = 0;
                        MT_API.MT_Get_Axis_Num(ref axes);
                        MT_API.MT_Set_Axis_Mode_Position(0);
                    }
                    return Json(Ok(null));
                case "close":
                    if (_opened) { try { MT_API.MT_Close_USB(); } catch { } try { MT_API.MT_DeInit(); } catch { } _opened = false; }
                    return Json(Ok(null));
                case "check":
                {
                    if (!_opened) return Err("not_open");
                    int rc = MT_API.MT_Check();
                    var d = Ok(null); d["check"] = (rc == MT_API.R_OK); d["rc"] = rc; return Json(d);
                }
                case "position":
                {
                    if (!_opened) return Err("not_open");
                    Int32 pos = 0;
                    int rc = MT_API.MT_Get_Axis_Software_P_Now(0, ref pos);
                    if (rc != MT_API.R_OK) return Err("MT_Get_Axis_Software_P_Now rc=" + rc);
                    var d = Ok(null); d["steps"] = pos; d["deg"] = Math.Round(pos / StepsPerDeg, 3); return Json(d);
                }
                case "running":
                {
                    if (!_opened) return Err("not_open");
                    Byte run = 0;
                    int rc = MT_API.MT_Get_Axis_Status_Run(0, ref run);
                    if (rc != MT_API.R_OK) return Err("MT_Get_Axis_Status_Run rc=" + rc);
                    var d = Ok(null); d["running"] = (run == 1); return Json(d);
                }
                case "set_params":
                {
                    if (!_opened) return Err("not_open");
                    double speed = Argd(args, "speed_deg_s", 20.0);
                    double acc = Argd(args, "accel_deg_s2", 40.0);
                    double dec = Argd(args, "dec_deg_s2", 40.0);
                    int r1 = MT_API.MT_Set_Axis_Acc(0, Deg(acc));
                    int r2 = MT_API.MT_Set_Axis_Dec(0, Deg(dec));
                    int r3 = MT_API.MT_Set_Axis_Position_V_Max(0, Deg(speed));
                    var d = Ok(null); d["acc_rc"] = r1; d["dec_rc"] = r2; d["vmax_rc"] = r3; return Json(d);
                }
                case "move_rel":
                case "move_abs":
                {
                    if (!_opened) return Err("not_open");
                    double deg = Argd(args, "deg", 0.0);
                    int steps = (int)Math.Truncate(deg * StepsPerDeg);
                    int rc = name == "move_rel"
                        ? MT_API.MT_Set_Axis_Position_P_Target_Rel(0, steps)
                        : MT_API.MT_Set_Axis_Position_P_Target_Abs(0, steps);
                    if (rc != MT_API.R_OK) return Err("move rc=" + rc);
                    var d = Ok(null); d["steps"] = steps; return Json(d);
                }
                case "stop":
                {
                    if (!_opened) return Err("not_open");
                    int rc = MT_API.MT_Set_Axis_Position_Stop(0);
                    if (rc != MT_API.R_OK) MT_API.MT_Set_Axis_Halt(0);
                    return Json(Ok(null));
                }
                default:
                    return Err("unknown cmd: " + name);
            }
        }
        catch (Exception ex) { return Err(ex.Message); }
    }

    private static int Deg(double deg) { return (int)Math.Truncate(deg * StepsPerDeg); }
    private static double Argd(Dictionary<string, object> a, string k, double def)
    { object v; return (a != null && a.TryGetValue(k, out v) && v != null) ? Convert.ToDouble(v) : def; }

    /** Minimal {"k":v,"k2":v2} parser — csc (C#5) without extra references. */
    private static Dictionary<string, object> ParseArgs(string s)
    {
        var d = new Dictionary<string, object>();
        if (string.IsNullOrEmpty(s)) return d;
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


def find_mt_api_dll() -> Optional[Path]:
    """Locate MT_API.dll — vendored copy first, then the measurement-system tree."""
    root = Path(__file__).resolve().parents[2]
    vendored = root / "experiment_platform" / "backend" / "vendor" / "MT_API.dll"
    if vendored.exists():
        return vendored
    for cand in (root / "measurement system").rglob("MT_API.dll"):
        return cand
    return None


class StirrerError(RuntimeError):
    pass


class StirrerAgent:
    """Thread-safe wrapper over the x86 helper process (or a virtual motor).

    ``simulate=True`` keeps a virtual position and instant moves so the RC
    campaign flow is testable without the chamber hardware attached.
    """

    def __init__(self, settings: Settings, simulate: bool = False,
                 speed_deg_s: float = 20.0, accel_deg_s2: float = 40.0):
        self.s = settings
        self.simulate = simulate
        self.speed_deg_s = speed_deg_s
        self.accel_deg_s2 = accel_deg_s2
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

    def ensure_helper(self, force: bool = False) -> Path:
        """Compile the x86 helper (once) and stage MT_API.dll next to it."""
        self.tools_dir.mkdir(parents=True, exist_ok=True)
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
               "/out:" + str(exe), str(cs)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise StirrerError(f"csc failed: {r.stderr[:500]}")
        hash_file.write_text(src_hash, encoding="utf-8")
        dll = find_mt_api_dll()
        if dll:
            shutil.copy2(dll, self.tools_dir / "MT_API.dll")
        else:
            raise StirrerError("MT_API.dll not found (measurement system not installed?)")
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
        r = self._request("open", timeout=30.0)
        if not r.get("ok"):
            self.last_error = r.get("error")
            return r
        # MT_Open_USB can "succeed" at the driver level with no controller
        # attached — MT_Check is the real liveness probe.
        chk = self._request("check")
        if not chk.get("ok") or not chk.get("check"):
            self.last_error = "controller not attached (MT_Check failed)"
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
                              "dll_found": find_mt_api_dll() is not None}
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
