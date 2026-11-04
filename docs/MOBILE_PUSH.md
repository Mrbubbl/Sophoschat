# 手机 PWA 与消息通知

适用版本：0.4.0。当前项目采用标准 Web Push/VAPID，不包含国内厂商原生推送 SDK，也没有原生 Android 客户端。

## 已实现的推送链路

1. 在 HTTPS 站点登录，在「账号与通知」允许通知并开启离线推送。
2. 浏览器创建 PushSubscription，将 endpoint 和加密材料保存到服务器。
3. 新消息到达时，服务器调用浏览器厂商的推送服务。
4. 手机收到 push 事件后，由 Service Worker 显示系统通知，点击可回到相应聊天。

锁屏或关闭页面不等于关闭 Web Push。按标准，浏览器可以在收到推送后唤醒 Service Worker；WebSocket 连接和网页定时器则不能代替这种离线唤醒。参见 [Google 的推送 FAQ](https://web.dev/articles/push-notifications-faq)。

## 国内安卓 Chrome 的限制

Chrome 使用 FCM，网站不能自行指定它改走华为、小米、OPPO 或 vivo 的推送服务。需要服务器到推送服务，以及手机到推送服务两段网络都可用；服务器放在海外不能单独保证手机这一段送达。国内机型、系统省电策略及网络环境存在差异，需要在真实目标手机上验证。

锁定后台、允许后台联网和关闭省电限制可能有帮助，但不保证网页一直运行，也不能修复不可达的推送通道。只有通知权限，并不代表订阅创建成功或消息已送达。

通知是否显示顶部横幅、响铃和锁屏内容由系统通知类别、权限和勿扰设置决定。应用不能保证每条消息都弹出悬浮窗口。

参考：[Web Push 的厂商通道选择](https://web.dev/articles/push-notifications-overview)、[FCM 网络要求](https://firebase.google.cn/docs/cloud-messaging/network-configuration?hl=en)、[Android 通知类别](https://developer.android.com/develop/ui/compose/notifications/channels)。

## iPhone / iPad

iOS/iPadOS 16.4 及以上支持添加到主屏幕的 Web App 使用 Web Push。需要从主屏幕打开并由用户点击授予通知权限；不能将普通浏览器标签页等同于已安装 Web App。仍应验证实际设备、网络及通知设置。

参考：[Apple Web Push 文档](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers)。

## 当前已知兼容性问题

离线 push 处理使用 `ServiceWorkerRegistration.showNotification()`。但页面收到实时消息的通知分支仍使用 `new Notification()`；多数手机浏览器不支持这个构造器，应后续统一到 Service Worker 通知路径。它与 FCM 的网络可达性是两个独立问题，不能把修改这个调用说成已经解决国内离线送达。

参考：[MDN 移动端 Notification 构造器限制](https://developer.mozilla.org/en-US/docs/Web/API/Notification/Notification)。

## 建议的真机检查

| 场景 | 检查内容 |
|---|---|
| 页面打开 | 消息实时显示，权限与声音配置正常 |
| 切到其他应用 | 新消息是否到达通知栏，点击是否定位正确聊天 |
| 锁屏 5 分钟、30 分钟 | 分别测试送达时延，区分 Wi-Fi 与移动网络 |
| 关闭页面 | 验证真正的 Web Push，不依赖原页面 WebSocket |
| 关闭通知或断开推送网络 | 不应把订阅状态当作实际送达成功 |

这份代码没有完成国内多品牌手机送达率验收，不能承诺聊天级的离线可靠性。

## 后续方案

若目标是国内安卓稳定后台提醒，可保留现有网页界面和服务端，增加原生应用外壳，并集成真实厂商推送通道；聚合推送平台可统一管理这些通道。仅把 PWA 包成 APK，或使用仍依赖 FCM 的默认插件，不会自动解决问题。

厂商接入仍需对应应用注册、配置、认证及平台规则，不能声称完全不受系统和配额影响。参考：[厂商推送通道说明](https://help.aliyun.com/zh/document_detail/434676.html)、[Capacitor 默认推送插件](https://capacitorjs.com/docs/apis/push-notifications)。
