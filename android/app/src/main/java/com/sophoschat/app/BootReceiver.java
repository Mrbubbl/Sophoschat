package com.sophoschat.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 开机或应用更新后拉起后台连接服务。 */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context ctx, Intent intent) {
        PushService.start(ctx);
    }
}
