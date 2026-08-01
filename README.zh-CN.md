# Crop2Draw（中文摘要）

完整说明与配图见英文 [README.md](README.md)。

将复杂架构图裁切为图层，导出可编辑的 [draw.io](https://www.diagrams.net/) 文件。

**仓库：** https://github.com/ChenAI-TGF/Crop2Draw

```bash
git clone https://github.com/ChenAI-TGF/Crop2Draw.git
cd Crop2Draw
pip install -r requirements.txt
copy secrets.json.example secrets.json   # 仅文字 OCR 需要
python crop_to_drawio.py path\to\figure.png
```

**切勿提交 `secrets.json`。** 协议：[MIT](LICENSE)
