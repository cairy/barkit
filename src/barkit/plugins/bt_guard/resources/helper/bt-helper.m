// bt-helper — BarKit「蓝牙看护」组件的最小助手：只做蓝牙控制器电源的读与切。
//
// 【为什么必须是个 .app，而且只能由 open 拉起】（2026-09-23 本机实测，别重走）
// macOS 的蓝牙私有 API 要求调用者是一个「带 NSBluetoothAlwaysUsageDescription
// 声明的进程身份」，判据是**代码签名里嵌的那份 Info.plist**，在内存里改 bundle id
// 不算数：
//   · 裸二进制 / 裸 Python 调私有 API            → SIGABRT (exit 134)
//   · 裸 Python 先改内存里的 CFBundleIdentifier  → 照样 SIGABRT
//   · 内嵌 __TEXT,__info_plist 段的裸 Mach-O     → SIGABRT
//   · 直接 exec .app/Contents/MacOS/ 内的二进制  → SIGABRT（躺进 bundle 里不算数）
//   · 同一个二进制跑【不碰蓝牙】的分支           → 正常退出（证明不是二进制本身的问题）
//   · 经 open 启动同一个 .app                    → 正常
// 所以：必须由 open（LaunchServices）拉起，且不能为了「更轻」退化成单文件。
//
// 【怎么和 app 说话】一条本机 Unix 域套接字会话，节点是
//     ~/Library/Application Support/BarKit/bt_helper.sock
// **由 app 侧先 listen、本助手起来后 connect**（app 侧那份实现是
// `src/barkit/plugins/bt_guard/bt_session.py`）。线格式与消息类型的**真源**是
// `src/barkit/capabilities/helper/protocol.py`，本文件是它的 Objective-C 镜像：
//
//   帧   = 4 字节大端长度前缀 + UTF-8 JSON 正文（单帧上限 1 MiB）
//   消息 = {"type":"hello","protocol":1,"client":"bt-helper"}
//          {"type":"welcome","ok":true,"protocol":1,"helper_version":"bt-helper"}
//          {"type":"op.run","op":"power-off","params":null,"req_id":"123-1"}
//          {"type":"reply","req_id":"123-1","ok":true,
//           "result":{"code":0,"lines":["before=1","after=0","done"]}}
//
// **改这里的任何字面量都要同批改 protocol.py（反之亦然）** —— 跨语言的这一侧
// 拿不到 Python 常量，只能两边各写一份。旧形态那对
// `bt_request.json` / `bt_result.json` 随本次改造一起消失（方案 §11.2）。
//
// 【为什么只有三条命令】单设备重连（connect / disconnect）在本机故障态下实测
// 全返回 kIOReturnError、HID 层 20 秒零变化 —— 对「蓝牙层未连 ＋ HID 无枚举」这个
// 形态无效。真正有效且免 root 的只有控制器断电重开这一档，实测 8.1 秒恢复、配对无损。
// 所以本助手只暴露它需要的三条，不堆放没用的能力（能少一条就少一条）。
//
// 命令：power | power-off | power-on
// 退出码：0 正常收尾（含 app 关连接 / 空闲上限到）/ 3 连不上 app 或握手失败
//         （单条命令成没成不看退出码，看 reply 里的 code）

#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>
#import <errno.h>
#import <string.h>
#import <sys/socket.h>
#import <sys/un.h>
#import <unistd.h>

extern int IOBluetoothPreferenceGetControllerPowerState(void);
extern void IOBluetoothPreferenceSetControllerPowerState(int powerState);

// ──────────────────────────────────────────────
// 会话（线格式与 protocol.py 同源）
// ──────────────────────────────────────────────

//: 与 `barkit/paths.py` 的 `BT_HELPER_SOCKET` 同源。**改一边必须改另一边。**
static NSString *const kSocketRel = @"Library/Application Support/BarKit/bt_helper.sock";

static const uint32_t kMaxFrame = 1u << 20;

