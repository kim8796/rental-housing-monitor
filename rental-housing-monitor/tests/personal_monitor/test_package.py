def test_personal_monitor_package_has_version() -> None:
    import personal_monitor

    assert personal_monitor.__version__ == "0.1.0"
