# 手动裁切 → Draw.io

将复杂示意图（架构图、论文配图等）手动裁切并导出为可编辑的 [draw.io](https://www.diagrams.net/) 文件。

完整说明见英文 [README.md](README.md)。

## 快速开始

```bash
pip install -r requirements.txt
copy secrets.json.example secrets.json   # 仅文字 OCR 需要；填入百度密钥
python crop_to_drawio.py path\to\figure.png
```

**切勿将 `secrets.json` 提交到 GitHub。** 仓库只包含占位文件 `secrets.json.example`。

## 许可

[MIT](LICENSE)
