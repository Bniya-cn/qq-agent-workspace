import urllib.request
import json

def test():
    urls = [
        ("POST", "http://127.0.0.1:8080/api/switch/small_talk", {"enable": True}),
        ("POST", "http://127.0.0.1:8080/api/switch/auto_reply", {"enable": True}),
        ("POST", "http://127.0.0.1:8080/api/switch/small_talk_style", {"style": "group_mimic"}),
    ]
    for method, u, body in urls:
        try:
            req = urllib.request.Request(
                u,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method=method
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                print(f"OK {u} -> code={resp.status}, body={resp.read().decode('utf-8')}")
        except urllib.error.HTTPError as e:
            print(f"HTTPError {u} -> code={e.code}, body={e.read().decode('utf-8')}")
        except Exception as e:
            print(f"FAIL {u} -> {e}")

if __name__ == "__main__":
    test()
