package com.sophoschat.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.app.ProgressDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.webkit.CookieManager;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

/**
 * App 外壳的 OTA：向自己的服务器问 /api/app/android，有更高 versionCode 就提示更新；
 * 下载 /api/app/android.apk（带登录 Cookie），核对 SHA-256，用系统 PackageInstaller 安装。
 * 只有签名与当前安装一致的包才能覆盖安装（系统强制），所以发布密钥必须固定。
 */
final class Updater {
    private static final long CHECK_EVERY_MS = 6L * 3600 * 1000;
    private static boolean busy = false;

    static void maybeCheck(Activity a, String serverUrl) {
        SharedPreferences sp = a.getSharedPreferences("updater", Context.MODE_PRIVATE);
        long now = System.currentTimeMillis();
        if (busy || now - sp.getLong("last_check", 0) < CHECK_EVERY_MS) return;
        sp.edit().putLong("last_check", now).apply();
        busy = true;
        Handler ui = new Handler(Looper.getMainLooper());
        new Thread(() -> {
            try {
                String cookie = CookieManager.getInstance().getCookie(serverUrl);
                if (cookie == null || !cookie.contains("aichat_session")) { busy = false; sp.edit().putLong("last_check", 0).apply(); return; }
                JSONObject info = new JSONObject(get(serverUrl + "api/app/android", cookie));
                int remote = info.optInt("versionCode", 0);
                if (!info.optBoolean("available") || remote <= currentVersion(a) || remote == sp.getInt("skipped", 0)) { busy = false; return; }
                ui.post(() -> ask(a, serverUrl, cookie, info));
            } catch (Exception e) {
                busy = false;
            }
        }, "update-check").start();
    }

    private static void ask(Activity a, String serverUrl, String cookie, JSONObject info) {
        if (a.isFinishing()) { busy = false; return; }
        String size = info.optLong("size", 0) > 0 ? String.format(" · %.1f MB", info.optLong("size") / 1048576.0) : "";
        new AlertDialog.Builder(a)
                .setTitle("发现新版本 " + info.optString("versionName") + size)
                .setMessage(info.optString("notes", "").isEmpty() ? "更新 App 外壳（聊天内容和功能会随服务器自动更新）。" : info.optString("notes"))
                .setPositiveButton("更新", (d, w) -> download(a, serverUrl, cookie, info))
                .setNeutralButton("跳过这个版本", (d, w) -> {
                    a.getSharedPreferences("updater", Context.MODE_PRIVATE).edit().putInt("skipped", info.optInt("versionCode")).apply();
                    busy = false;
                })
                .setNegativeButton("以后", (d, w) -> busy = false)
                .setOnCancelListener(d -> busy = false)
                .show();
    }

    @SuppressWarnings("deprecation")
    private static void download(Activity a, String serverUrl, String cookie, JSONObject info) {
        if (Build.VERSION.SDK_INT >= 26 && !a.getPackageManager().canRequestPackageInstalls()) {
            Toast.makeText(a, "请允许 SophosChat “安装未知应用”，返回后会再次提示更新", Toast.LENGTH_LONG).show();
            a.getSharedPreferences("updater", Context.MODE_PRIVATE).edit().putLong("last_check", 0).apply();
            busy = false;
            a.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + a.getPackageName())));
            return;
        }
        ProgressDialog pd = new ProgressDialog(a);
        pd.setMessage("正在下载新版本…");
        pd.setProgressStyle(ProgressDialog.STYLE_HORIZONTAL);
        pd.setMax(100);
        pd.setCancelable(false);
        pd.show();
        Handler ui = new Handler(Looper.getMainLooper());
        new Thread(() -> {
            File f = new File(a.getCacheDir(), "update.apk");
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(serverUrl + "api/app/android.apk").openConnection();
                c.setRequestProperty("Cookie", cookie);
                c.setConnectTimeout(15_000); c.setReadTimeout(60_000);
                long total = c.getContentLengthLong(), got = 0;
                MessageDigest sha = MessageDigest.getInstance("SHA-256");
                try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(f)) {
                    byte[] buf = new byte[64 * 1024]; int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n); sha.update(buf, 0, n); got += n;
                        if (total > 0) { final int pct = (int) (got * 100 / total); ui.post(() -> pd.setProgress(pct)); }
                    }
                } finally { c.disconnect(); }
                String want = info.optString("sha256", "");
                if (!want.isEmpty() && !want.equalsIgnoreCase(hex(sha.digest()))) throw new Exception("安装包校验失败");
                install(a, f);
                ui.post(() -> { pd.dismiss(); busy = false; });
            } catch (Exception e) {
                ui.post(() -> {
                    pd.dismiss(); busy = false;
                    Toast.makeText(a, "下载失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
                });
            }
        }, "update-download").start();
    }

    private static void install(Context ctx, File apk) throws Exception {
        PackageInstaller pi = ctx.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(ctx.getPackageName());
        int id = pi.createSession(params);
        try (PackageInstaller.Session s = pi.openSession(id)) {
            try (InputStream in = new FileInputStream(apk); OutputStream out = s.openWrite("base.apk", 0, apk.length())) {
                byte[] buf = new byte[64 * 1024]; int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                s.fsync(out);
            }
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
            PendingIntent cb = PendingIntent.getBroadcast(ctx, id, new Intent(ctx, InstallReceiver.class), flags);
            s.commit(cb.getIntentSender());
        }
    }

    @SuppressWarnings("deprecation")
    private static int currentVersion(Context ctx) {
        try {
            PackageInfo p = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            return Build.VERSION.SDK_INT >= 28 ? (int) p.getLongVersionCode() : p.versionCode;
        } catch (Exception e) { return 0; }
    }

    private static String get(String url, String cookie) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestProperty("Cookie", cookie);
        c.setConnectTimeout(15_000); c.setReadTimeout(20_000);
        try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"))) {
            StringBuilder sb = new StringBuilder(); String line;
            while ((line = r.readLine()) != null) sb.append(line);
            return sb.toString();
        } finally { c.disconnect(); }
    }

    private static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x));
        return sb.toString();
    }

    private Updater() { }
}
