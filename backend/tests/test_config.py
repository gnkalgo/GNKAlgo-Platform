from app.config import Settings

def test_comma_separated_cors_origins(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,https://www.gnkalgo.com")
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["http://localhost:3000", "https://www.gnkalgo.com"]


def test_dhan_request_code_is_parsed_from_environment(monkeypatch):
    monkeypatch.setenv("DHAN_MARKET_REQUEST_CODE", "17")
    settings = Settings(_env_file=None)
    assert settings.dhan_market_request_code == 17
