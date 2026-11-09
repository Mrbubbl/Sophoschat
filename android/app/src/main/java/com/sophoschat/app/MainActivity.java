package com.sophoschat.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.SharedPreferences;
import android.os.PowerManager;
import android.provider.Settings;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

/** SophosChat 外壳：用系统 WebView 打开服务器上的网页应用，补上返回键、文件选择、麦克风/摄像头和下载。 */
public class MainActivity extends Activity {
    private static final int REQ_FILE = 1001;
    private static final int REQ_PERM = 1002;
    private static final int REQ_NOTIF = 1003;

    /** 前台可见时，后台服务不弹通知（网页自己会提示）。 */
    static volatile boolean visible = false;

    private WebView web;
    private ProgressBar bar;
    private TextView errorView;
    private String serverUrl;
    private String serverHost;

    private ValueCallback<Uri[]> filePathCallback;
    private PermissionRequest pendingPermission;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        serverUrl = getString(R.string.server_url);
        serverHost = Uri.parse(serverUrl).getHost();

        FrameLayout root = new FrameLayout(this);
        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(100);
        bar.setVisibility(View.GONE);
        root.addView(bar, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 6));

        errorView = new TextView(this);
        errorView.setText("连不上服务器\n请检查网络，轻点屏幕重试");
        errorView.setTextSize(16);
        errorView.setGravity(android.view.Gravity.CENTER);
        errorView.setTextColor(0xFF555555);
        errorView.setVisibility(View.GONE);
        errorView.setOnClickListener(v -> { errorView.setVisibility(View.GONE); web.setVisibility(View.VISIBLE); web.reload(); });
        root.addView(errorView, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " SophosChatApp/1.0");
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);

        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                if (serverHost.equals(u.getHost())) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true;
            }
            @Override public void onPageStarted(WebView v, String url, Bitmap f) { bar.setVisibility(View.VISIBLE); }
            @Override public void onPageFinished(WebView v, String url) { bar.setVisibility(View.GONE); }
            @Override public void onReceivedError(WebView v, WebResourceRequest r, android.webkit.WebResourceError e) {
                if (r.isForMainFrame()) { web.setVisibility(View.INVISIBLE); errorView.setVisibility(View.VISIBLE); }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView v, int p) { bar.setProgress(p); }

            @Override public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    if (!serverHost.equals(request.getOrigin().getHost())) { request.deny(); return; }
                    List<String> need = new ArrayList<>();
                    for (String res : request.getResources()) {
                        if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(res)) need.add(Manifest.permission.RECORD_AUDIO);
                        if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(res)) need.add(Manifest.permission.CAMERA);
                    }
                    List<String> missing = new ArrayList<>();
                    for (String p : need) if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) missing.add(p);
                    if (missing.isEmpty()) { request.grant(request.getResources()); return; }
                    pendingPermission = request;
                    requestPermissions(missing.toArray(new String[0]), REQ_PERM);
                });
            }

            @Override public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams params) {
                if (filePathCallback != null) filePathCallback.onReceiveValue(null);
                filePathCallback = cb;
                try {
                    startActivityForResult(params.createIntent(), REQ_FILE);
                } catch (Exception e) {
                    filePathCallback = null;
                    return false;
                }
                return true;
            }
        });

        web.setDownloadListener((url, ua, disposition, mime, len) -> {
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                req.addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url));
                req.addRequestHeader("User-Agent", ua);
                req.setMimeType(mime);
                req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, URLUtil.guessFileName(url, disposition, mime));
                ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(req);
            } catch (Exception e) {
                try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))); } catch (Exception ignored) { }
            }
        });

        int room = getIntent().getIntExtra("room_id", 0);
        if (savedInstanceState != null) web.restoreState(savedInstanceState);
        else web.loadUrl(room > 0 ? serverUrl + "?room=" + room : serverUrl);
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        int room = intent.getIntExtra("room_id", 0);
        if (room > 0 && web != null) {
            web.evaluateJavascript("window.dispatchEvent(new CustomEvent('sophos:open-room',{detail:" + room + "}))", null);
        }
    }

    @Override protected void onResume() {
        super.onResume();
        visible = true;
        PushService.start(this);
        ensureBackgroundSetup();
    }

    /** 首次使用：申请通知权限，再引导加入电池优化白名单（国内手机还要手动开“自启动/后台运行”）。 */
    private void ensureBackgroundSetup() {
        SharedPreferences sp = getSharedPreferences("setup", MODE_PRIVATE);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                && !sp.getBoolean("notif_asked", false)) {
            sp.edit().putBoolean("notif_asked", true).apply();
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
            return;
        }
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName()) && !sp.getBoolean("battery_done", false)) {
            sp.edit().putBoolean("battery_done", true).apply(); // 只弹一次，不反复打扰
            new AlertDialog.Builder(this)
                    .setTitle("让消息在后台也能及时到达")
                    .setMessage("1. 点“去设置”，允许 SophosChat 不受电池优化限制。\n\n2. 国产手机（华为、小米、OPPO、vivo 等）还请在“应用设置”里，把“自启动”“后台运行/后台活动”也设为允许。各品牌位置不同，在应用设置里找“电池”或“启动管理”。\n\n通知栏里会有一条很小的“SophosChat”常驻通知，这是保持连接所必需的。")
                    .setPositiveButton("去设置", (d, w) -> {
                        try { startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName()))); }
                        catch (Exception e) { startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName()))); }
                    })
                    .setNeutralButton("打开应用设置", (d, w) -> startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName()))))
                    .setNegativeButton("以后再说", null)
                    .show();
        }
    }

    @Override protected void onSaveInstanceState(Bundle out) { super.onSaveInstanceState(out); web.saveState(out); }

    @Override public void onBackPressed() {
        // 网页自己用 History API 管理“弹层/聊天页”，所以先交给网页回退，到头了才退出
        if (web.canGoBack()) web.goBack(); else super.onBackPressed();
    }

    @Override protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_FILE && filePathCallback != null) {
            filePathCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(res, data));
            filePathCallback = null;
        } else super.onActivityResult(req, res, data);
    }

    @Override public void onRequestPermissionsResult(int req, String[] perms, int[] results) {
        if (req == REQ_NOTIF) { ensureBackgroundSetup(); return; }
        if (req != REQ_PERM || pendingPermission == null) return;
        boolean ok = results.length > 0;
        for (int r : results) if (r != PackageManager.PERMISSION_GRANTED) ok = false;
        if (ok) pendingPermission.grant(pendingPermission.getResources()); else pendingPermission.deny();
        pendingPermission = null;
    }

    @Override protected void onPause() {
        super.onPause();
        visible = false;
        CookieManager.getInstance().flush();
        PushService.snapshot(this);
    }
}