//: 连不上时的重试：app 是「先 listen 再 open」的，正常一次就成；给它几秒只是防
//: LaunchServices 的调度抖动。100 × 50ms = 5 秒。
static const int kConnectAttempts = 100;
static const useconds_t kConnectGapUs = 50 * 1000;

//: 空闲上限（秒）：app 崩了之后本助手不该赖着不走。收不到下一帧即退出。
static const int kIdleTimeoutS = 30;

static NSMutableString *gOut = nil;

static void emit(NSString *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    NSString *s = [[NSString alloc] initWithFormat:fmt arguments:ap];
    va_end(ap);
    [gOut appendString:s];
    [gOut appendString:@"\n"];
}

//: 把 gOut 拆成「非空行」数组（reply 的 result.lines）。
static NSArray<NSString *> *takeLines(void) {
    NSMutableArray<NSString *> *lines = [NSMutableArray array];
    for (NSString *l in [gOut componentsSeparatedByString:@"\n"]) {
        if ([l length] > 0) [lines addObject:l];
    }
    [gOut setString:@""];
    return lines;
}

//: 读满 n 字节。0 = EOF、负 = 出错或超时（SO_RCVTIMEO），两者都返回 NO。
static BOOL readAll(int fd, void *buf, size_t n) {
    size_t got = 0;
    unsigned char *p = (unsigned char *)buf;
    while (got < n) {
        ssize_t r = read(fd, p + got, n - got);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) return NO;
        got += (size_t)r;
    }
    return YES;
}

//: 收一帧。拿到合法 JSON 对象返回它，其余（EOF / 超时 / 坏帧 / 超长）返回 nil。
static NSDictionary *readFrame(int fd) {
    unsigned char hdr[4];
    if (!readAll(fd, hdr, sizeof(hdr))) return nil;
    uint32_t len = ((uint32_t)hdr[0] << 24) | ((uint32_t)hdr[1] << 16)
                 | ((uint32_t)hdr[2] << 8) | (uint32_t)hdr[3];
    if (len > kMaxFrame) return nil;        // 长度前缀来自对面，不设上限＝任人分配
    NSMutableData *body = [NSMutableData dataWithLength:len];
    if (len > 0 && !readAll(fd, body.mutableBytes, len)) return nil;
    id obj = [NSJSONSerialization JSONObjectWithData:body options:0 error:NULL];
    return [obj isKindOfClass:[NSDictionary class]] ? obj : nil;
}

static BOOL writeFrame(int fd, NSDictionary *msg) {
    NSData *body = [NSJSONSerialization dataWithJSONObject:msg options:0 error:NULL];
    if (!body || body.length > kMaxFrame) return NO;
    uint32_t len = (uint32_t)body.length;
    unsigned char hdr[4] = { (unsigned char)(len >> 24), (unsigned char)(len >> 16),
                             (unsigned char)(len >> 8), (unsigned char)len };
    NSMutableData *out = [NSMutableData dataWithBytes:hdr length:sizeof(hdr)];
    [out appendData:body];
    const unsigned char *p = out.bytes;
    size_t sent = 0;
    while (sent < out.length) {
        ssize_t w = write(fd, p + sent, out.length - sent);
        if (w < 0 && errno == EINTR) continue;
        if (w <= 0) return NO;
        sent += (size_t)w;
    }
    return YES;
}

//: 连上 app 的会话节点；连不上返回 -1。**不打印任何东西** —— 这条路上没人在看
//: 标准输出（是 open 拉起来的），失败只体现为退出码。
static int connectHelper(void) {
    NSString *path = [NSHomeDirectory() stringByAppendingPathComponent:kSocketRel];
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (path.length >= sizeof(addr.sun_path)) return -1;
    strncpy(addr.sun_path, path.fileSystemRepresentation, sizeof(addr.sun_path) - 1);

    for (int attempt = 0; attempt < kConnectAttempts; attempt++) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) return -1;
        if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) return fd;
        close(fd);
        usleep(kConnectGapUs);
    }
    return -1;
}

