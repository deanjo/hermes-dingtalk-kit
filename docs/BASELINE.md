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
| `overlays/hermes/plugins/platforms/dingtalk/adapter.py` | `c2afccc75ddda63e1b89480c79ddd44f4118a5c91c8a0e8a5193e67e87e86613` |
| `overlays/hermes/gateway/run.py` | `82f206a2446e0401434dd28dabaafc5b9ba45d5cbe194200ba31040027be00fc` |
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
shasum -a 256 \
  overlays/hermes/plugins/platforms/dingtalk/adapter.py \
  overlays/hermes/gateway/run.py \
  overlays/hermes/gateway/session.py \
  overlays/hermes/gateway/session_context.py \
  patches/*.patch
```

