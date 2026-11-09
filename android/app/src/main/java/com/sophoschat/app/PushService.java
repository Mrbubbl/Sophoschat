package com.sophoschat.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

/**
 * 后台连接服务（前台服务 + 常驻通知）：
 * 1. 用 App 里已登录的 Cookie 连 wss://服务器/ws?client=android；
 * 2. 服务端判断“谁该收到通知”（静音等）后，通过这条连接推 native.notify，这里只负责弹系统通知；
 * 3. 断线自动重连（指数退避 + 网络恢复时立刻重连）；重连后向服务器拉未读数，补发断线期间漏掉的提示；
 * 4. App 在前台时不弹（网页自己会显示）。
 */
public class PushService extends Service {
    static final String CH_MSG = "messages";
    static final String CH_SVC = "service";
    static final int FG_ID = 1;
    private static final String PREFS = "push_seen";

    private final Handler main = new Handler(Looper.getMainLooper());
    private OkHttpClient client;
    private WebSocket socket;
    private boolean connecting = false;
    private boolean authBlocked = false;
    private boolean destroyed = false;
    private int backoffMs = 2000;
    private String serverUrl;
    private ConnectivityManager.NetworkCallback netCb;
    private String status = "正在连接…";

    /** 离开 App 时调用：把当前各会话的未读数记为“已知”，避免之后把已读过的当成漏掉的。 */
    public static void snapshot(Context ctx) {
        Intent i = new Intent(ctx, PushService.class).setAction("snapshot");
        try {
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i); else ctx.startService(i);
        } catch (Exception ignored) { }
    }

    public static void start(Context ctx) {
        Intent i = new Intent(ctx, PushService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i); else ctx.startService(i);
        } catch (Exception ignored) { /* 后台受限时启动会被系统拒绝，下次打开 App 再拉起 */ }
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public void onCreate() {
        super.onCreate();
        serverUrl = getString(R.string.server_url);
        client = new OkHttpClient.Builder()
                .pingInterval(30, TimeUnit.SECONDS)
                .connectTimeout(15, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .retryOnConnectionFailure(true)
                .build();
        createChannels();
        ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        netCb = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network n) { main.post(() -> { backoffMs = 2000; reconnectNow(); }); }
        };
        try { cm.registerDefaultNetworkCallback(netCb); } catch (Exception ignored) { }
        main.postDelayed(watchdog, 60_000);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        goForeground();
        if (intent != null && "snapshot".equals(intent.getAction())) {
            new Thread(() -> catchUp(false), "push-snapshot").start();
            return START_STICKY;
        }
        authBlocked = false; // 每次拉起（例如刚登录完回到 App）都重新尝试
        if (socket == null) connect();
        return START_STICKY;
    }

    @Override public void onDestroy() {
        destroyed = true;
        main.removeCallbacksAndMessages(null);
        try { if (socket != null) socket.cancel(); } catch (Exception ignored) { }
        try { ((ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE)).unregisterNetworkCallback(netCb); } catch (Exception ignored) { }
        super.onDestroy();
    }

    // 兜底：每分钟检查一次，连接丢了（比如系统冻结后恢复）就重连
    private final Runnable watchdog = new Runnable() {
        @Override public void run() {
            if (destroyed) return;
            if (socket == null && !connecting && !authBlocked) connect();
            main.postDelayed(this, 60_000);
        }
    };

    private void reconnectNow() {
        if (destroyed || authBlocked) return;
        if (socket != null) { try { socket.cancel(); } catch (Exception ignored) { } socket = null; }
        connecting = false;
        connect();
    }

    private void connect() {
        if (destroyed || connecting || socket != null) return;
        String cookie = CookieManager.getInstance().getCookie(serverUrl);
        if (cookie == null || !cookie.contains("aichat_session")) {
            authBlocked = true;
            setStatus("未登录，登录后自动连接");
            return;
        }
        connecting = true;
        String wsUrl = serverUrl.replaceFirst("^http", "ws") + "ws?client=android";
        Request req = new Request.Builder().url(wsUrl).header("Cookie", cookie).header("User-Agent", "SophosChatApp/1.1").build();
        socket = client.newWebSocket(req, new WebSocketListener() {
            @Override public void onOpen(WebSocket ws, Response r) {
                connecting = false; backoffMs = 2000;
                setStatus("已连接");
                new Thread(() -> catchUp(true), "push-catchup").start();
            }
            @Override public void onMessage(WebSocket ws, String text) {
                try {
                    JSONObject o = new JSONObject(text);
                    String type = o.optString("type");
                    if ("native.notify".equals(type)) onNotify(o);
                    else if ("session.revoked".equals(type)) { authBlocked = true; setStatus("登录已失效，请重新登录"); }
                } catch (Exception ignored) { }
            }
            @Override public void onClosed(WebSocket ws, int code, String reason) { lost(ws, code); }
            @Override public void onFailure(WebSocket ws, Throwable t, Response r) { lost(ws, r != null ? r.code() : 0); }
        });
    }

    private void lost(WebSocket ws, int code) {
        main.post(() -> {
            if (ws != socket && socket != null) return; // 已经是旧连接
            socket = null; connecting = false;
            if (destroyed) return;
            if (code == 4401 || code == 401) { authBlocked = true; setStatus("登录已失效，请重新登录"); return; }
            setStatus("连接断开，正在重连…");
            main.postDelayed(() -> { if (!destroyed && !authBlocked) connect(); }, backoffMs);
            backoffMs = Math.min(backoffMs * 2, 60_000);
        });
    }

    // ------------------------------------------------------------ 通知
    private void onNotify(JSONObject o) {
        boolean call = o.optBoolean("call", false);
        if (MainActivity.visible && !call) return; // 前台时网页自己提示
        int room = o.optInt("room_id", 0);
        show(room, o.optString("title", "SophosChat"), o.optString("body", ""), call);
        if (!call) bumpSeen(room, 1);
    }

    private void show(int roomId, String title, String body, boolean call) {
        Intent open = new Intent(this, MainActivity.class).putExtra("room_id", roomId)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, roomId, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CH_MSG) : new Notification.Builder(this);
        b.setSmallIcon(R.drawable.ic_stat).setContentTitle(title).setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(pi).setAutoCancel(true).setCategory(call ? Notification.CATEGORY_CALL : Notification.CATEGORY_MESSAGE)
                .setWhen(System.currentTimeMillis()).setShowWhen(true);
        if (Build.VERSION.SDK_INT < 26) b.setPriority(Notification.PRIORITY_HIGH);
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(10_000 + roomId, b.build());
    }

    private void createChannels() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel(CH_MSG, "新消息", NotificationManager.IMPORTANCE_HIGH));
        NotificationChannel svc = new NotificationChannel(CH_SVC, "后台连接", NotificationManager.IMPORTANCE_MIN);
        svc.setShowBadge(false);
        nm.createNotificationChannel(svc);
    }

    private Notification fgNotification() {
        PendingIntent pi = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CH_SVC) : new Notification.Builder(this);
        b.setSmallIcon(R.drawable.ic_stat).setContentTitle("SophosChat").setContentText(status).setContentIntent(pi).setOngoing(true).setShowWhen(false);
        if (Build.VERSION.SDK_INT < 26) b.setPriority(Notification.PRIORITY_MIN);
        return b.build();
    }

    private void goForeground() {
        Notification n = fgNotification();
        if (Build.VERSION.SDK_INT >= 34) startForeground(FG_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(FG_ID, n);
    }

    private void setStatus(String s) {
        status = s;
        try { ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(FG_ID, fgNotification()); } catch (Exception ignored) { }
    }

    // ------------------------------------------------------------ 补拉未读
    private SharedPreferences seen() { return getSharedPreferences(PREFS, MODE_PRIVATE); }

    private void bumpSeen(int room, int by) {
        SharedPreferences p = seen();
        p.edit().putInt("r" + room, p.getInt("r" + room, 0) + by).apply();
    }

    /** 重连后：问服务器每个会话的未读数，比“已提示过的数量”多的就补一条提示（断线期间漏掉的）。 */
    private void catchUp(boolean notify) {
        try {
            String cookie = CookieManager.getInstance().getCookie(serverUrl);
            if (cookie == null) return;
            JSONObject res = getJson("api/rooms", cookie);
            JSONArray rooms = res.optJSONArray("rooms");
            if (rooms == null) return;
            SharedPreferences p = seen();
            SharedPreferences.Editor ed = p.edit();
            for (int i = 0; i < rooms.length(); i++) {
                JSONObject r = rooms.getJSONObject(i);
                int id = r.getInt("id"), unread = r.optInt("unread", 0), known = p.getInt("r" + id, 0);
                boolean quiet = r.optBoolean("muted") || r.optBoolean("archived");
                if (notify && unread > known && !quiet && !MainActivity.visible) {
                    String t = r.optString("title", "");
                    show(id, t.isEmpty() ? "SophosChat" : t, unread + " 条未读消息 · " + r.optString("last_message", ""), false);
                }
                ed.putInt("r" + id, unread);
            }
            ed.apply();
        } catch (Exception ignored) { }
    }

    private JSONObject getJson(String path, String cookie) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(serverUrl + path).openConnection();
        c.setRequestProperty("Cookie", cookie);
        c.setConnectTimeout(15_000); c.setReadTimeout(20_000);
        try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"))) {
            StringBuilder sb = new StringBuilder(); String line;
            while ((line = r.readLine()) != null) sb.append(line);
            return new JSONObject(sb.toString());
        } finally { c.disconnect(); }
    }
}
