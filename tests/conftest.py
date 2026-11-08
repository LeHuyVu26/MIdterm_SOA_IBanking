"""Cấu hình pytest.
- Unit test (test_unit.py): không cần hệ thống chạy.
- Integration test (test_api.py, test_concurrency.py): cần `docker compose up` + DB mới; tự bỏ qua nếu gateway không truy cập được.
"""
import os, pytest, requests

os.environ.setdefault("JWT_SECRET", "unit-test-secret")
os.environ.setdefault("INTERNAL_KEY", "unit-test-key")
os.environ.setdefault("USER_URL", "http://user")
os.environ.setdefault("TUITION_URL", "http://tuition")
os.environ.setdefault("NOTIFY_URL", "http://notify")
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/none")

GW = os.environ.get("GATEWAY", "http://localhost:8080")

def _stack_up():
    try:
        return requests.get(GW, timeout=3).ok
    except requests.RequestException:
        return False

def pytest_collection_modifyitems(config, items):
    if any("integration" in i.keywords for i in items) and not _stack_up():
        skip = pytest.mark.skip(reason=f"Hệ thống chưa chạy tại {GW} (docker compose up --build -d)")
        for i in items:
            if "integration" in i.keywords:
                i.add_marker(skip)