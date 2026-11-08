"""Tình huống concurrency + OTP lockout. Chạy trên DB MỚI:  docker compose down -v && docker compose up --build -d
    pip install -r tests/requirements.txt && pytest tests -v
Các test chạy theo thứ tự khai báo (có phụ thuộc trạng thái dữ liệu mẫu)."""
import pytest, requests
from common import *

pytestmark = pytest.mark.integration

def test_wrong_otp_5_times_locks_transaction():
    """Sai OTP quá 5 lần -> FAILED, OTP đúng cũng không dùng lại được, không bị trừ tiền."""
    hc = login("sv_chi")
    t0 = init(hc, "52100005")
    good = otp_of(t0); wrong = wrong_otp(good)
    s, codes = requests.Session(), []
    for _ in range(5):
        r = post_verify(s, hc, t0, wrong)
        codes.append((r.status_code, r.json()["detail"]["code"]))
    assert codes[:4] == [(400, "OTP_INVALID")] * 4 and codes[4] == (400, "OTP_LOCKED"), codes
    assert post_verify(s, hc, t0, good).status_code == 409
    hist = requests.get(f"{GW}/payment/payments/history", headers=hc).json()
    assert next(x for x in hist if x["id"] == t0)["status"] == "FAILED"
    assert balance(hc) == 20_000_000

def test_same_account_concurrent_payments_never_overspend():
    """1 tài khoản (20tr), 2 giao dịch 15tr đồng thời -> chỉ 1 thành công, số dư không âm."""
    h1 = login("sv_an"); t1, t2 = init(h1, "52100001"), init(h1, "52100002")
    codes = run_parallel([(h1, t1), (h1, t2)])
    assert sorted(codes) == [200, 422] and balance(h1) == 5_000_000, codes

def test_same_tuition_paid_only_once():
    """2 tài khoản cùng trả 1 MSSV -> chỉ 1 thành công, người còn lại không bị trừ tiền."""
    h2, h3 = login("sv_binh"), login("sv_chi")
    ta, tb = init(h2, "52100003"), init(h3, "52100003")
    codes = run_parallel([(h2, ta), (h3, tb)])
    bals = sorted([balance(h2), balance(h3)])
    assert sorted(codes) == [200, 409] and bals == [8_000_000, 20_000_000], (codes, bals)

def test_paid_tuition_cannot_be_paid_again():
    """Sau khi đã thanh toán, tra cứu báo PAID và tạo giao dịch mới bị từ chối (409)."""
    h = login("sv_dung")
    t = requests.get(f"{GW}/tuition/tuitions/52100003", headers=h).json()
    assert t["status"] == "PAID"
    r = requests.post(f"{GW}/payment/payments", json={"mssv": "52100003"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TUITION_NOT_PAYABLE"