from types import SimpleNamespace

from experiment_platform.backend import phone_detect


def test_offline_probe_does_not_overwrite_newer_shake_address(monkeypatch, tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path)
    reads = iter([
        {"pdu_ip": "10.0.1.21", "last_seen_ms": 1},
        {"pdu_ip": "10.0.1.24", "last_seen_ms": 2},
    ])
    saved = {}
    monkeypatch.setattr(phone_detect, "_load", lambda _settings: next(reads))
    monkeypatch.setattr(phone_detect, "_save", lambda _settings, value: saved.update(value))
    monkeypatch.setattr(phone_detect, "_probe_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "experiment_platform.backend.phone_detect.AdbTransport",
        lambda: SimpleNamespace(devices=lambda: []),
    )

    result = phone_detect.detect_phone(settings)

    assert result["pdu_ip"] == "10.0.1.24"
    assert saved == {"pdu_ip": "10.0.1.24", "last_seen_ms": 2}
