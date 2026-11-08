"""Unit test: không cần Docker/DB. Chạy:  pip install -r tests/requirements.txt && pytest tests/test_unit.py -v"""
import importlib.util, pathlib, time, jwt, pytest, requests
from fastapi import HTTPException
from pydantic import ValidationError

SVC = pathlib.Path(__file__).resolve().parent.parent / "services"

def load(name):
    spec = importlib.util.spec_from_file_location(f"svc_{name}", SVC / name / "main.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m) # type: ignore
    return m

user, tuition, notif, pay = load("user"), load("tuition"), load("notification"), load("payment")

def token(sub="1", exp=3600, secret=None):
    return "Bearer " + jwt.encode({"sub": sub, "exp": int(time.time()) + exp}, secret or pay.SECRET, "HS256")

# ---------------- User service ----------------
def test_password_hash_roundtrip_and_salted():
    h1, h2 = user.hash_pw("123456"), user.hash_pw("123456")
    assert h1 != h2 and "123456" not in h1            # có salt, không lưu plaintext
    assert user.check_pw("123456", h1) and not user.check_pw("sai", h1)

# ---------------- Payment: auth & validation ----------------
def test_uid_of_accepts_valid_token():
    assert pay.uid_of(token("7")) == 7

@pytest.mark.parametrize("hdr", [None, "", "Bearer", "Bearer abc", token(exp=-10), token(secret="khac")])
def test_uid_of_rejects_bad_tokens(hdr):
    with pytest.raises(HTTPException) as e:
        pay.uid_of(hdr)
    assert e.value.status_code == 401 and e.value.detail["code"] == "UNAUTHORIZED" # type: ignore

@pytest.mark.parametrize("v", ["52100001", "abc_1"])
def test_payreq_accepts(v): assert pay.PayReq(mssv=v).mssv == v

@pytest.mark.parametrize("v", ["", "a b", "x" * 21, "1;DROP"])
def test_payreq_rejects(v):
    with pytest.raises(ValidationError): pay.PayReq(mssv=v)

@pytest.mark.parametrize("v", ["123456", "000000"])
def test_otpreq_accepts(v): assert pay.OtpReq(otp=v).otp == v

@pytest.mark.parametrize("v", ["12345", "1234567", "abcdef", "12 456", ""])
def test_otpreq_rejects(v):
    with pytest.raises(ValidationError): pay.OtpReq(otp=v)

# ---------------- Payment: OTP generation ----------------
class FakeCur:
    """Giả lập cursor: `taken` = các mã OTP đang còn hiệu lực của giao dịch khác."""
    def __init__(self, taken=()): self.taken, self.sql, self.last = set(taken), [], None
    def execute(self, sql, params=()):
        self.sql.append(sql); self.last = (sql, params)
    def fetchone(self):
        sql, params = self.last # type: ignore
        if sql.startswith("SELECT 1"): return {"x": 1} if params[0] in self.taken else None
        if sql.startswith("INSERT"):   return {"expires_at": "T+5m"}

def test_new_otp_format_lock_and_5_minutes():
    cur = FakeCur()
    code, exp = pay.new_otp(cur, "txn-1")
    assert len(code) == 6 and code.isdigit() and exp == "T+5m"
    assert "pg_advisory_xact_lock" in cur.sql[0]                      # tuần tự hóa kiểm tra trùng
    assert any(s.startswith("UPDATE otps SET used=true") for s in cur.sql)  # vô hiệu OTP cũ
    assert any("interval '5 minutes'" in s for s in cur.sql)           # hạn 5 phút

def test_new_otp_regenerates_on_collision(monkeypatch):
    seq = iter([123456, 123456, 654321])
    monkeypatch.setattr(pay.secrets, "randbelow", lambda n: next(seq))
    code, _ = pay.new_otp(FakeCur(taken={"123456"}), "txn-2")
    assert code == "654321"                                           # bỏ mã trùng, sinh mã mới

def test_new_otp_keeps_leading_zeros(monkeypatch):
    monkeypatch.setattr(pay.secrets, "randbelow", lambda n: 42)
    assert pay.new_otp(FakeCur(), "t")[0] == "000042"

# ---------------- Payment: compensation helpers ----------------
class Resp:
    def __init__(self, ok=True, code=200): self.ok, self.status_code = ok, code

def test_safe_release_swallows_network_error(monkeypatch):
    def boom(*a): raise requests.ConnectionError("down")
    monkeypatch.setattr(pay, "tuition_call", boom)
    pay.safe_release("52100001", "t")          # không được ném lỗi

def test_safe_release_swallows_http_error(monkeypatch):
    monkeypatch.setattr(pay, "tuition_call", lambda *a: Resp(False, 500))
    pay.safe_release("52100001", "t")

def test_safe_release_calls_release(monkeypatch):
    calls = []
    monkeypatch.setattr(pay, "tuition_call", lambda *a: calls.append(a) or Resp())
    pay.safe_release("52100001", "t1")
    assert calls == [("release", "52100001", "t1")]

def test_notify_returns_false_on_failure(monkeypatch):
    def boom(*a, **k): raise requests.ConnectionError()
    monkeypatch.setattr(pay.requests, "post", boom)
    assert pay.notify("/x", {}) is False
    monkeypatch.setattr(pay.requests, "post", lambda *a, **k: Resp(True))
    assert pay.notify("/x", {}) is True

# ---------------- Internal API guard ----------------
@pytest.mark.parametrize("fn", [lambda k: tuition.internal(k), lambda k: notif.guard(k)])
def test_internal_key_required(fn):
    with pytest.raises(HTTPException) as e: fn("sai")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException): fn(None)
    fn(tuition.KEY)                            # đúng key -> không lỗi

# ---------------- Notification content ----------------
def test_otp_mail_contains_code_txn_and_ttl(monkeypatch):
    sent = {}
    monkeypatch.setattr(notif, "send", lambda to, subj, body: sent.update(to=to, subj=subj, body=body))
    notif.otp(notif.OtpMail(email="a@x.com", name="An", otp="123456", txn_id="T1", mssv="52100001", amount=15000000), notif.KEY)
    assert sent["to"] == "a@x.com" and "123456" in sent["body"] and "T1" in sent["body"] and "5 phút" in sent["body"]

def test_mail_failure_maps_to_502(monkeypatch):
    def boom(*a): raise OSError("smtp down")
    monkeypatch.setattr(notif, "send", boom)
    with pytest.raises(HTTPException) as e:
        notif.ok(notif.OkMail(email="a@x.com", name="An", txn_id="T", mssv="1", amount=1, balance=0), notif.KEY)
    assert e.value.status_code == 502 and e.value.detail["code"] == "MAIL_FAILED" # type: ignore