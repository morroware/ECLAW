from app.config import Settings


def test_cors_origins_parses_csv_values():
    settings = Settings(cors_allowed_origins='https://example.com, https://admin.example.com')
    assert settings.cors_origins == ['https://example.com', 'https://admin.example.com']


def test_cors_origins_falls_back_when_empty():
    settings = Settings(cors_allowed_origins='   ')
    assert settings.cors_origins == ['http://localhost', 'http://127.0.0.1']
