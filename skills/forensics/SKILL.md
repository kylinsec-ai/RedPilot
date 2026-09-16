---
name: forensics
description: 数字取证与隐写分析。覆盖文件雕复、内存取证、流量分析、图片隐写、磁盘镜像分析。
---

# 数字取证流程

## 文件类型识别
```bash
file $FILE
xxd $FILE | head -20
binwalk $FILE
```

## 流量分析
```bash
tshark -r $PCAP -T fields -e http.request.uri
tshark -r $PCAP -Y "http" -T json
tshark -r $PCAP -Y "tcp.stream eq 0" -T fields -e data | xxd -r -p
```

## 图片隐写
```bash
exiftool $IMAGE
steghide extract -sf $IMAGE
strings $IMAGE | grep -i flag
binwalk -e $IMAGE
# LSB 隐写: zsteg / stegsolve
```

## 内存取证
```bash
volatility3 -f $DUMP windows.info
volatility3 -f $DUMP windows.pslist
volatility3 -f $DUMP windows.filescan | grep -i flag
volatility3 -f $DUMP windows.dumpfiles --virtaddr $ADDR
```

## 磁盘取证
```bash
mmls $IMAGE        # 分区表
fls -r $IMAGE      # 文件列表
icat $IMAGE $INODE # 提取文件
foremost -i $IMAGE -o output/
```
