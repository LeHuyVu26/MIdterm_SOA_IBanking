"""Hàm dùng chung cho integration test."""
import re, time, email, threading, requests, concurrent.futures as cf

GW, MH = "http://localhost:8080/api", "http://localhost:8025/api/v2/messages?limit=200"

def login(u, pw="123456"):
    r = requests.post(f"{GW}/user/auth/login", json={"username": u, "password": pw}); r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}

def init(h, mssv):
    r = requests.post(f"{GW}/payment/payments", json={"mssv": mssv}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["transaction_id"]

def otp_of(txn, skip=()):
    """Đọc OTP mới nhất của giao dịch từ MailHog (bỏ qua các mã trong `skip`)."""
    for _ in range(30):
        for m in requests.get(MH).json()["items"]:
            body = email.message_from_string(m["Raw"]["Data"]).get_payload(decode=True).decode("utf-8", "ignore") # type: ignore
            if txn in body:
                code = re.search(r"OTP: (\d{6})", body)
                if code and code.group(1) not in skip: return code.group(1)
        time.sleep(0.5)
    raise AssertionError("không thấy email OTP")

def post_verify(s, h, txn, otp):
    return s.post(f"{GW}/payment/payments/{txn}/verify-otp", json={"otp": otp}, headers=h)

def balance(h):
    return requests.get(f"{GW}/payment/payments/account", headers=h).json()["balance"]

def wrong_otp(good):
    return "000000" if good != "000000" else "111111"

def run_parallel(jobs):
    """Lấy OTP của TẤT CẢ giao dịch trước, mở sẵn kết nối, rồi dùng Barrier để mọi thread bắn verify cùng lúc."""
    otps = [otp_of(t) for _, t in jobs]
    barrier = threading.Barrier(len(jobs), timeout=15)

    def fire(h, txn, otp):
        s = requests.Session()
        s.get(f"{GW}/payment/payments/account", headers=h)  # làm nóng kết nối keep-alive
        barrier.wait()
        return post_verify(s, h, txn, otp).status_code

    with cf.ThreadPoolExecutor(len(jobs)) as ex:
        futs = [ex.submit(fire, h, t, o) for (h, t), o in zip(jobs, otps)]
        return [f.result() for f in futs]