// ──────────────────────────────────────────────
// 命令
// ──────────────────────────────────────────────

// 轮询等待控制器到达期望状态，返回是否达成。
// 为什么要等：SetControllerPowerState 是「发起」而不是「完成」—— 蓝牙栈拆掉 / 重建
// 要好几秒，调用方真正需要的下一个事实是「到了没有」，不是「我喊过了」。
// 上限 8s 的依据：本机实测整轮断电重开 8.1 秒恢复；单条命令给到同一量级。
// 这也是 app 侧 REPLY_WAIT_S（20 秒）必须宽于它的原因。
static BOOL waitPower(int want, double timeout_s) {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeout_s];
    while ([deadline timeIntervalSinceNow] > 0) {
        if (IOBluetoothPreferenceGetControllerPowerState() == want) return YES;
        usleep(200 * 1000);
    }
    return IOBluetoothPreferenceGetControllerPowerState() == want;
}

//: 跑一条命令，把输出行填进 outLines，返回命令自己的退出码（进 reply 的 code）。
static int executeOp(NSString *cmd, NSArray<NSString *> *__strong *outLines) {
    [gOut setString:@""];

    if ([cmd isEqualToString:@"power"]) {
        emit(@"state=%d", IOBluetoothPreferenceGetControllerPowerState());
        *outLines = takeLines();
        return 0;
    }

    if ([cmd isEqualToString:@"power-off"] || [cmd isEqualToString:@"power-on"]) {
        int want = [cmd isEqualToString:@"power-on"] ? 1 : 0;
        int before = IOBluetoothPreferenceGetControllerPowerState();
        emit(@"before=%d", before);
        if (before == want) {
            emit(@"after=%d", before);
            emit(@"already");
            *outLines = takeLines();
            return 0;
        }
        IOBluetoothPreferenceSetControllerPowerState(want);
        BOOL reached = waitPower(want, 8.0);
        emit(@"after=%d", IOBluetoothPreferenceGetControllerPowerState());
        emit(reached ? @"done" : @"timeout");
        *outLines = takeLines();
        return reached ? 0 : 1;
    }

    emit(@"error: unknown command %@", cmd);
    *outLines = takeLines();
    return 2;
}

// ──────────────────────────────────────────────
// 主流程：握手 → 收命令 → 回执 → 对面关连接即退出
// ──────────────────────────────────────────────

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        (void)argc;
        (void)argv;
        gOut = [NSMutableString string];

        int fd = connectHelper();
        if (fd < 0) return 3;

        struct timeval tv;
        tv.tv_sec = kIdleTimeoutS;
        tv.tv_usec = 0;
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        if (!writeFrame(fd, @{ @"type": @"hello",
                               @"protocol": @1,
                               @"client": @"bt-helper" })) {
            close(fd);
            return 3;
        }
        NSDictionary *welcome = readFrame(fd);
        if (![welcome[@"type"] isEqualToString:@"welcome"]
            || ![welcome[@"ok"] boolValue]) {
            close(fd);
            return 3;
        }

        for (;;) {
            NSDictionary *msg = readFrame(fd);
            if (!msg) break;                 // app 关了连接，或空闲到点

            if (![msg[@"type"] isEqualToString:@"op.run"]) {
                writeFrame(fd, @{ @"type": @"error",
                                  @"reason": @"unknown_type",
                                  @"detail": [NSString stringWithFormat:@"%@",
                                              msg[@"type"] ?: @"(none)"] });
                continue;
            }
            NSArray<NSString *> *lines = nil;
            int code = executeOp(msg[@"op"] ?: @"", &lines);
            NSDictionary *reply = @{
                @"type": @"reply",
                @"req_id": msg[@"req_id"] ?: @"",
                @"ok": @(code == 0),
                @"result": @{ @"code": @(code), @"lines": lines ?: @[] },
            };
            if (!writeFrame(fd, reply)) break;
        }
        close(fd);
    }
    return 0;
}
