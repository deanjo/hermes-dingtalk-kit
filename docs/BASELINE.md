---
status: current
applies_when: 核对当前检出的钉钉插件覆盖文件与补丁指纹
not_for: 判定线上已安装的代码版本或替代运行验收
current_authority: background-reference
supersedes: []
superseded_by: []
owner: hermes-dingtalk-kit
last_reviewed: 2026-09-07
---

# Baseline

## 当前基线

本仓库初始基线来自远程 Hermes-3：

```text
host: cicada@192.168.88.33
deploy dir: /Users/cicada/hermes-docker/hermes-3
source dir: image-patches/dingtalk-raw-process
base image: hermes-agent:stable-daocloud
```

## 覆盖文件

| 文件 | sha256 |
|---|---|
| `overlays/hermes/plugins/platforms/dingtalk/adapter.py` | `9887d1e884c2b44b53f9aef772dc6a90600e9c80fd8861512f7a22cb746218df` |
| `overlays/hermes/plugins/platforms/dingtalk/delivery_gate.py` | `7c3d2682570274a31f398d07e6e5e1b81e9a6f602fdb7903ea6d82c9cb74dd60` |
| `overlays/hermes/plugins/platforms/dingtalk/incoming.py` | `0153d9e33113b0fa807bb7ea8af2560364bd6b800f45faf862300a300a4e01d0` |
| `overlays/hermes/plugins/platforms/dingtalk/markdown.py` | `72344b724b6a87fa17c7a75fb8f5001c2ea00be65c0ec879cc088d71e1b80808` |
| `overlays/hermes/plugins/platforms/dingtalk/media.py` | `3252ed3a7536f5bdbdaa4ebeb6b979896589c411468bb2e8fff351403f66d547` |
| `overlays/hermes/plugins/platforms/dingtalk/mentions.py` | `7755bf8621d4c25c1661afc884cd4572b77b446e5e405247277d4d69ecb4b852` |
| `overlays/hermes/plugins/platforms/dingtalk/plugin_setup.py` | `d658c297f916fcab9c579dc015d30c5df13b632c809eedbe99e4f306553b0dec` |
| `overlays/hermes/plugins/platforms/dingtalk/private_send.py` | `fd1b0eadce1320760526a997dc8b356031e26ca0af2a4608e2ff3e498c0aec04` |
| `overlays/hermes/plugins/platforms/dingtalk/reply_context.py` | `24535800143d1d7f655a4fe12af307c0cb11be8a0607823ffa7efe9c97ac74a8` |
| `overlays/hermes/plugins/platforms/dingtalk/task_binding.py` | `1adb2aa53f4b40b6820116e2d962dae26222ce35e911dbf4c0219a4ddd87f7d9` |
| `overlays/hermes/plugins/platforms/dingtalk/text.py` | `81377dc485271f34410087ca020a072cbe19323ffc7551367844665e77c74c90` |
| `overlays/hermes/plugins/product_confirmation/store.py` | `00bb443b5a9aa50e04876350a2e883983274dd341ddfadd1307aeba79f931455` |
| `overlays/hermes/plugins/product_confirmation/tools.py` | `6b28849ad53aa668d8fe1916e5d2ca79254bd7adff1a6ed4fd58a056f2794e26` |
| `overlays/hermes/plugins/h1_task_write/tools.py` | `c1023dc7dee2f2676323823ae9c509d5476b352644cd51eb7bf01fde0f8746a3` |
| `overlays/hermes/gateway/run.py` | `88e6ac2e7ab2f30c8a0ba3f85d73abecc046149e06deeb84ff0fe6714af236f7` |
| `overlays/hermes/gateway/session.py` | `948a86ac483a63668a7b6990e5107778345b0415190c5bf72796dfb100217cc9` |
| `overlays/hermes/gateway/session_context.py` | `bbea82bea958c1c336647455aa2c84fd0119204bbf2af38558d7ce72bcfc404c` |

## 补丁文件

| 文件 | 行数 | sha256 |
|---|---:|---|
| `patches/dingtalk-adapter.patch` | 712 | `c9ad207b071e1d4fdd66b09c03215dface408591064ef4b479176c28e2e0f60e` |
| `patches/gateway-run.patch` | 87 | `8209542eb288b38f8b71e90bdcc2e1beeaef1f405996e7a781bd4b11438c20ce` |
| `patches/gateway-session.patch` | 47 | `64d6385851286872a181f8132f1a736062f08df97e9303b95dfda70c1578138a` |
| `patches/gateway-session-context.patch` | 57 | `e78e8a9eb5aabfd6c6677570c8dd047fa3810eff21606535b4e7cf52f0200d9a` |

## 复核命令

```bash
# 逐文件核对（与上表比对）；CI 侧由 tests/test_baseline_manifest.py 自动守护
shasum -a 256 \
  overlays/hermes/plugins/platforms/dingtalk/adapter.py \
  overlays/hermes/plugins/platforms/dingtalk/delivery_gate.py \
  overlays/hermes/plugins/platforms/dingtalk/incoming.py \
  overlays/hermes/plugins/platforms/dingtalk/markdown.py \
  overlays/hermes/plugins/platforms/dingtalk/media.py \
  overlays/hermes/plugins/platforms/dingtalk/mentions.py \
  overlays/hermes/plugins/platforms/dingtalk/plugin_setup.py \
  overlays/hermes/plugins/platforms/dingtalk/private_send.py \
  overlays/hermes/plugins/platforms/dingtalk/reply_context.py \
  overlays/hermes/plugins/platforms/dingtalk/task_binding.py \
  overlays/hermes/plugins/platforms/dingtalk/text.py \
  overlays/hermes/plugins/product_confirmation/store.py \
  overlays/hermes/plugins/product_confirmation/tools.py \
  overlays/hermes/plugins/h1_task_write/tools.py \
  overlays/hermes/gateway/run.py \
  overlays/hermes/gateway/session.py \
  overlays/hermes/gateway/session_context.py \
  patches/*.patch
```
