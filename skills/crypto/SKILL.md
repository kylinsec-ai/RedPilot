---
name: crypto
description: 密码学分析与破解。覆盖 RSA 参数攻击、AES 模式弱点、哈希碰撞、古典密码、编码识别。
---

# 密码学攻击流程

## 识别阶段
1. 看编码格式: Base64 / Hex / Base32
2. 看密文特征: 块长度 → AES/DES, 大数 → RSA
3. 看题目描述关键词

## RSA 攻击
```python
from Crypto.Util.number import long_to_bytes, inverse
# 小 e 攻击
import gmpy2
m = gmpy2.iroot(c, e)[0]
# 共模攻击
# Wiener 攻击 (e 很大)
# p-1 光滑 / 费马分解 (n 的因子接近)
```

## AES 攻击
- ECB 模式: 逐字节爆破 / 块重排
- CBC 模式: padding oracle / bit flipping
- CTR 模式: nonce 重用 → XOR 密钥流

## 古典密码
- Caesar: 枚举 26 种偏移
- Vigenere: Kasiski / 频率分析
- 替换密码: 频率分析 + 已知明文

## 工具
```bash
python3 -c "import base64; print(base64.b64decode('...'))"
openssl enc -d -aes-128-cbc -in enc.bin -K $KEY -iv $IV
```
