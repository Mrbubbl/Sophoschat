import json

from aichat import config


def test_android_update_endpoint(env):
    assert env.anon.get("/api/app/android").status_code == 401
    assert env.b.get("/api/app/android").json() == {"available": False}
    d = config.get().data_dir / "app"
    d.mkdir(exist_ok=True)
    (d / "SophosChat.apk").write_bytes(b"PK-fake-apk")
    (d / "version.json").write_text(json.dumps({"versionCode": 7, "versionName": "1.7", "notes": "x", "sha256": "ab", "size": 11}))
    r = env.b.get("/api/app/android").json()
    assert r["available"] and r["versionCode"] == 7 and r["url"] == "/api/app/android.apk"
    apk = env.b.get("/api/app/android.apk")
    assert apk.status_code == 200 and apk.content == b"PK-fake-apk"
    assert env.anon.get("/api/app/android.apk").status_code == 401
