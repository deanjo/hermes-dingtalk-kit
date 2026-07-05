# Security

不要在 issue、PR、commit、README 或测试 fixture 中提交真实密钥。

禁止提交：

- `.env`
- API key
- token
- cookie
- Authorization header
- DingTalk client secret
- Discourse API key

发布前运行：

```bash
./scripts/verify_no_secrets.sh
```

如果发现密钥已经提交，先轮换密钥，再重写 Git 历史；不要只删除最新提交里的明文。

