# keil-bridge

[English](README.md) | 简体中文

把 Keil MDK（ARM-ADS，armcc 5 / armclang 6）工程转换成 `arm-none-eabi-gcc` + `make` 工程，
并把在新工程里改过的源码按原编码、原换行写回 Keil 工程。

只处理常见的写法；遇到不认识的配置或语法会给出警告或报错（带 `文件:行号`），不会去猜。

## 安装

```sh
uv tool install .        # 或在本目录下用 uv run keil-bridge ...
```

依赖：Python ≥ 3.11（只用标准库）；构建时需要 `arm-none-eabi-gcc` 和 `make`。

## 用法

```sh
keil-bridge convert path/to/Project.uvprojx out-dir [--target NAME] [--set KEY=VALUE ...] [--config x.toml] [--no-git]
cd out-dir
make                       # build/<name>.elf .hex .bin
make flash                 # st-flash；FLASHER=pyocd / FLASHER=openocd OPENOCD_ARGS=...
keil-bridge status         # M 这边改过 / K Keil 那边改过 / C 两边都改过 / ? 新文件
keil-bridge backport --dry-run
keil-bridge backport       # 写回 Keil 工程；两边都改过的文件会拒绝写回，除非加 --force
keil-bridge regen --set c_std=gnu99 --set optimize=-Os   # 改设置并重新生成 keil-bridge/
keil-bridge regen --unset c_std
```

## 转换内容

- 工程根目录（uvprojx 和它引用的所有文件、include 目录的公共祖先）下的
  `.c .h .s .cpp .hpp .inc` 文件转成 UTF-8 + LF。原编码按 ASCII / UTF-8 / GBK 检测，GBK 要求解码后再编码能还原原字节。
  构建产物、IDE 状态文件等不复制，列在 `CONVERSION.md` 里。
- `keil-bridge/`（由工具生成，`regen` 时整体重写）：
  - `project.mk`：源文件、宏定义、include 路径、CPU 参数，来自 uvprojx 的 Target 设置
  - `startup_*.c`：从 armasm 启动文件翻译出的向量表和 weak 中断函数
  - `<device>.ld`：按 IROM1/IRAM1 和启动文件里的 Stack_Size/Heap_Size 生成的链接脚本
  - `keil_compat.h`：用 `-include` 强制包含，映射 `__align`、`__weak`、`__forceinline`、`__irq`
  - `syscalls.c`：newlib 桩函数；工程里重定义了 `fputc`（Keil 的 printf 重定向写法）时，`_write` 会转调它
  - `include-shims/`：`#include` 的文件名只在忽略大小写时才匹配的，生成转发头文件，不改源码
  - `manifest.json`：每个文件的原编码、原换行和哈希，供 `status`/`backport` 使用
- 根目录：`Makefile`（只在缺失时生成，可以自己改）、`keil-bridge.toml`（覆盖设置）、
  `compile_commands.json`、`CONVERSION.md`（选项对照表和全部诊断信息）、`.editorconfig`、`.gitignore`。
- 默认 `git init` 并提交一次。

转换时会用 GCC 预处理每个源文件（所以 `#if defined(__CC_ARM)` 分支里的代码不会误报），
检查 armcc 专用写法：`__asm` 嵌入汇编、`__packed`、`__svc`、`__nop()` 等 intrinsic、`#pragma import`。

## 覆盖设置

优先级：uvprojx < `keil-bridge.toml` < `--set`。`--set` 的值会写进 `keil-bridge.toml`。

| 键 | 类型 | 说明 |
|---|---|---|
| `c_std` | 字符串 | `gnu90`、`gnu99`、`gnu11` 等；Keil 的 C90 模式对应 `gnu90` |
| `optimize` | 字符串 | `-O0` … `-O3`、`-Os`、`-Og`、`-Oz`、`-Ofast` |
| `warnings` | 字符串 | `default`、`none`、`all` |
| `defines` `undefines` `includes` `sources` | 字符串列表 | 也可以用 `<键>_add`、`<键>_remove` 增删 |
| `cpu` `fpu` `float_abi` | 字符串 | 如 `cortex-m4`、`fpv4-sp-d16`、`hard` |
| `signed_char` `short_enums` | 布尔 | |
| `flash_origin` `flash_size` `ram_origin` `ram_size` `stack_size` `heap_size` | 整数 | |
| `startup` | 字符串 | 要翻译的 armasm 启动文件；设为 `""` 表示自己提供 GCC 启动文件（用 `sources_add` 加入） |
| `linker_script` | 字符串 | 使用自己的链接脚本，不再生成 |
| `extra_cflags` `extra_asflags` `extra_ldflags` `libc_specs` | 字符串列表 | |

某个选项被覆盖后，与它有关的转换警告不再显示（例如设置了 `extra_cflags`，MiscControls 未转换的警告就不再出现）。
临时调整也可以直接 `make OPT=-O2 CSTD=gnu99 EXTRA_CFLAGS=-Wall`。

## Backport 规则

- 只写回 manifest 里记录过的源文件；新建的文件、删除的文件只给警告，uvprojx 不会被修改。
- 原来是 ASCII 的文件加了中文后按 GBK 写回；GBK 无法表示的字符（如 emoji）报错，该文件不写回。
- 原来混用 CRLF/LF 的文件，写回时统一为 CRLF（转换时会提示）。

## 不支持的内容

C51 等非 ARM 工具链、C++、armcc 的 .lib/.o、ROPI/RWPI、分散加载文件（scatter file，会改用生成的链接脚本并给出警告）、
RTE 组件、IROM2/IRAM2 和片外存储、文件/组级别的单独编译选项。

## 开发

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
