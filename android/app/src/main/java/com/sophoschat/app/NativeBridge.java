package com.sophoschat.app;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.provider.Settings;
import android.webkit.JavascriptInterface;

/**
 * 给网页“设置”用的极小接口（window.SophosNative）：只有“强力保活”开关和“打开本应用的系统设置”。
 * 默认关闭：像三星这类后台管得松的手机不需要它；vivo / OPPO / 小米等被系统杀得厉害的手机再打开。
 */
public class NativeBridge {
    static final String PREFS = "native_settings";
    static final String KEY_KEEPALIVE = "keepalive";

    private final Context ctx;
    NativeBridge(Context ctx) { this.ctx = ctx.getApplicationContext(); }

    static boolean keepAlive(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_KEEPALIVE, false);
    }

    @JavascriptInterface public boolean getKeepAlive() { return keepAlive(ctx); }

    @JavascriptInterface public void setKeepAlive(boolean on) {
        SharedPreferences sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        sp.edit().putBoolean(KEY_KEEPALIVE, on).apply();
        PushService.applySettings(ctx);
    }

    @JavascriptInterface public void openAppSettings() {
        Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + ctx.getPackageName()))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try { ctx.startActivity(i); } catch (Exception ignored) { }
    }
}
