# 第三方组件声明

BarKit 自身按 [MIT](LICENSE) 发布。本文件列出**随本仓库源码一起分发**的第三方组件，
以及它们各自要求随附的声明。只列真的在仓库里的东西：没有放进来的依赖不在此列（见 §3）。

---

## 1. `hev-socks5-tunnel`（随源码分发的预编译二进制）

| 项 | 值 |
|---|---|
| 文件 | `src/barkit/plugins/ipad_tunnel/resources/hev-socks5-tunnel` |
| 上游 | <https://github.com/heiher/hev-socks5-tunnel> |
| 许可 | MIT（Copyright © 2022 hev） |
| 形态 | Mach-O 64-bit executable arm64，316,080 字节 |
| sha256 | `547b0555bdf40b0662de92cd2fa7de14ad4f01d37b54575b9fb3e78134b9a342` |
| 用途 | 建 utun 虚拟网卡，把流量转发给 iPad 侧 SOCKS5 引擎 |

**上游版本号没有登记。** 仓库认的是上表的 sha256（`capabilities/helper/rootbin.py`
的安装基线，哈希不符即拒绝安装），不是版本号 —— 记录哈希比记录版本更能锁住字节。
同一份二进制的上游 Release 是 **macOS arm64** 的 `hev-socks5-tunnel`（不是
`hev-socks5-server`）。

**本仓库没有修改这份二进制。** 安装时 `daemon.install()` 会做一次 ad-hoc 签名，
那是**在目标机上**改字节的步骤，产物不入仓库；仓库内文件保持上游原样。

### 一份二进制里静态链接的四个组件

`otool -L` 只显示 `/usr/lib/libSystem.B.dylib`，其余全部静态链入：

| 组件 | 上游 | 许可 |
|---|---|---|
| lwIP | <https://github.com/heiher/lwip>（lwIP 的 fork） | BSD-3-Clause（Copyright © 2001, 2002 Swedish Institute of Computer Science） |
| hev-socks5-core | <https://github.com/heiher/hev-socks5-core> | MIT（Copyright © 2022 hev） |
| hev-task-system | <https://github.com/heiher/hev-task-system> | MIT（Copyright © 2022 hev） |
| yaml | <https://github.com/heiher/yaml> | MIT（Copyright © 2022 hev） |

---

## 2. 许可证全文

### MIT（hev-socks5-tunnel、hev-socks5-core、hev-task-system、yaml）

```
Copyright (c) 2022 hev

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### BSD-3-Clause（lwIP）

```
Copyright (c) 2001, 2002 Swedish Institute of Computer Science.
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. The name of the author may not be used to endorse or promote products
   derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR IMPLIED
WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT
SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT
OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY
OF SUCH DAMAGE.
```

---

## 3. 不随本仓库分发的依赖

下面这些由使用者自行安装（`uv sync`），本仓库只声明版本约束，不打包、不转分发：

| 依赖 | 许可 |
|---|---|
| PySide6-Essentials / shiboken6 | LGPL-3.0（或 GPL-3.0 / 商业授权，三选一） |
| PyYAML | MIT |
| pyobjc-framework-Cocoa / -Quartz / -ApplicationServices | MIT |
| mac-ocr-text、mac-barcode-read（<https://github.com/cairy/python-skills>） | 见各自仓库 |

**一句提醒（将来若打包 `.app` 对外分发才会触发）**：PySide6 是 LGPL-3.0。
只在源码层面发布时无需处理；一旦连 Qt 一起装进安装包分发，就得满足 LGPL 的条件 ——
动态链接、随包附许可证副本、允许接收者替换 Qt 库。现在做的不是这件事，所以只留个记号。
