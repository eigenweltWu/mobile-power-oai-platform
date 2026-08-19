from experiment_platform.backend.config import Settings
from experiment_platform.backend.stirrer import STEPS_PER_DEG, StirrerAgent, _HELPER_CS


def test_stirrer_uses_verified_reverb_chamber_driver(tmp_path):
    assert "using StirrerDll;" in _HELPER_CS
    assert "SerialPort(port, 9600, Parity.Even, 8, StopBits.One)" in _HELPER_CS
    assert "Motor.RotateV(steps, speed, 138, 138)" in _HELPER_CS
    assert 's.StartsWith("{") && s.EndsWith("}")' in _HELPER_CS
    assert "MT_Check" not in _HELPER_CS

    agent = StirrerAgent(Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web"), simulate=True)
    assert agent.open()["ok"] is True
    assert agent.move_rel(36)["steps"] == round(36 * STEPS_PER_DEG)
    assert agent.position_deg() == 36
