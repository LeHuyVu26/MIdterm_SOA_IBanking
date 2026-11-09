import uuid, pytest, requests
from common import *

pytestmark = pytest.mark.integration

def err(r): return r.json()["detail"]["code"]

# ---- User service ----
def test_login_success_returns_token_and_profile():
    r = requests.post(f"{GW}/user/auth/login", json={"username": "sv_an", "password": "123456"})
    assert r.status_code == 200
    d = r.json(); assert d["access_token"] and d["user"]["email"] == "an@example.com"
    assert "password_hash" not in d["user"]

def test_login_wrong_password_401():
    r = requests.post(f"{GW}/user/auth/login", json={"username": "sv_an", "password": "sai"})
    assert r.status_code == 401 and err(r) == "INVALID_CREDENTIALS"

def test_login_missing_field_422():
    assert requests.post(f"{GW}/user/auth/login", json={"username": "sv_an"}).status_code == 422

def test_profile_requires_token():
    assert requests.get(f"{GW}/user/users/me").status_code == 401
    assert requests.get(f"{GW}/user/users/me", headers={"Authorization": "Bearer abc"}).status_code == 401

def test_profile_returns_current_user():
    r = requests.get(f"{GW}/user/users/me", headers=login("sv_binh"))
    assert r.status_code == 200 and r.json()["full_name"] == "Trần Thị Bình"

# ---- Tuition service ----
def test_tuition_lookup_ok():
    r = requests.get(f"{GW}/tuition/tuitions/52100001", headers=login("sv_an"))
    assert r.status_code == 200
    d = r.json(); assert d["student_name"] == "Nguyễn Văn An" and d["amount"] == 15_000_000
    
def test_tuition_not_found_404():
    r = requests.get(f"{GW}/tuition/tuitions/00000000", headers=login("sv_an"))
    assert r.status_code == 404 and err(r) == "TUITION_NOT_FOUND"

def test_tuition_requires_token():
    assert requests.get(f"{GW}/tuition/tuitions/52100001").status_code == 401

def test_internal_endpoints_are_blocked_at_gateway():
    r = requests.post(f"{GW}/tuition/internal/tuitions/52100001/reserve", json={"txn_id": "x"})
    assert r.status_code == 404
    assert requests.post(f"{GW}/payment/internal/anything").status_code == 404

# ---- Payment service: validation & errors ----
def test_account_balance():
    assert requests.get(f"{GW}/payment/payments/account", headers=login("sv_dung")).json()["balance"] == 1_000_000

def test_payment_requires_token():
    assert requests.post(f"{GW}/payment/payments", json={"mssv": "52100001"}).status_code == 401

@pytest.mark.parametrize("mssv", ["", "bad id!", "x" * 21])
def test_payment_rejects_invalid_mssv_422(mssv):
    r = requests.post(f"{GW}/payment/payments", json={"mssv": mssv}, headers=login("sv_an"))
    assert r.status_code == 422

def test_payment_unknown_mssv_404():
    r = requests.post(f"{GW}/payment/payments", json={"mssv": "99999999"}, headers=login("sv_an"))
    assert r.status_code == 404 and err(r) == "TUITION_NOT_FOUND"

def test_payment_insufficient_balance_422_and_no_transaction_created():
    h = login("sv_dung")
    before = len(requests.get(f"{GW}/payment/payments/history", headers=h).json())
    r = requests.post(f"{GW}/payment/payments", json={"mssv": "52100004"}, headers=h)
    assert r.status_code == 422 and err(r) == "INSUFFICIENT_BALANCE"
    assert len(requests.get(f"{GW}/payment/payments/history", headers=h).json()) == before
    assert balance(h) == 1_000_000

# ---- OTP ----
def test_otp_must_be_6_digits_422():
    h = login("sv_an"); t = init(h, "52100005")
    for bad in ("abc", "12345", "1234567", ""):
        assert post_verify(requests, h, t, bad).status_code == 422

def test_wrong_otp_reports_remaining_attempts():
    h = login("sv_an"); t = init(h, "52100005")
    r = post_verify(requests, h, t, wrong_otp(otp_of(t)))
    assert r.status_code == 400 and err(r) == "OTP_INVALID" and "4" in r.json()["detail"]["message"]

def test_resend_invalidates_previous_otp():
    h = login("sv_an"); t = init(h, "52100005")
    old = otp_of(t)
    assert requests.post(f"{GW}/payment/payments/{t}/resend-otp", headers=h).status_code == 200
    new = otp_of(t, skip=(old,))
    assert new != old
    r = post_verify(requests, h, t, old)
    assert r.status_code == 400 and err(r) == "OTP_INVALID"

def test_user_cannot_use_another_users_transaction():
    ha, hb = login("sv_an"), login("sv_binh"); t = init(ha, "52100005")
    r = post_verify(requests, hb, t, otp_of(t))
    assert r.status_code == 404 and err(r) == "TXN_NOT_FOUND"
    assert requests.post(f"{GW}/payment/payments/{t}/resend-otp", headers=hb).status_code == 404

def test_verify_unknown_transaction_404():
    r = post_verify(requests, login("sv_an"), str(uuid.uuid4()), "123456")
    assert r.status_code == 404

def test_verify_malformed_transaction_id_422():
    r = requests.post(f"{GW}/payment/payments/not-a-uuid/verify-otp", json={"otp": "123456"}, headers=login("sv_an"))
    assert r.status_code == 422

# ---- History ----
def test_history_only_contains_own_transactions():
    ha, hb = login("sv_an"), login("sv_binh"); t = init(ha, "52100005")
    mine = {x["id"] for x in requests.get(f"{GW}/payment/payments/history", headers=ha).json()}
    theirs = {x["id"] for x in requests.get(f"{GW}/payment/payments/history", headers=hb).json()}
    assert t in mine and t not in theirs