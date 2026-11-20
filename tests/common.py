import time, subprocess, threading, requests, concurrent.futures as cf

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # thư mục chứa docker-compose.yml
GW = "http://localhost:8080/api"

def login(u, pw="123456"):
    r = requests.post(f"{GW}/user/auth/login", json={"username": u, "password": pw}); r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}

def init(h, mssv):
    r = requests.post(f"{GW}/payment/payments", json={"mssv": mssv}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["transaction_id"]

def otp_of(txn, skip=()):
    sql = f"SELECT code FROM otps WHERE txn_id='{txn}' ORDER BY id DESC LIMIT 1"
    for _ in range(30):
        out = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "paymentdb", "-tAc", sql],
                             capture_output=True, text=True, cwd=ROOT).stdout.strip()
        if out and out not in skip: return out
        time.sleep(0.5)
    raise AssertionError("không thấy OTP trong DB")

def post_verify(s, h, txn, otp):
    return s.post(f"{GW}/payment/payments/{txn}/verify-otp", json={"otp": otp}, headers=h)

def balance(h):
    return requests.get(f"{GW}/payment/payments/account", headers=h).json()["balance"]

def wrong_otp(good):
    return "000000" if good != "000000" else "111111"

def run_parallel(jobs):
    otps = [otp_of(t) for _, t in jobs]
    barrier = threading.Barrier(len(jobs), timeout=15)

    def fire(h, txn, otp):
        s = requests.Session()
        s.get(f"{GW}/payment/payments/account", headers=h)
        barrier.wait()
        return post_verify(s, h, txn, otp).status_code

    with cf.ThreadPoolExecutor(len(jobs)) as ex:
        futs = [ex.submit(fire, h, t, o) for (h, t), o in zip(jobs, otps)]
        return [f.result() for f in futs]